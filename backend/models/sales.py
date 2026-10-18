"""
Sales Ledger Model - Transactional OLTP (PostgreSQL).

Records immutable completed checkout transactions.
Connected via Foreign Key to `Inventory` to guarantee relational integrity.
"""

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from sqlalchemy import String, Integer, DateTime, ForeignKey, Uuid, Float
from sqlalchemy.orm import relationship, Mapped, mapped_column

from backend.models.base import Base

if TYPE_CHECKING:
    from backend.models.inventory import Inventory


class SalesLedger(Base):
    """
    Immutable ledger of successful retail transactions.
    Captures completed checkouts for auditing and Exasol OLAP batch ingestion.
    """

    __tablename__ = "sales_ledger"
    __table_args__ = {"extend_existing": True}


    transaction_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, doc="Unique transaction UUID"
    )
    item_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("inventory.item_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
        doc="Foreign Key referencing inventory.item_id",
    )
    user_id: Mapped[str] = mapped_column(
        String(128), nullable=False, index=True, doc="Customer ID or stitched golden identity"
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, doc="Quantity purchased in this transaction")
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
        doc="UTC timestamp of transaction completion",
    )

    # Relationships
    inventory_item: Mapped["Inventory"] = relationship("Inventory", back_populates="sales")

    def __repr__(self) -> str:
        return (
            f"<SalesLedger(transaction_id={self.transaction_id}, item_id='{self.item_id}', "
            f"user_id='{self.user_id}', quantity={self.quantity}, timestamp='{self.timestamp}')>"
        )


class RetentionCampaignAudit(Base):
    """
    Audit log of retention interventions and automated win-back campaigns.
    Logs VIP customers saved when Churn Score >= 0.85 joins with Top 20% LTV.
    """

    __tablename__ = "retention_campaign_audit"
    __table_args__ = {"extend_existing": True}


    audit_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, doc="Unique campaign audit UUID"
    )
    ticket_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True, doc="Zendesk support ticket ID"
    )
    user_id: Mapped[str] = mapped_column(
        String(128), nullable=False, index=True, doc="Target customer ID or Golden Customer ID"
    )
    churn_score: Mapped[float] = mapped_column(
        Float, nullable=False, doc="PyTorch sentiment churn score (>= 0.85)"
    )
    ltv_tier: Mapped[str] = mapped_column(
        String(64), nullable=False, default="VIP_TOP_20_PERCENT", doc="Customer LTV tier ranking"
    )
    action_triggered: Mapped[str] = mapped_column(
        String(128), nullable=False, doc="Retention campaign action dispatched (e.g. Concierge Replacement)"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="DISPATCHED", doc="Delivery status of the campaign action"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
        doc="UTC timestamp of retention campaign trigger",
    )

    def __repr__(self) -> str:
        return (
            f"<RetentionCampaignAudit(audit_id={self.audit_id}, ticket_id='{self.ticket_id}', "
            f"user_id='{self.user_id}', churn_score={self.churn_score}, action='{self.action_triggered}')>"
        )

