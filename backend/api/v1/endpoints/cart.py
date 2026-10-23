"""
Cart velocity endpoints (Redis, rolling 5-minute window).

`POST /cart/add` is what makes cart velocity exist at all: before it, nothing in the API ever added a session to the
Redis sorted set, so the predictive safety buffer had no input. Checkout removes the session again on success.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.db.session import get_db
from backend.models.inventory import Inventory
from backend.schemas.dashboard import CartAddRequest, CartAddResponse, CartVelocityResponse
from backend.services.cart_tracker import CartVelocityTracker, get_cart_tracker
from backend.services.dashboard_db import UNAVAILABLE_DETAIL

logger = logging.getLogger("omnisync.cart")

router = APIRouter()

REDIS_DOWN_DETAIL = "Cart tracking is unavailable: the Redis cache cannot be reached."


@router.post(
    "/add",
    response_model=CartAddResponse,
    status_code=status.HTTP_200_OK,
    summary="Record that a shopper session added an item to their cart",
    responses={404: {"description": "Item not in the catalog"}, 503: {"description": "Redis or database unavailable"}},
)
def add_to_cart(
    request: CartAddRequest, db: Session = Depends(get_db), tracker: CartVelocityTracker = Depends(get_cart_tracker)
):
    try:
        exists = db.query(Inventory.item_id).filter(Inventory.item_id == request.item_id).first() is not None
    except SQLAlchemyError as exc:
        db.rollback()
        logger.warning("Catalog lookup failed: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=UNAVAILABLE_DETAIL)
    if not exists:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Item '{request.item_id}' not found in product catalog.")

    try:
        tracker.add_to_cart(request.item_id, request.user_session_id)
        active = tracker.get_cart_velocity(request.item_id)
    except Exception as exc:
        logger.warning("Redis cart update failed: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=REDIS_DOWN_DETAIL)

    return CartAddResponse(
        item_id=request.item_id,
        user_session_id=request.user_session_id,
        window_seconds=tracker.DEFAULT_WINDOW_SECONDS,
        active_carts_5m=int(active),
    )


@router.get(
    "/velocity/{item_id}",
    response_model=CartVelocityResponse,
    summary="Active carts holding an item in the last 5 minutes",
    responses={503: {"description": "Redis unavailable"}},
)
def get_cart_velocity(item_id: str, tracker: CartVelocityTracker = Depends(get_cart_tracker)):
    try:
        active = tracker.get_cart_velocity(item_id)
    except Exception as exc:
        logger.warning("Redis cart read failed: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=REDIS_DOWN_DETAIL)
    return CartVelocityResponse(
        item_id=item_id, window_seconds=tracker.DEFAULT_WINDOW_SECONDS, active_carts_5m=int(active)
    )
