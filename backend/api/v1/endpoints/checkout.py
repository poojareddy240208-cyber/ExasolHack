"""
Checkout and Optimistic Concurrency Control (OCC) Endpoints.

Handles retail order placement with high throughput and 0ms added latency.
Architectural Separation:
- Transactional state lives strictly in PostgreSQL (OLTP).
- Zero Postgres code enters Exasol, and zero Exasol analytic queries block this checkout path.
- Predictive Safety Failsafe:
  available_stock = stock - virtual_safety_buffer
  If quantity > available_stock, immediately aborts with HTTP 409 Conflict.
- Enforces strict Optimistic Concurrency Control (OCC) failsafe:
  UPDATE inventory
  SET stock = stock - :qty, version = version + 1
  WHERE item_id = :id AND version = :read_version AND stock >= :qty;
- Schema constraint: Postgres enforces `CHECK (stock >= 0)`.
- Cart Cleanup: Removes user_session_id from Redis cart ZSET upon successful commit.
- Dynamic Return Friction: Reads pre-computed risk level (1-5) from local cache/Postgres
  (adding 0ms checkout latency).
"""

import uuid
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.db.session import get_db
from backend.models.inventory import Inventory
from backend.models.sales import SalesLedger
from backend.services.cart_tracker import CartVelocityTracker, get_cart_tracker
from backend.services.risk_lookup import get_return_risk_level
from backend.schemas.checkout import (
    CheckoutRequest,
    CheckoutResponse,
    CheckoutItemResult,
)

logger = logging.getLogger("omnisync.checkout")

router = APIRouter()


@router.post(
    "",
    response_model=CheckoutResponse,
    status_code=status.HTTP_200_OK,
    summary="Process Transactional Checkout (OCC Protected & Predictive Failsafe)",
    description=(
        "Executes transactional checkout against PostgreSQL with Optimistic Concurrency Control (OCC) "
        "and Predictive Safety Buffer guards. Automatically removes session from Redis cart on success."
    ),
)
def process_checkout(
    request: CheckoutRequest,
    db: Session = Depends(get_db),
    cart_tracker: CartVelocityTracker = Depends(get_cart_tracker),
):
    """
    Checkout Transaction Handler.

    Workflow:
    1. Fetches current Inventory row for item_id.
    2. Predictive Failsafe: available_stock = stock - virtual_safety_buffer.
       If quantity > available_stock, raises HTTP 409 Conflict.
    3. OCC Transaction:
       `UPDATE inventory SET stock = stock - :qty, version = version + 1
        WHERE item_id = :id AND version = :read_version AND stock >= :qty;`
       If 0 rows affected, raises HTTP 409 Conflict (concurrency collision).
    4. Cart Cleanup: calls cart_tracker.remove_from_cart(item_id, user_session_id).
    5. Audit: records checkout into SalesLedger and commits transaction.
    """
    logger.info(
        f"Processing checkout: item={request.item_id}, qty={request.quantity}, session={request.user_session_id}"
    )

    # 1. Fetch current Inventory record
    item = db.query(Inventory).filter(Inventory.item_id == request.item_id).first()
    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Item '{request.item_id}' not found in product catalog.",
        )

    # 2. Predictive Failsafe Check
    # Available stock incorporates the Virtual Safety Buffer synced from Exasol LightGBM surge predictions
    available_stock = item.stock - item.virtual_safety_buffer
    if request.quantity > available_stock:
        logger.warning(
            f"Predictive safety failsafe triggered for {request.item_id}: "
            f"requested={request.quantity}, physical={item.stock}, buffer={item.virtual_safety_buffer}, available={available_stock}"
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Item is in extremely high demand and currently reserved. Please try again in a few minutes.",
        )

    # 3. Optimistic Concurrency Control (OCC) Execution
    read_version = request.expected_version if request.expected_version is not None else item.version

    occ_query = text(
        "UPDATE inventory "
        "SET stock = stock - :qty, version = version + 1 "
        "WHERE item_id = :id AND version = :read_version AND stock >= :qty"
    )

    result = db.execute(
        occ_query,
        {
            "qty": request.quantity,
            "id": item.item_id,
            "read_version": read_version,
        },
    )

    # If rowcount == 0, either version changed concurrently or stock was exhausted
    if result.rowcount == 0:
        db.rollback()
        logger.warning(
            f"OCC conflict on {request.item_id}: version {read_version} no longer valid or insufficient physical stock."
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Concurrent transaction conflict. Another transaction updated this inventory version. Please retry.",
        )

    # 4. Audit: Record transaction in SalesLedger
    order_uuid = uuid.uuid4()
    order_id = f"ORD-{order_uuid.hex[:8].upper()}"
    effective_user_id = request.user_id or request.user_session_id

    sales_record = SalesLedger(
        transaction_id=order_uuid,
        item_id=item.item_id,
        user_id=effective_user_id,
        quantity=request.quantity,
        timestamp=datetime.now(timezone.utc),
    )
    db.add(sales_record)
    db.commit()
    db.refresh(item)

    # 5. Cart Cleanup in Redis
    try:
        cart_tracker.remove_from_cart(request.item_id, request.user_session_id)
        logger.debug(f"Removed session {request.user_session_id} from cart:{request.item_id}")
    except Exception as e:
        logger.warning(f"Could not remove session from Redis cart (broker offline or testing mode): {e}")

    # Pre-computed return risk level: one primary-key read of the `user_risk_cache` table that Celery Beat
    # keeps in sync from Exasol (Level 1 <10% returns ... Level 5 >60%). No analytics query runs here.
    user_risk_level = get_return_risk_level(db, effective_user_id)

    new_version = item.version
    remaining_stock = item.stock

    item_result = CheckoutItemResult(
        item_id=item.item_id,
        quantity=request.quantity,
        success=True,
        new_version=new_version,
        remaining_stock=remaining_stock,
    )

    return CheckoutResponse(
        order_id=order_id,
        status="SUCCESS",
        message="Order successfully placed with OCC safety and stock integrity.",
        item_id=item.item_id,
        quantity=request.quantity,
        new_version=new_version,
        remaining_stock=remaining_stock,
        items=[item_result],
        precomputed_return_risk_level=user_risk_level,
    )
