"""
OmniSync Inventory Management Service.

Provides inventory reservation lifecycle management and automated reaper routines:
- Scans and releases expired pending reservations.
- Restores active held stock back into available inventory.
- Enforces relational invariants: Available = Physical - Reserved.
"""

import logging
from datetime import datetime
from typing import List
from sqlalchemy.orm import Session

from backend.models.inventory import Inventory, InventoryReservation

logger = logging.getLogger("omnisync.inventory_manager")


def release_expired_reservations(db: Session) -> int:
    """
    Finds all InventoryReservation records where status == 'PENDING' and expires_at < datetime.utcnow().
    For each, marks status as 'EXPIRED' and decrements inventory.reserved_stock -= quantity.

    Args:
        db: SQLAlchemy database Session.

    Returns:
        The total count of expired reservations released.
    """
    now = datetime.utcnow()
    logger.info(f"Running release_expired_reservations reaper at {now.isoformat()}")

    try:
        with db.begin_nested():
            expired_reservations: List[InventoryReservation] = (
                db.query(InventoryReservation)
                .filter(
                    InventoryReservation.status == "PENDING",
                    InventoryReservation.expires_at < now,
                )
                .all()
            )

            released_count = 0
            for reservation in expired_reservations:
                reservation.status = "EXPIRED"
                item = (
                    db.query(Inventory)
                    .filter(Inventory.item_id == reservation.item_id)
                    .first()
                )
                if item:
                    current_reserved = (
                        item.reserved_stock if item.reserved_stock is not None else 0
                    )
                    item.reserved_stock = max(
                        0, current_reserved - reservation.quantity
                    )
                    logger.debug(
                        f"Released reservation {reservation.id} for item {item.item_id}: "
                        f"restored {reservation.quantity} units (new reserved_stock={item.reserved_stock})"
                    )
                released_count += 1

        db.commit()
        if released_count > 0:
            logger.info(
                f"Reaper released {released_count} expired inventory reservations."
            )
        return released_count
    except Exception as exc:
        db.rollback()
        logger.error(
            f"Error executing release_expired_reservations: {exc}", exc_info=True
        )
        raise
