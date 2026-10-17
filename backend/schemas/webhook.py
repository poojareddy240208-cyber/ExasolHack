"""
Pydantic v2 Schemas for Zendesk Webhooks.
Captures customer support ticket payloads to be asynchronously queued to Celery.
"""

from typing import Optional, Dict, Any
from datetime import datetime, timezone
from pydantic import BaseModel, Field, ConfigDict


class ZendeskTicketPayload(BaseModel):
    """
    Standard Zendesk webhook ticket creation/update payload.
    Immediately dropped into Celery to avoid API timeouts.
    """

    model_config = ConfigDict(from_attributes=True)

    ticket_id: str = Field(..., description="Unique ticket identifier from Zendesk")
    user_id: Optional[str] = Field(None, description="Known customer ID if authenticated")
    email: Optional[str] = Field(None, description="Customer contact email")
    subject: str = Field(..., description="Ticket subject line")
    description: str = Field(..., description="Ticket content / complaint text for NLP churn sentiment analysis")
    priority: str = Field("normal", description="Ticket priority: low, normal, high, urgent")
    status: str = Field("new", description="Ticket lifecycle status")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Ticket timestamp")
    custom_fields: Dict[str, Any] = Field(default_factory=dict, description="Metadata & tags")


class WebhookResponse(BaseModel):
    """Immediate acknowledgement returned to Zendesk (202 Accepted)."""

    status: str = "queued"
    task_id: str
    ticket_id: str
    message: str = "Payload dispatched to Celery worker for asynchronous NLP sentiment and churn analysis."
