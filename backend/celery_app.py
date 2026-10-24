"""
OmniSync Celery Application & Task Scheduler.

Orchestrates asynchronous tasks:
1. Ingesting, cleaning, and staging Zendesk ticket payloads into Exasol Personal Edition.
2. Celery Beat periodic scheduler triggering Exasol-to-Postgres data synchronization
   via `EXPORT ... INTO JDBC` commands.

Strict Architectural Rules:
- Exasol is a passive analytics engine: Celery Beat acts as the external orchestrator.
- No model training or inference data leaves Exasol; weights stay inside BucketFS.
- Staging ingestion decouples fast HTTP 202 webhook intake from in-database NLP classification.
"""

import logging
from typing import Any, Dict
from celery import Celery
from celery.schedules import crontab
import pyexasol

from backend.core.config import settings
from backend.core.exasol import insert_staging_ticket
from backend.utils.nlp_cleaner import clean_ticket_text, normalize_timestamp

logger = logging.getLogger("omnisync.celery")

# Initialize Celery app instance
celery = Celery(
    "omnisync",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)
celery_app = celery  # Module export alias for consistent task decorator referencing

# Celery Configuration
celery.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=300,  # 5 minutes maximum for batch tasks
    worker_prefetch_multiplier=1,
    # Fail fast when Redis is unreachable so the API can answer 503 instead of hanging (connect timeout only;
    # a socket read timeout would also affect the worker's long-polling connection).
    broker_connection_timeout=2,
)

# Periodic Task Schedule (Celery Beat)
celery.conf.beat_schedule = {
    # Periodically trigger Exasol LightGBM demand surge predictions & push to Postgres
    "sync-inventory-virtual-safety-stock-every-5-min": {
        "task": "backend.celery_app.trigger_exasol_inventory_sync",
        "schedule": 300.0,  # Every 5 minutes
    },
    # Module 3: recompute Return Risk Levels in Exasol, then EXPORT INTO JDBC the new metrics to Postgres
    "sync-return-risk-levels-every-5-min": {
        "task": "backend.celery_app.trigger_exasol_return_risk_sync",
        "schedule": 300.0,  # Every 5 minutes
    },
    # Module 3: rebuild the Golden Record (identity stitching) inside Exasol
    "stitch-identities-golden-record-hourly": {
        "task": "backend.celery_app.trigger_identity_stitching_batch",
        "schedule": crontab(minute=0),  # Top of every hour
    },
    # Module 4: score staged Zendesk tickets and join churn >= 0.85 with Top 20% LTV VIP customers
    "vip-churn-radar-every-5-min": {
        "task": "backend.celery_app.trigger_vip_churn_radar",
        "schedule": 300.0,  # Every 5 minutes
    },
    # Inventory Reservation Reaper: releases expired PENDING reservations every 60 seconds
    "reap-expired-inventory-reservations-every-60s": {
        "task": "backend.celery_app.reap_expired_reservations_task",
        "schedule": 60.0,  # Every 60 seconds
    },
}


@celery_app.task(
    bind=True,
    name="process_zendesk_ticket_task",
    max_retries=3,
    default_retry_delay=5,
    # The webhook never reads the result. Ignoring it stops publish from subscribing to the Redis result
    # backend, which made an outage hang the API for over a minute.
    ignore_result=True,
)
def process_zendesk_ticket_task(self, ticket_payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Asynchronous task for processing, cleaning, and staging Zendesk ticket webhooks.

    Workflow:
    1. Validates and extracts ticket fields (ticket_id, user_id/email, created_at, comment/description).
    2. Runs comment/description through NLP cleaner (strips HTML, control characters, excess whitespace).
    3. Inserts the record into OMNISYNC_ANALYTICS.STAGING_ZENDESK_TICKETS via PyExasol with STATUS='PENDING'.
    4. Triggers Celery retry logic on database connection errors.

    Args:
        self: Bound Celery task instance.
        ticket_payload: Raw Zendesk ticket dictionary.

    Returns:
        Structured result summary including status, ticket_id, and staging details.
    """
    if not isinstance(ticket_payload, dict):
        logger.error(f"[Celery] Invalid ticket payload type received: {type(ticket_payload)}")
        raise ValueError(f"Expected dict for ticket_payload, received {type(ticket_payload).__name__}")

    # Extract ticket_id from direct keys or nested ticket structure
    ticket_id = (
        ticket_payload.get("ticket_id")
        or ticket_payload.get("id")
        or (ticket_payload.get("ticket", {}).get("id") if isinstance(ticket_payload.get("ticket"), dict) else None)
    )
    if not ticket_id:
        logger.error(f"[Celery] Missing ticket_id in payload: {ticket_payload}")
        raise ValueError("Payload missing required ticket_id field")

    ticket_id = str(ticket_id).strip()

    # Extract user_id or email
    user_id = (
        ticket_payload.get("user_id")
        or ticket_payload.get("email")
        or ticket_payload.get("requester_id")
        or (
            ticket_payload.get("ticket", {}).get("requester_id")
            if isinstance(ticket_payload.get("ticket"), dict)
            else None
        )
        or (ticket_payload.get("ticket", {}).get("user_id") if isinstance(ticket_payload.get("ticket"), dict) else None)
    )
    user_id = str(user_id).strip() if user_id is not None else None

    # Extract comment / description text
    comment_val = ticket_payload.get("comment")
    if isinstance(comment_val, dict):
        comment_text = comment_val.get("body", "")
    elif isinstance(comment_val, str):
        comment_text = comment_val
    else:
        comment_text = ""

    description_text = ticket_payload.get("description") or ""
    if not isinstance(description_text, str):
        description_text = str(description_text)

    # If top-level text is empty, check nested ticket structure
    if not comment_text and not description_text and isinstance(ticket_payload.get("ticket"), dict):
        nested_ticket = ticket_payload["ticket"]
        nested_comment = nested_ticket.get("comment")
        if isinstance(nested_comment, dict):
            comment_text = nested_comment.get("body", "")
        elif isinstance(nested_comment, str):
            comment_text = nested_comment
        description_text = nested_ticket.get("description", "") or ""

    raw_text = comment_text or description_text or ticket_payload.get("subject", "")
    if not isinstance(raw_text, str):
        raw_text = str(raw_text)

    # Sanitize text for NLP processing
    cleaned_text = clean_ticket_text(raw_text)

    # Extract and normalize timestamp
    created_at_raw = ticket_payload.get("created_at") or (
        ticket_payload.get("ticket", {}).get("created_at") if isinstance(ticket_payload.get("ticket"), dict) else None
    )
    created_at = normalize_timestamp(created_at_raw)

    logger.info(
        f"[Celery] Staging Zendesk ticket {ticket_id} for user '{user_id}' "
        f"(raw_len={len(raw_text)}, cleaned_len={len(cleaned_text)})"
    )

    try:
        insert_staging_ticket(
            ticket_id=ticket_id,
            user_id=user_id,
            raw_text=raw_text,
            cleaned_text=cleaned_text,
            created_at=created_at,
            status="PENDING",
        )
    except (pyexasol.ExaError, ConnectionError, OSError) as exc:
        retries = self.request.retries
        max_retries = self.max_retries or 3
        logger.error(
            f"[Celery] Exasol database error staging ticket {ticket_id}: {exc}. "
            f"Scheduling retry ({retries + 1}/{max_retries})..."
        )
        raise self.retry(exc=exc, countdown=5)
    except Exception as exc:
        logger.error(
            f"[Celery] Unhandled error while staging ticket {ticket_id}: {exc}",
            exc_info=True,
        )
        raise

    return {
        "status": "STAGED",
        "ticket_id": ticket_id,
        "user_id": user_id,
        "raw_text_length": len(raw_text),
        "cleaned_text_length": len(cleaned_text),
        "created_at": created_at,
        "staging_table": "OMNISYNC_ANALYTICS.STAGING_ZENDESK_TICKETS",
    }


# Register alias under fully-qualified name for backwards compatibility
celery.tasks["backend.celery_app.process_zendesk_ticket_task"] = process_zendesk_ticket_task


@celery.task(bind=True, name="backend.celery_app.trigger_exasol_inventory_sync")
def trigger_exasol_inventory_sync(self):
    """
    Celery Beat task (every 5 minutes): refreshes the Exasol telemetry table from live Postgres stock and
    Redis cart velocity, then has Exasol compute the virtual safety buffer (LightGBM UDF) and writes it to the
    PostgreSQL `inventory.virtual_safety_buffer` column that checkout reads.
    """
    from backend.services.reverse_etl import (
        refresh_inventory_telemetry,
        sync_inventory_buffer_to_postgres,
    )

    logger.info("[Celery Beat] Refreshing inventory telemetry and syncing virtual safety buffer...")
    telemetry = refresh_inventory_telemetry()
    return {
        **sync_inventory_buffer_to_postgres(),
        "telemetry_rows": telemetry["telemetry_rows"],
    }


@celery.task(
    bind=True,
    name="backend.celery_app.trigger_vip_churn_radar",
    soft_time_limit=240,
)
def trigger_vip_churn_radar(self):
    """
    Celery Beat task (every 5 minutes): runs the Exasol Golden JOIN over staged Zendesk tickets and records
    each new VIP churn risk (churn >= 0.85, Top 20% LTV) in PostgreSQL `retention_campaign_audit`.
    """
    from backend.db.session import SessionLocal
    from backend.services.retention_engine import run_retention_vip_join

    logger.info("[Celery Beat] Running VIP churn radar...")
    session = SessionLocal()
    try:
        records = run_retention_vip_join(db_session=session)
    finally:
        session.close()
    return {"status": "RADAR_COMPLETE", "vip_churn_risks": len(records)}


@celery.task(
    bind=True,
    name="backend.celery_app.trigger_exasol_return_risk_sync",
    soft_time_limit=240,  # below the 5-minute schedule so a slow run stops cleanly instead of being killed
)
def trigger_exasol_return_risk_sync(self):
    """
    Celery Beat task (every 5 minutes): recompute Return Risk Levels (1-5) inside Exasol, then have
    Celery issue the Exasol `EXPORT ... INTO JDBC` that copies them (and the Golden Record summary) into
    PostgreSQL, so checkout only reads a pre-computed integer.
    """
    from backend.services.return_risk import run_return_risk_scoring
    from backend.services.reverse_etl import sync_module3_to_postgres

    logger.info("[Celery Beat] Recomputing Return Risk Levels in Exasol...")
    risk_counts = run_return_risk_scoring()
    logger.info("[Celery Beat] Exporting Module 3 metrics Exasol -> Postgres via EXPORT INTO JDBC...")
    sync_result = sync_module3_to_postgres()
    return {
        "target": "POSTGRES_USER_RISK_CACHE",
        "risk_level_counts": risk_counts,
        **sync_result,
    }


@celery.task(
    bind=True,
    name="backend.celery_app.trigger_identity_stitching_batch",
    time_limit=1800,  # stitching is the heaviest Module 3 job, so it gets more than the 5 minute default
)
def trigger_identity_stitching_batch(self):
    """
    Celery Beat task (hourly): triggers probabilistic entity resolution inside Exasol
    to rebuild the Unified Golden Record. The next 5-minute sync exports the result to Postgres.
    """
    from backend.services.identity_stitching import run_identity_stitching

    logger.info("[Celery Beat] Triggering Exasol Identity Stitching batch job...")
    return {
        "status": "STITCHING_COMPLETE",
        "target": "EXASOL_GOLDEN_RECORD",
        **run_identity_stitching(),
    }


@celery.task(
    bind=True,
    name="backend.celery_app.reap_expired_reservations_task",
    soft_time_limit=50,
)
def reap_expired_reservations_task(self):
    """
    Celery Beat task (every 60 seconds): finds expired PENDING reservations,
    marks them EXPIRED, and decrements reserved_stock to restore available inventory.
    """
    from backend.db.session import SessionLocal
    from backend.services.inventory_manager import release_expired_reservations

    logger.info("[Celery Beat] Reaping expired inventory reservations...")
    db = SessionLocal()
    try:
        count = release_expired_reservations(db)
        logger.info(f"[Celery Beat] Released {count} expired inventory reservations.")
        return {"status": "SUCCESS", "released_count": count}
    finally:
        db.close()
