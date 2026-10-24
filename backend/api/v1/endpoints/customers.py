"""
Golden Record read endpoints (Identity Stitching dashboard).

The Golden Record is built inside Exasol (weighted probabilistic matching) and copied to PostgreSQL
(`golden_customers`, `golden_customer_members`) by Celery Beat. These endpoints search and read that copy.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from backend.api.v1.endpoints.return_risk import build_return_risk
from backend.db.session import get_db
from backend.schemas.dashboard import (
    GoldenCustomerProfile,
    GoldenCustomerSearchResponse,
    GoldenCustomerSummary,
    GoldenMember,
    GoldenStatsResponse,
    IdentityInvestigationResponse,
)
from backend.services.dashboard_db import (
    fetch_all,
    fetch_one,
    ltv_tier,
    percent_rank_for_spend,
)
from backend.services.identity_stitching import generate_identity_investigation

router = APIRouter()

_SUMMARY_COLUMNS = (
    "gc.golden_customer_id, gc.primary_email, gc.member_count, gc.total_spend_usd, gc.first_seen_at, gc.last_seen_at"
)


def _summary(db: Session, row) -> GoldenCustomerSummary:
    spend = float(row[3])
    return GoldenCustomerSummary(
        golden_customer_id=row[0],
        primary_email=row[1],
        member_count=row[2],
        total_spend_usd=spend,
        first_seen_at=row[4],
        last_seen_at=row[5],
        ltv_tier=ltv_tier(percent_rank_for_spend(db, spend)),
    )


@router.get(
    "/stats",
    response_model=GoldenStatsResponse,
    summary="Headline numbers for the Golden Record page",
)
def golden_stats(db: Session = Depends(get_db)):
    totals = fetch_one(
        db,
        "SELECT COUNT(*), COALESCE(SUM(member_count), 0), "
        "COALESCE(SUM(CASE WHEN member_count > 1 THEN 1 ELSE 0 END), 0), MAX(synced_at) FROM golden_customers",
    )
    customers, nodes, multi = int(totals[0]), int(totals[1]), int(totals[2])
    return GoldenStatsResponse(
        golden_customers=customers,
        stitched_nodes=nodes,
        multi_identity_customers=multi,
        avg_members_per_customer=round(nodes / customers, 3) if customers else 0.0,
        last_synced_at=totals[3],
    )


@router.get(
    "",
    response_model=GoldenCustomerSearchResponse,
    summary="Search golden customers by email, golden id, Shopify user id, device id or Zendesk email",
)
def search_golden_customers(
    q: str = Query(
        ...,
        min_length=2,
        max_length=255,
        description="Email (partial ok) or an exact id",
    ),
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
):
    term = q.strip()
    rows = fetch_all(
        db,
        f"SELECT DISTINCT {_SUMMARY_COLUMNS} FROM golden_customers gc "
        "LEFT JOIN golden_customer_members m ON m.golden_customer_id = gc.golden_customer_id "
        "WHERE lower(gc.primary_email) LIKE :like OR gc.golden_customer_id = :term "
        "OR m.source_id = :term OR lower(m.source_id) = :lowered "
        "ORDER BY gc.total_spend_usd DESC LIMIT :limit",
        {
            "like": f"%{term.lower()}%",
            "term": term,
            "lowered": term.lower(),
            "limit": limit,
        },
    )
    results = [_summary(db, row) for row in rows]
    return GoldenCustomerSearchResponse(query=term, count=len(results), results=results)


@router.get(
    "/{id}/investigation",
    response_model=IdentityInvestigationResponse,
    summary="Explainable AI (XAI) Identity Detective investigation evidence and confidence",
    responses={404: {"description": "Identity investigation not found"}},
)
def get_identity_investigation(id: str, db: Session = Depends(get_db)):
    result = generate_identity_investigation(id, db)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Identity investigation not found for '{id}'.",
        )
    return IdentityInvestigationResponse(**result)


@router.get(
    "/{golden_customer_id}",
    response_model=GoldenCustomerProfile,
    summary="Full Golden Record profile: stitched identities, lifetime value and return risk",
    responses={404: {"description": "Unknown golden customer id"}},
)
def get_golden_customer(golden_customer_id: str, db: Session = Depends(get_db)):
    row = fetch_one(
        db,
        f"SELECT {_SUMMARY_COLUMNS} FROM golden_customers gc WHERE gc.golden_customer_id = :id",
        {"id": golden_customer_id},
    )
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"Golden customer '{golden_customer_id}' not found.",
        )

    summary = _summary(db, row)
    member_rows = fetch_all(
        db,
        "SELECT node_id, node_type, source_id, confidence FROM golden_customer_members "
        "WHERE golden_customer_id = :id ORDER BY confidence DESC, node_id",
        {"id": golden_customer_id},
    )
    members = [GoldenMember(node_id=r[0], node_type=r[1], source_id=r[2], confidence=float(r[3])) for r in member_rows]

    # The risk level is per golden customer, so any Shopify account or guest device of the customer carries it.
    risk = None
    for member in members:
        if member.node_type in ("SHOPIFY", "DEVICE"):
            candidate = build_return_risk(db, member.source_id)
            if candidate.found:
                risk = candidate
                break

    return GoldenCustomerProfile(
        **summary.model_dump(),
        ltv_percent_rank=percent_rank_for_spend(db, summary.total_spend_usd),
        members=members,
        return_risk=risk,
    )
