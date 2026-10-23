"""
Inventory Model - Transactional OLTP (PostgreSQL).

Defines the physical stock ledger, reservation holds, and concurrency versioning for retail products.
Crucial Architectural Guards:
- `CheckConstraint("stock >= 0", name="check_stock_non_negative")`: Prevents negative physical inventory.
- Database Invariant: Available Stock = Physical Stock - Reserved Stock
- `version`: Monotonically incremented integer used for Optimistic Concurrency Control (OCC).
- `virtual_safety_buffer`: Updated periodically from Exasol via Celery Beat JDBC export:
    Buffer = MAX(0, Predicted_Demand_15m - Current_Physical_Stock + Active_Carts_5m)
"""

import uuid
from datetime import datetime, timezone
from typing import List, Optional, TYPE_CHECKING
from sqlalchemy import String, Integer, DateTime, ForeignKey, CheckConstraint
from sqlalchemy.orm import relationship, Mapped, mapped_column

from backend.models.base import Base

if TYPE_CHECKING:
    from backend.models.sales import SalesLedger


class Inventory(Base):
    """
    Physical product catalog and inventory ledger table.
    Enforces OCC version tracking, non-negative stock constraints, and reservation balances.
    """

    __tablename__ = "inventory"

    item_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, index=True, doc="Unique SKU identifier"
    )
    name: Mapped[str] = mapped_column(
        String(255), nullable=False, doc="Product display name"
    )
    stock: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, doc="Current physical stock available"
    )
    reserved_stock: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        doc="Current stock held under active reservation holds",
    )
    forecasted_depletion_timestamp: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, doc="Predicted stockout timestamp"
    )
    forecast_valid_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="Forecast horizon validity expiration",
    )
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        doc="Optimistic Concurrency Control (OCC) version stamp",
    )
    virtual_safety_buffer: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        doc="Virtual safety stock buffer synced from Exasol LightGBM surge predictions",
    )

    # Relationships
    sales: Mapped[List["SalesLedger"]] = relationship(
        "SalesLedger",
        back_populates="inventory_item",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    reservations: Mapped[List["InventoryReservation"]] = relationship(
        "InventoryReservation",
        back_populates="inventory_item",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    __table_args__ = (
        CheckConstraint("stock >= 0", name="check_stock_non_negative"),
        CheckConstraint(
            "reserved_stock >= 0", name="check_reserved_stock_non_negative"
        ),
    )

    @property
    def physical_stock(self) -> int:
        """Alias for physical stock ledger count."""
        return self.stock

    @physical_stock.setter
    def physical_stock(self, value: int) -> None:
        self.stock = value

    @property
    def available_stock(self) -> int:
        """Database invariant: Available = Physical - Reserved."""
        return (self.stock or 0) - (self.reserved_stock or 0)

    def __repr__(self) -> str:
        return (
            f"<Inventory(item_id='{self.item_id}', name='{self.name}', "
            f"stock={self.stock}, reserved={self.reserved_stock}, "
            f"available={self.available_stock}, version={self.version}, "
            f"buffer={self.virtual_safety_buffer})>"
        )


class InventoryReservation(Base):
    """
    Active inventory hold reservation model.
    Tracks cart and checkout stock holds with expiration timestamps.
    """

    __tablename__ = "inventory_reservation"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="check_reservation_qty_positive"),
        {"extend_existing": True},
    )

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        doc="Unique reservation hold UUID",
    )
    item_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("inventory.item_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="Foreign Key referencing inventory.item_id",
    )
    user_session_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        index=True,
        doc="Cart / User session holding the reservation",
    )
    quantity: Mapped[int] = mapped_column(
        Integer, nullable=False, doc="Number of units held"
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
        doc="Expiration timestamp for hold",
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="PENDING",
        index=True,
        doc="Hold status: 'PENDING', 'COMPLETED', or 'EXPIRED'",
    )

    # Relationships
    inventory_item: Mapped["Inventory"] = relationship(
        "Inventory", back_populates="reservations"
    )

    def __repr__(self) -> str:
        return (
            f"<InventoryReservation(id='{self.id}', item_id='{self.item_id}', "
            f"user_session_id='{self.user_session_id}', quantity={self.quantity}, "
            f"expires_at='{self.expires_at}', status='{self.status}')>"
        )
