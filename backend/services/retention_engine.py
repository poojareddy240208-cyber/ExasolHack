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


def parse_nlp_udf_output(raw_output: Any) -> Dict[str, Any]:
    """
    Parses structured NLP output from Exasol PREDICT_CHURN_SCORE UDF (JSON VARCHAR(2000)).
    Extracts:
    - sentiment (Float)
    - urgency (String: 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL')
    - root_cause (String: 'FULFILLMENT_FAILURE', 'PRODUCT_PREFERENCE', 'BILLING_ERROR')
    """
    import json

    sentiment = 0.0
    urgency = "LOW"
    root_cause = "PRODUCT_PREFERENCE"

    if isinstance(raw_output, (int, float)):
        sentiment = float(raw_output)
        urgency = (
            "CRITICAL"
            if sentiment >= 0.9
            else (
                "HIGH"
                if sentiment >= 0.75
                else ("MEDIUM" if sentiment >= 0.5 else "LOW")
            )
        )
        root_cause = (
            "FULFILLMENT_FAILURE" if sentiment >= 0.85 else "PRODUCT_PREFERENCE"
        )
    elif isinstance(raw_output, dict):
        sentiment = float(
            raw_output.get("sentiment", raw_output.get("churn_score", 0.0))
        )
        raw_urg = raw_output.get("urgency", "MEDIUM")
        if isinstance(raw_urg, (int, float)):
            urgency = (
                "CRITICAL"
                if raw_urg >= 0.9
                else (
                    "HIGH"
                    if raw_urg >= 0.75
                    else ("MEDIUM" if raw_urg >= 0.5 else "LOW")
                )
            )
        else:
            urgency = str(raw_urg).upper()
        root_cause = str(raw_output.get("root_cause", "PRODUCT_PREFERENCE")).upper()
    elif isinstance(raw_output, str):
        try:
            parsed = json.loads(raw_output)
            if isinstance(parsed, dict):
                sentiment = float(
                    parsed.get("sentiment", parsed.get("churn_score", 0.0))
                )
                raw_urg = parsed.get("urgency", "MEDIUM")
                if isinstance(raw_urg, (int, float)):
                    urgency = (
                        "CRITICAL"
                        if raw_urg >= 0.9
                        else (
                            "HIGH"
                            if raw_urg >= 0.75
                            else ("MEDIUM" if raw_urg >= 0.5 else "LOW")
                        )
                    )
                else:
                    urgency = str(raw_urg).upper()
                root_cause = str(parsed.get("root_cause", "PRODUCT_PREFERENCE")).upper()
            else:
                sentiment = float(parsed)
                urgency = "CRITICAL" if sentiment >= 0.9 else "MEDIUM"
                root_cause = (
                    "FULFILLMENT_FAILURE" if sentiment >= 0.85 else "PRODUCT_PREFERENCE"
                )
        except Exception:
            try:
                sentiment = float(raw_output)
                urgency = "CRITICAL" if sentiment >= 0.9 else "MEDIUM"
                root_cause = (
                    "FULFILLMENT_FAILURE" if sentiment >= 0.85 else "PRODUCT_PREFERENCE"
                )
            except Exception:
                sentiment = 0.0
                urgency = "LOW"
                root_cause = "PRODUCT_PREFERENCE"

    valid_urgencies = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
    if urgency not in valid_urgencies:
        urgency = (
            "CRITICAL"
            if sentiment >= 0.9
            else (
                "HIGH"
                if sentiment >= 0.75
                else ("MEDIUM" if sentiment >= 0.5 else "LOW")
            )
        )

    valid_causes = {"FULFILLMENT_FAILURE", "PRODUCT_PREFERENCE", "BILLING_ERROR"}
    if root_cause not in valid_causes:
        if any(kw in root_cause for kw in ["FULFILL", "SHIP", "DELIV", "SERVICE"]):
            root_cause = "FULFILLMENT_FAILURE"
        elif any(kw in root_cause for kw in ["BILL", "CHARGE", "PRICE", "REFUND"]):
            root_cause = "BILLING_ERROR"
        else:
            root_cause = "PRODUCT_PREFERENCE"

    return {
        "sentiment": round(sentiment, 4),
        "urgency": urgency,
        "root_cause": root_cause,
    }


def evaluate_retention_action(
    root_cause: str,
    urgency: str,
    sentiment: float = 0.0,
    ltv_tier: str = "",
) -> Dict[str, str]:
    """
    Context-Aware Action Engine for VIP retention alerts.
    Implements rule engine:
    - If root_cause == 'PRODUCT_PREFERENCE' -> AUTO_RETURN_LABEL
    - If root_cause == 'FULFILLMENT_FAILURE' and urgency == 'CRITICAL' -> ESCALATE_HUMAN_CONCIERGE_AND_OVERNIGHT_REPLACEMENT
    - If root_cause == 'BILLING_ERROR' -> AUTO_REFUND_AND_10_PCT_APOLOGY
    - Includes xai_explanation rationale string.
    """
    rc = (root_cause or "").upper().strip()
    urg = (urgency or "").upper().strip()

    if rc == "PRODUCT_PREFERENCE":
        action = "AUTO_RETURN_LABEL"
        explanation = (
            "Product preference issue detected. Customer dissatisfied with item style, color, or variant. "
            "Automated prepaid return label recommended."
        )
    elif rc == "FULFILLMENT_FAILURE" and urg == "CRITICAL":
        action = "ESCALATE_HUMAN_CONCIERGE_AND_OVERNIGHT_REPLACEMENT"
        explanation = (
            "Critical urgency detected due to fulfillment failure. "
            "Standard discount insufficient. Human concierge escalation required."
        )
    elif rc == "FULFILLMENT_FAILURE":
        action = "EXPEDITED_REPLACEMENT_AND_TRACKING_DISPATCH"
        explanation = (
            f"Fulfillment delay detected with {urg or 'HIGH'} urgency. "
            "Expedited replacement dispatch and carrier priority escalation recommended."
        )
    elif rc == "BILLING_ERROR":
        action = "AUTO_REFUND_AND_10_PCT_APOLOGY"
        explanation = (
            "Billing discrepancy or charge error detected. "
            "Automated full refund and 10% apology coupon recommended."
        )
    else:
        if urg == "CRITICAL":
            action = "ESCALATE_HUMAN_CONCIERGE_AND_OVERNIGHT_REPLACEMENT"
            explanation = "Critical urgency detected. Immediate human concierge escalation required."
        else:
            action = "VIP_CONCIERGE_OUTREACH"
            explanation = (
                f"Elevated churn risk ({round(sentiment, 2)}) for {ltv_tier or 'VIP customer'}. "
                "Dedicated customer success outreach recommended."
            )

    return {
        "recommended_action": action,
        "xai_explanation": explanation,
    }


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
        List of identified VIP churn risk records with parsed NLP intelligence and recommended actions.
    """
    sql = get_golden_retention_join_sql(churn_threshold, ltv_percentile)
    logger.info(
        f"[retention] Executing Exasol Golden JOIN (Churn >= {churn_threshold}, LTV >= {ltv_percentile})..."
    )

    conn = connect_exasol(host=host, schema="OMNISYNC_ANALYTICS")
    try:
        results = conn.export_to_list(sql)
        records: List[Dict[str, Any]] = []
        for row in results:
            raw_output = row[3]
            nlp_data = parse_nlp_udf_output(raw_output)
            sentiment = nlp_data["sentiment"]
            urgency = nlp_data["urgency"]
            root_cause = nlp_data["root_cause"]
            ltv_tier = str(row[6])

            action_eval = evaluate_retention_action(
                root_cause=root_cause,
                urgency=urgency,
                sentiment=sentiment,
                ltv_tier=ltv_tier,
            )
            recommended_action = action_eval["recommended_action"]
            xai_explanation = action_eval["xai_explanation"]
            action_triggered = (
                str(row[7]) if len(row) > 7 and row[7] else recommended_action
            )

            record = {
                "ticket_id": str(row[0]),
                "user_id": str(row[1]),
                "sentiment_excerpt": str(row[2])[:200] if row[2] else "",
                "churn_score": sentiment,
                "sentiment": sentiment,
                "urgency": urgency,
                "root_cause": root_cause,
                "total_spend": float(row[4]),
                "ltv_percent_rank": float(row[5]),
                "ltv_tier": ltv_tier,
                "recommended_action": recommended_action,
                "action_triggered": action_triggered,
                "xai_explanation": xai_explanation,
            }
            records.append(record)

        logger.info(
            f"[retention] Golden JOIN identified {len(records)} VIP churn risks."
        )

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
            new_records = [
                rec for rec in records if rec["ticket_id"] not in already_audited
            ]
            for rec in new_records:
                db_session.add(
                    RetentionCampaignAudit(
                        audit_id=uuid.uuid4(),
                        ticket_id=rec["ticket_id"],
                        user_id=rec["user_id"],
                        churn_score=rec["sentiment"],
                        ltv_tier=rec["ltv_tier"],
                        action_triggered=rec["recommended_action"],
                        status="DISPATCHED",
                        created_at=datetime.now(timezone.utc),
                        approved_by=None,
                        executed_at=None,
                        xai_explanation=rec["xai_explanation"],
                        urgency=rec["urgency"],
                        root_cause=rec["root_cause"],
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
