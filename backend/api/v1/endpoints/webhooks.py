"""
Webhook Ingestion Endpoints.

Handles external webhooks, specifically Zendesk ticket creation events.
Payloads are immediately validated via Pydantic v2 and dropped into Celery to avoid
API timeouts and decouple fast-ingest from in-database NLP analytics in Exasol.
"""

import logging
from typing import Optional
from fastapi import APIRouter, Header, HTTPException, status
from kombu.exceptions import OperationalError
from backend.schemas.webhook import ZendeskTicketPayload, WebhookResponse

logger = logging.getLogger("omnisync.webhooks")

router = APIRouter()


@router.post(
    "/zendesk",
    response_model=WebhookResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest Zendesk Support Ticket",
    description=(
        "Receives Zendesk webhook events and immediately enqueues the payload into Celery. "
        "Exasol downstream runs a Transformer-based NLP Classifier via Python UDFs "
        "to compute Churn Score (threshold >= 0.85) and join with Top 20% LTV VIP customers."
    ),
)
def handle_zendesk_webhook(
    payload: ZendeskTicketPayload,
    x_zendesk_signature: Optional[str] = Header(
        None, description="HMAC verification signature provided by Zendesk webhook settings"
    ),
):
    """
    Synchronously accepts Zendesk support ticket payload in a worker thread.
    Avoids API timeouts and event-loop blocking by pushing directly to Redis / Celery task queue with a tight timeout.
    """
    logger.info(f"Received Zendesk ticket webhook: {payload.ticket_id} for user {payload.email or payload.user_id}")

    try:
        from backend.celery_app import process_zendesk_ticket_task

        payload_dict = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload.dict()
        task = process_zendesk_ticket_task.apply_async(
            args=[payload_dict],
            expires=10,
        )
        active_task_id = task.id
    except (OperationalError, ConnectionError, TimeoutError, OSError) as exc:
        logger.error(f"Celery broker unreachable during webhook dispatch: {exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Message broker unavailable. Unable to enqueue support ticket.",
        )
    except Exception as exc:
        logger.error(f"Unexpected error enqueuing Zendesk ticket {payload.ticket_id}: {exc}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Service unavailable: {str(exc)}",
        )

    return WebhookResponse(
        status="queued",
        task_id=active_task_id,
        ticket_id=payload.ticket_id,
        message="Payload dispatched to Celery worker for asynchronous NLP sentiment and churn analysis.",
    )

