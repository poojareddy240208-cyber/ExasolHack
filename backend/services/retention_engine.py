"""
OmniSync Retention Engine & VIP Churn Detection.

Executes the Exasol Golden JOIN:
- Ingests support ticket text and scores sentiment via PyTorch UDF: `PREDICT_CHURN_SCORE(cleaned_text)`.
- Connects high-churn tickets (`churn_score >= 0.85`) with the user's Lifetime Value (Top 20% LTV).
- Dispatches retention interventions and audits saves in PostgreSQL `retention_campaign_audit`.

Strict Architectural Invariants:
- Exasol is a passive analytics engine: Celery Beat or operational triggers initiate the query.
- Inference occurs in-database via PyTorch UDF; weights reside in BucketFS.
- Zero analytics queries block transactional checkout.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from backend.models.sales import RetentionCampaignAudit
from backend.services.exasol_client import connect_exasol

logger = logging.getLogger("omnisync.retention")

DEFAULT_CHURN_THRESHOLD = 0.85
DEFAULT_LTV_PERCENTILE = 0.80  # Top 20%


def get_vip_churn_radar_sql(
    churn_threshold: float = DEFAULT_CHURN_THRESHOLD,
    ltv_percentile: float = DEFAULT_LTV_PERCENTILE,
) -> str:
    """
    Builds the Exasol SQL Golden JOIN query.

    Calculates customer LTV from GOLDEN_CUSTOMERS.TOTAL_SPEND_USD (rebuilt from RAW_PURCHASES),
    identifies the Top 20% percentile cutoff, scores support ticket sentiment via
    PREDICT_CHURN_SCORE UDF as a standard scalar function in the SELECT list,
    and resolves tickets (including anonymous tickets) by joining STAGING_ZENDESK_TICKETS
    to IDENTITY_NODES (matching email or device context), which joins to GOLDEN_CUSTOMERS.
    """
    return f"""
    WITH customer_ltv AS (
        -- LTV is read strictly from GOLDEN_CUSTOMERS.TOTAL_SPEND_USD: spend is already stitched across every
        -- linked account and guest device (rebuilt from RAW_PURCHASES net of refunds by the stitching job).
        SELECT
            GOLDEN_CUSTOMER_ID,
            TOTAL_SPEND_USD AS total_spend,
            PERCENT_RANK() OVER (ORDER BY TOTAL_SPEND_USD) AS ltv_percent_rank
        FROM OMNISYNC_ANALYTICS.GOLDEN_CUSTOMERS
    ),
    vip_customers AS (
        SELECT
            GOLDEN_CUSTOMER_ID,
            total_spend,
            ltv_percent_rank,
            CASE
                WHEN ltv_percent_rank >= 0.99 THEN 'Top 1% VIP'
                WHEN ltv_percent_rank >= 0.95 THEN 'Top 5% VIP'
                WHEN ltv_percent_rank >= 0.90 THEN 'Top 10% VIP'
                ELSE 'Top 20% VIP'
            END AS ltv_tier
        FROM customer_ltv
        WHERE ltv_percent_rank >= {ltv_percentile}
    ),
    ticket_keys AS (
        -- A ticket with no user id and no email has no identity to resolve, so it is left out here.
        -- Email-only tickets carry the email in USER_ID; it is normalised like IDENTITY_NODES.EMAIL
        -- (lower-case, "+tag" removed).
        SELECT
            TICKET_ID,
            USER_ID,
            CLEANED_TEXT,
            REGEXP_REPLACE(LOWER(TRIM(USER_ID)), '\\+[^@]*@', '@') AS EMAIL_KEY
        FROM OMNISYNC_ANALYTICS.STAGING_ZENDESK_TICKETS
        WHERE USER_ID IS NOT NULL
    ),
    ticket_nodes AS (
        -- STAGING_ZENDESK_TICKETS -> IDENTITY_NODES, one plain equality per matching rule so Exasol can
        -- hash-join each of them (a single OR condition would force a nested loop).
        SELECT k.TICKET_ID, n.NODE_ID
        FROM ticket_keys k JOIN OMNISYNC_ANALYTICS.IDENTITY_NODES n ON n.SOURCE_ID = k.USER_ID
        UNION
        SELECT k.TICKET_ID, n.NODE_ID
        FROM ticket_keys k JOIN OMNISYNC_ANALYTICS.IDENTITY_NODES n ON n.EMAIL = k.EMAIL_KEY
        UNION
        SELECT k.TICKET_ID, n.NODE_ID
        FROM ticket_keys k
        JOIN OMNISYNC_ANALYTICS.IDENTITY_NODES n ON n.NODE_ID = 'ZENDESK:' || LOWER(TRIM(k.USER_ID))
    ),
    vip_tickets AS (
        -- IDENTITY_NODES -> GOLDEN_CUSTOMER_MEMBERS -> GOLDEN_CUSTOMERS, then keep only VIP customers, so the
        -- (expensive) churn model below only scores tickets that could trigger a retention action.
        SELECT DISTINCT
            k.TICKET_ID,
            k.CLEANED_TEXT,
            gc.GOLDEN_CUSTOMER_ID,
            vc.total_spend,
            vc.ltv_percent_rank,
            vc.ltv_tier
        FROM ticket_keys k
        JOIN ticket_nodes tn ON tn.TICKET_ID = k.TICKET_ID
        JOIN OMNISYNC_ANALYTICS.GOLDEN_CUSTOMER_MEMBERS m ON m.NODE_ID = tn.NODE_ID
        JOIN OMNISYNC_ANALYTICS.GOLDEN_CUSTOMERS gc ON gc.GOLDEN_CUSTOMER_ID = m.GOLDEN_CUSTOMER_ID
        JOIN vip_customers vc ON vc.GOLDEN_CUSTOMER_ID = gc.GOLDEN_CUSTOMER_ID
    ),
    scored AS (
        -- PREDICT_CHURN_SCORE is a scalar UDF (RETURNS DOUBLE), called like any function in the SELECT list.
        SELECT
            t.TICKET_ID,
            t.CLEANED_TEXT,
            t.GOLDEN_CUSTOMER_ID,
            t.total_spend,
            t.ltv_percent_rank,
            t.ltv_tier,
            OMNISYNC_ANALYTICS.PREDICT_CHURN_SCORE(t.CLEANED_TEXT) AS CHURN_SCORE
        FROM vip_tickets t
    )
    SELECT
        TICKET_ID,
        GOLDEN_CUSTOMER_ID AS USER_ID,
        CLEANED_TEXT AS sentiment_excerpt,
        CHURN_SCORE AS churn_score,
        total_spend,
        ltv_percent_rank,
        ltv_tier,
        CASE
            WHEN ltv_percent_rank >= 0.95 THEN 'ACTION TRIGGERED: Concierge Replacement'
            WHEN ltv_percent_rank >= 0.90 THEN 'ACTION TRIGGERED: Priority Escalation'
            ELSE 'ACTION TRIGGERED: VIP Win-Back Offer'
        END AS action_triggered
    FROM scored
    WHERE CHURN_SCORE >= {churn_threshold}
    ORDER BY total_spend DESC, CHURN_SCORE DESC
    """


# Alias for backward compatibility
get_golden_retention_join_sql = get_vip_churn_radar_sql


def run_retention_vip_join(
    host: Optional[str] = None,
    churn_threshold: float = DEFAULT_CHURN_THRESHOLD,
    ltv_percentile: float = DEFAULT_LTV_PERCENTILE,
    db_session: Optional[Session] = None,
) -> List[Dict[str, Any]]:
    """
    Executes the Exasol Golden JOIN connecting Churn Score >= 0.85 with Top 20% LTV VIP customers.
    Optionally records matched VIP interventions into PostgreSQL `RetentionCampaignAudit`.

    Args:
        host: Optional Exasol host override (e.g. 'localhost').
        churn_threshold: Churn score cutoff (defaults to 0.85).
        ltv_percentile: Lifetime value percentile cutoff (defaults to 0.80 / Top 20%).
        db_session: Optional SQLAlchemy session for PostgreSQL audit logging.

    Returns:
        List of identified VIP churn risk records.
    """
    sql = get_golden_retention_join_sql(churn_threshold, ltv_percentile)
    logger.info(f"[retention] Executing Exasol Golden JOIN (Churn >= {churn_threshold}, LTV >= {ltv_percentile})...")

    conn = connect_exasol(host=host, schema="OMNISYNC_ANALYTICS")
    try:
        results = conn.export_to_list(sql)
        records: List[Dict[str, Any]] = []
        for row in results:
            record = {
                "ticket_id": str(row[0]),
                "user_id": str(row[1]),
                "sentiment_excerpt": str(row[2])[:200] if row[2] else "",
                "churn_score": float(row[3]),
                "total_spend": float(row[4]),
                "ltv_percent_rank": float(row[5]),
                "ltv_tier": str(row[6]),
                "action_triggered": str(row[7]),
            }
            records.append(record)

        logger.info(f"[retention] Golden JOIN identified {len(records)} VIP churn risks.")

        # If a PostgreSQL session is provided, audit each NEW intervention. The join re-evaluates every staged
        # ticket on each run, so tickets that already have a campaign are skipped (no duplicate campaigns).
        if db_session and records:
            ticket_ids = [rec["ticket_id"] for rec in records]
            already_audited = {
                row[0]
                for row in db_session.query(RetentionCampaignAudit.ticket_id)
                .filter(RetentionCampaignAudit.ticket_id.in_(ticket_ids))
                .all()
            }
            new_records = [rec for rec in records if rec["ticket_id"] not in already_audited]
            for rec in new_records:
                db_session.add(
                    RetentionCampaignAudit(
                        audit_id=uuid.uuid4(),
                        ticket_id=rec["ticket_id"],
                        user_id=rec["user_id"],
                        churn_score=rec["churn_score"],
                        ltv_tier=rec["ltv_tier"],
                        action_triggered=rec["action_triggered"],
                        status="DISPATCHED",
                        created_at=datetime.now(timezone.utc),
                    )
                )
            db_session.commit()
            logger.info(
                f"[retention] Logged {len(new_records)} new VIP campaigns ({len(already_audited)} already audited)."
            )

        return records
    except Exception as exc:
        logger.error(f"[retention] Error executing Golden JOIN: {exc}")
        if db_session:
            db_session.rollback()
        raise
    finally:
        conn.close()
