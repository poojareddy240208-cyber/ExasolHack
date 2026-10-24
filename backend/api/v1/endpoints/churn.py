"""
VIP Churn Early Warning read endpoint.

Alerts are written to PostgreSQL `retention_campaign_audit` by the Celery Beat "VIP churn radar" task, after Exasol
joins a high churn score (>= 0.85) with a Top 20% LTV golden customer. This endpoint only lists them.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from fastapi import APIRouter, Depends, Query, status, HTTPException
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.db.session import get_db
from backend.models.sales import RetentionCampaignAudit
from backend.schemas.dashboard import (
    ChurnActionApproveRequest,
    ChurnActionApproveResponse,
    ChurnAlert,
    ChurnAlertsResponse,
)
from backend.services.dashboard_db import UNAVAILABLE_DETAIL, fetch_all

logger = logging.getLogger("omnisync.churn")

router = APIRouter()


def _emails_for(db: Session, golden_ids) -> Dict[str, Optional[str]]:
    """Primary email per golden customer, when the Golden Record has been synced. Never fails the request."""
    if not golden_ids:
        return {}
    try:
        placeholders = ", ".join(f":g{i}" for i in range(len(golden_ids)))
        rows = fetch_all(
            db,
            "SELECT golden_customer_id, primary_email FROM golden_customers "
            f"WHERE golden_customer_id IN ({placeholders})",
            {f"g{i}": gid for i, gid in enumerate(golden_ids)},
        )
    except HTTPException:
        return {}
    return {r[0]: r[1] for r in rows}


@router.get(
    "/alerts",
    response_model=ChurnAlertsResponse,
    summary="Recent VIP churn alerts (churn score >= 0.85 and Top 20% lifetime value)",
)
def list_churn_alerts(
    limit: int = Query(50, ge=1, le=200),
    since_hours: int = Query(
        24,
        ge=1,
        le=24 * 90,
        description="Window for the count_in_window headline number",
    ),
    min_score: float = Query(0.0, ge=0.0, le=1.0),
    db: Session = Depends(get_db),
):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    try:
        total = db.query(func.count(RetentionCampaignAudit.audit_id)).scalar() or 0
        in_window = (
            db.query(func.count(RetentionCampaignAudit.audit_id))
            .filter(RetentionCampaignAudit.created_at >= cutoff)
            .scalar()
            or 0
        )
        rows = (
            db.query(RetentionCampaignAudit)
            .filter(RetentionCampaignAudit.churn_score >= min_score)
            .order_by(RetentionCampaignAudit.created_at.desc())
            .limit(limit)
            .all()
        )
    except SQLAlchemyError as exc:
        db.rollback()
        logger.warning("Churn alert query failed: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=UNAVAILABLE_DETAIL)

    emails = _emails_for(db, sorted({row.user_id for row in rows}))
    alerts = [
        ChurnAlert(
            ticket_id=row.ticket_id,
            golden_customer_id=row.user_id,
            customer_email=emails.get(row.user_id),
            churn_score=row.churn_score,
            ltv_tier=row.ltv_tier,
            action_triggered=row.action_triggered,
            status=row.status,
            created_at=row.created_at,
            xai_explanation=getattr(row, "xai_explanation", None),
            urgency=getattr(row, "urgency", None),
            root_cause=getattr(row, "root_cause", None),
            approved_by=getattr(row, "approved_by", None),
            executed_at=getattr(row, "executed_at", None),
        )
        for row in rows
    ]
    return ChurnAlertsResponse(
        since_hours=since_hours,
        count_in_window=int(in_window),
        total_alerts=int(total),
        alerts=alerts,
    )


@router.post(
    "/alerts/{ticket_id}/approve",
    response_model=ChurnActionApproveResponse,
    summary="Approve and execute a recommended VIP retention action (governance & idempotency)",
    responses={
        200: {"description": "Action executed or already executed (idempotent)"},
        404: {"description": "Alert not found"},
    },
)
def approve_churn_alert(
    ticket_id: str,
    body: ChurnActionApproveRequest,
    db: Session = Depends(get_db),
):
    audit = db.query(RetentionCampaignAudit).filter(RetentionCampaignAudit.ticket_id == ticket_id).first()
    if not audit:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Retention campaign alert for ticket '{ticket_id}' not found.",
        )

    # Idempotency rule: if status is already ACTION_EXECUTED, return 200 with message "Action already executed."
    if audit.status == "ACTION_EXECUTED":
        return ChurnActionApproveResponse(
            ticket_id=audit.ticket_id,
            status=audit.status,
            message="Action already executed.",
            approved_by=audit.approved_by,
            executed_at=audit.executed_at,
        )

    # Update status to ACTION_EXECUTED, record approved_by and executed_at
    now_utc = datetime.now(timezone.utc)
    audit.status = "ACTION_EXECUTED"
    audit.approved_by = body.approved_by
    audit.executed_at = now_utc

    try:
        db.commit()
        db.refresh(audit)
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error(f"Failed to commit alert approval for ticket {ticket_id}: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update retention campaign audit status.",
        )

    return ChurnActionApproveResponse(
        ticket_id=audit.ticket_id,
        status="ACTION_EXECUTED",
        message="Action approved and executed successfully.",
        approved_by=audit.approved_by,
        executed_at=audit.executed_at,
    )
