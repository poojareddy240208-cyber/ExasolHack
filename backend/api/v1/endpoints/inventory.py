"""
Inventory read endpoints (Predictive Inventory dashboard).

Reads the OCC ledger from PostgreSQL and the live 5-minute cart velocity from Redis. The virtual safety buffer is
synced from Exasol by Celery Beat; nothing here runs a model or an analytics query.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.db.session import get_db
from backend.models.inventory import Inventory
from backend.schemas.dashboard import InventoryItemView, InventoryListResponse
from backend.services.cart_tracker import CartVelocityTracker, get_cart_tracker
from backend.services.dashboard_db import UNAVAILABLE_DETAIL

logger = logging.getLogger("omnisync.inventory")

router = APIRouter()


def _carts_or_none(tracker: CartVelocityTracker, item_id: str) -> Optional[int]:
    """Live cart count, or None when Redis is unavailable (the page can still show stock and buffers)."""
    try:
        return int(tracker.get_cart_velocity(item_id))
    except Exception as exc:
        logger.warning("Cart velocity unavailable for %s: %s", item_id, exc)
        return None


def _to_view(row, active_carts: Optional[int]) -> InventoryItemView:
    estimate = None
    if row.virtual_safety_buffer > 0 and active_carts is not None:
        estimate = max(0, row.virtual_safety_buffer + row.stock - active_carts)
    return InventoryItemView(
        item_id=row.item_id,
        name=row.name,
        stock=row.stock,
        version=row.version,
        virtual_safety_buffer=row.virtual_safety_buffer,
        available_stock=max(0, row.stock - row.virtual_safety_buffer),
        active_carts_5m=active_carts,
        predicted_demand_15m_estimate=estimate,
    )


# Only the needed columns are selected: loading whole Inventory rows would also load every sales_ledger row of the SKU.
_COLUMNS = (Inventory.item_id, Inventory.name, Inventory.stock, Inventory.version, Inventory.virtual_safety_buffer)


@router.get(
    "",
    response_model=InventoryListResponse,
    summary="List inventory with safety buffers and live cart velocity",
)
def list_inventory(db: Session = Depends(get_db), tracker: CartVelocityTracker = Depends(get_cart_tracker)):
    try:
        rows = db.query(*_COLUMNS).order_by(Inventory.item_id).all()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.warning("Inventory query failed: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=UNAVAILABLE_DETAIL)

    carts = [_carts_or_none(tracker, row.item_id) for row in rows]
    redis_ok = all(count is not None for count in carts)
    return InventoryListResponse(
        generated_at=datetime.now(timezone.utc),
        total_items=len(rows),
        total_active_carts_5m=sum(carts) if rows and redis_ok else (0 if not rows else None),
        redis_available=redis_ok,
        items=[_to_view(row, count) for row, count in zip(rows, carts)],
    )


@router.get(
    "/{item_id}",
    response_model=InventoryItemView,
    summary="One inventory item",
    responses={404: {"description": "Item not in the catalog"}},
)
def get_inventory_item(
    item_id: str, db: Session = Depends(get_db), tracker: CartVelocityTracker = Depends(get_cart_tracker)
):
    try:
        row = db.query(*_COLUMNS).filter(Inventory.item_id == item_id).first()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.warning("Inventory query failed: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=UNAVAILABLE_DETAIL)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Item '{item_id}' not found in product catalog.")
    return _to_view(row, _carts_or_none(tracker, item_id))
