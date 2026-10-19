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
        -- Evaluates customer lifetime value using GOLDEN_CUSTOMERS.TOTAL_SPEND_USD
        -- (stitched across all linked accounts and guest devices from RAW_PURCHASES)
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
    scored_tickets AS (
        SELECT 
            t.TICKET_ID,
            t.USER_ID,
            t.CLEANED_TEXT,
            t.CREATED_AT,
            OMNISYNC_ANALYTICS.PREDICT_CHURN_SCORE(t.CLEANED_TEXT) AS CHURN_SCORE
        FROM OMNISYNC_ANALYTICS.STAGING_ZENDESK_TICKETS t
    )
    SELECT DISTINCT
        st.TICKET_ID,
        COALESCE(st.USER_ID, gc.PRIMARY_EMAIL, gc.GOLDEN_CUSTOMER_ID) AS USER_ID,
        st.CLEANED_TEXT AS sentiment_excerpt,
        st.CHURN_SCORE AS churn_score,
        vc.total_spend,
        vc.ltv_percent_rank,
        vc.ltv_tier,
        CASE 
            WHEN vc.ltv_percent_rank >= 0.95 THEN 'ACTION TRIGGERED: Concierge Replacement'
            WHEN vc.ltv_percent_rank >= 0.90 THEN 'ACTION TRIGGERED: Priority Escalation'
            ELSE 'ACTION TRIGGERED: VIP Win-Back Offer'
        END AS action_triggered
    FROM scored_tickets st
    JOIN OMNISYNC_ANALYTICS.IDENTITY_NODES n 
        ON (
            st.USER_ID = n.SOURCE_ID
            OR LOWER(TRIM(st.USER_ID)) = LOWER(TRIM(n.EMAIL))
            OR n.NODE_ID = st.USER_ID
            OR n.NODE_ID = 'DEVICE:' || st.USER_ID
            OR n.NODE_ID = 'ZENDESK:' || LOWER(TRIM(st.USER_ID))
            OR n.NODE_ID = 'SHOPIFY:' || st.USER_ID
        )
    LEFT JOIN OMNISYNC_ANALYTICS.GOLDEN_CUSTOMER_MEMBERS m 
        ON n.NODE_ID = m.NODE_ID
    JOIN OMNISYNC_ANALYTICS.GOLDEN_CUSTOMERS gc 
        ON (gc.GOLDEN_CUSTOMER_ID = m.GOLDEN_CUSTOMER_ID OR gc.PRIMARY_EMAIL = n.EMAIL)
    JOIN vip_customers vc 
        ON gc.GOLDEN_CUSTOMER_ID = vc.GOLDEN_CUSTOMER_ID
    WHERE st.CHURN_SCORE >= {churn_threshold}
    ORDER BY vc.total_spend DESC, st.CHURN_SCORE DESC
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

        # If PostgreSQL session is provided, audit the interventions
        if db_session and records:
            for rec in records:
                audit = RetentionCampaignAudit(
                    audit_id=uuid.uuid4(),
                    ticket_id=rec["ticket_id"],
                    user_id=rec["user_id"],
                    churn_score=rec["churn_score"],
                    ltv_tier=rec["ltv_tier"],
                    action_triggered=rec["action_triggered"],
                    status="DISPATCHED",
                    created_at=datetime.now(timezone.utc),
                )
                db_session.add(audit)
            db_session.commit()
            logger.info(f"[retention] Successfully logged {len(records)} VIP campaigns to RetentionCampaignAudit.")

        return records
    except Exception as exc:
        logger.error(f"[retention] Error executing Golden JOIN: {exc}")
        if db_session:
            db_session.rollback()
        raise
    finally:
        conn.close()
