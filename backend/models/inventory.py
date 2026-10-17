"""
Inventory Model - Transactional OLTP (PostgreSQL).

Defines the physical stock ledger and concurrency versioning for retail products.
Crucial Architectural Guard:
- `CheckConstraint("stock >= 0", name="check_stock_non_negative")`: Prevents negative physical inventory.
- `version`: Monotonically incremented integer used for Optimistic Concurrency Control (OCC):
    UPDATE inventory
    SET stock = stock - :qty, version = version + 1
    WHERE item_id = :id AND version = :read_version AND stock >= :qty;
- `virtual_safety_buffer`: Updated periodically from Exasol via Celery Beat JDBC export:
    Buffer = MAX(0, Predicted_Demand_15m - Current_Physical_Stock + Active_Carts_5m)
"""

from typing import List, TYPE_CHECKING
from sqlalchemy import Column, String, Integer, CheckConstraint
from sqlalchemy.orm import relationship, Mapped, mapped_column

from backend.models.base import Base

if TYPE_CHECKING:
    from backend.models.sales import SalesLedger


class Inventory(Base):
    """
    Physical product catalog and inventory ledger table.
    Enforces OCC version tracking and non-negative stock constraints.
    """

    __tablename__ = "inventory"

    item_id: Mapped[str] = mapped_column(String(64), primary_key=True, index=True, doc="Unique SKU identifier")
    name: Mapped[str] = mapped_column(String(255), nullable=False, doc="Product display name")
    stock: Mapped[int] = mapped_column(Integer, nullable=False, default=0, doc="Current physical stock available")
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, doc="Optimistic Concurrency Control (OCC) version stamp"
    )
    virtual_safety_buffer: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        doc="Virtual safety stock buffer synced from Exasol LightGBM surge predictions",
    )

    # Relationships
    sales: Mapped[List["SalesLedger"]] = relationship(
        "SalesLedger", back_populates="inventory_item", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (CheckConstraint("stock >= 0", name="check_stock_non_negative"),)

    def __repr__(self) -> str:
        return (
            f"<Inventory(item_id='{self.item_id}', name='{self.name}', "
            f"stock={self.stock}, version={self.version}, buffer={self.virtual_safety_buffer})>"
        )
