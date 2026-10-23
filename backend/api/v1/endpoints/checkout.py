"""
Checkout and Optimistic Concurrency Control (OCC) Endpoints.

Handles retail order placement with high throughput, predictive safety buffer failsafe,
and true two-phase inventory reservation lifecycle:
1. Available stock calculation: available_stock = physical_stock - reserved_stock
2. Active hold creation (InventoryReservation: PENDING, expires_at = now + 5 min)
3. Atomic reservation increment (reserved_stock += quantity)
4. Payment simulation & atomic completion (physical_stock -= quantity, reserved_stock -= quantity, status='COMPLETED')
5. Strict transaction management (db.begin_nested() savepoint with rollback on error)
6. Automatic Redis cart cleanup and precomputed return risk scoring.
"""

import uuid
import logging
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from backend.db.session import get_db
from backend.models.inventory import Inventory, InventoryReservation
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
    summary="Process Transactional Checkout (True Reservation Lifecycle & OCC Protected)",
    description=(
        "Executes transactional checkout against PostgreSQL with two-phase inventory reservation, "
        "enforcing the invariant: Available = Physical - Reserved. Automatically removes session from Redis cart."
    ),
)
def process_checkout(
    request: CheckoutRequest,
    db: Session = Depends(get_db),
    cart_tracker: CartVelocityTracker = Depends(get_cart_tracker),
):
    """
    Checkout Transaction Handler with True Reservation Lifecycle.

    Workflow:
    1. Fetches current Inventory row for item_id under row lock / nested transaction.
    2. Enforces OCC version checking if expected_version is provided.
    3. The Math: available_stock = physical_stock - reserved_stock.
    4. Guard: Checks predictive surge buffer and verifies quantity <= available_stock.
       Raises HTTP 409 Conflict if stock is insufficient or surge hold is triggered.
    5. The Flow:
       - Atomically creates InventoryReservation (expires_at = datetime.utcnow() + 5m, status='PENDING').
       - Atomically increments inventory.reserved_stock += quantity.
       - Simulates payment success.
       - Atomically decrements inventory.physical_stock -= quantity, decrements inventory.reserved_stock -= quantity,
         increments inventory.version += 1, and marks reservation as 'COMPLETED'.
       - Records transaction in SalesLedger.
    6. Cart Cleanup: Calls cart_tracker.remove_from_cart(item_id, user_session_id).
    7. Returns finalized order confirmation with remaining stock and risk level.
    """
    logger.info(
        f"Processing checkout: item={request.item_id}, qty={request.quantity}, session={request.user_session_id}"
    )

    try:
        # Strict transaction management using a nested savepoint
        with db.begin_nested():
            # 1. Fetch current Inventory record (with row lock if supported)
            item = (
                db.query(Inventory)
                .filter(Inventory.item_id == request.item_id)
                .with_for_update()
                .first()
            )
            if not item:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Item '{request.item_id}' not found in product catalog.",
                )

            # 2. OCC Version Conflict Guard
            if (
                request.expected_version is not None
                and request.expected_version != item.version
            ):
                logger.warning(
                    f"OCC conflict on {request.item_id}: expected version {request.expected_version}, actual {item.version}."
                )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Concurrent transaction conflict. Another transaction updated this inventory version. Please retry.",
                )

            # 3. The Math: Available = Physical - Reserved
            reserved = item.reserved_stock if item.reserved_stock is not None else 0
            available_stock = item.physical_stock - reserved

            # Predictive safety buffer surge check:
            # Rejects if quantity exceeds stock net of virtual safety buffer
            if item.virtual_safety_buffer and request.quantity > (
                available_stock - item.virtual_safety_buffer
            ):
                logger.warning(
                    f"Predictive safety failsafe triggered for {request.item_id}: "
                    f"requested={request.quantity}, physical={item.physical_stock}, reserved={reserved}, "
                    f"buffer={item.virtual_safety_buffer}, available={available_stock}"
                )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Item is in extremely high demand and currently reserved. Please try again in a few minutes.",
                )

            # 4. Availability Check: Check if quantity > available_stock. If true, raise 409 Conflict.
            if request.quantity > available_stock:
                logger.warning(
                    f"Insufficient stock for {request.item_id}: requested={request.quantity}, "
                    f"available={available_stock} (physical={item.physical_stock}, reserved={reserved})"
                )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Insufficient available stock for item '{item.item_id}'. Available: {available_stock}, Requested: {request.quantity}.",
                )

            # 5. Atomically create an InventoryReservation with expires_at = datetime.utcnow() + timedelta(minutes=5)
            now_utc = datetime.utcnow()
            expires_at = now_utc + timedelta(minutes=5)
            reservation_id = str(uuid.uuid4())

            reservation = InventoryReservation(
                id=reservation_id,
                item_id=item.item_id,
                user_session_id=request.user_session_id,
                quantity=request.quantity,
                expires_at=expires_at,
                status="PENDING",
            )
            db.add(reservation)

            # Atomically increment inventory.reserved_stock += quantity
            item.reserved_stock = reserved + request.quantity
            db.flush()

            # *Simulate payment success*
            payment_successful = True
            if not payment_successful:
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail="Payment simulation failed.",
                )

            # Atomically decrement inventory.physical_stock -= quantity,
            # decrement inventory.reserved_stock -= quantity,
            # and mark the reservation as 'COMPLETED'
            item.physical_stock -= request.quantity
            item.reserved_stock -= request.quantity
            item.version += 1
            reservation.status = "COMPLETED"

            # 6. Audit: Record transaction in SalesLedger
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

        # Commit outer transaction
        db.commit()
        db.refresh(item)

    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        logger.error(
            f"Error processing checkout for item '{request.item_id}': {exc}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Checkout processing failed.",
        )

    # 7. Cart Cleanup in Redis (best-effort, outside DB transaction)
    try:
        cart_tracker.remove_from_cart(request.item_id, request.user_session_id)
        logger.debug(
            f"Removed session {request.user_session_id} from cart:{request.item_id}"
        )
    except Exception as e:
        logger.warning(
            f"Could not remove session from Redis cart (broker offline or testing mode): {e}"
        )

    # 8. Pre-computed return risk level lookup (0ms warehouse latency PK read)
    user_risk_level = get_return_risk_level(db, effective_user_id)

    item_result = CheckoutItemResult(
        item_id=item.item_id,
        quantity=request.quantity,
        success=True,
        new_version=item.version,
        remaining_stock=item.physical_stock,
    )

    return CheckoutResponse(
        order_id=order_id,
        status="SUCCESS",
        message="Order successfully placed with true reservation lifecycle and stock integrity.",
        item_id=item.item_id,
        quantity=request.quantity,
        new_version=item.version,
        remaining_stock=item.physical_stock,
        items=[item_result],
        precomputed_return_risk_level=user_risk_level,
    )
