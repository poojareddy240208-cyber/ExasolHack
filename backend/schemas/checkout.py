"""
Pydantic v2 Schemas for Checkout and Concurrency Control.
Enforces validation for transactional requests routed to PostgreSQL OLTP.
"""

from typing import List, Optional
from pydantic import BaseModel, Field, ConfigDict


class CartItem(BaseModel):
    """Individual item in checkout payload."""

    model_config = ConfigDict(from_attributes=True)

    item_id: str = Field(..., description="Unique SKU or product UUID in PostgreSQL catalog")
    quantity: int = Field(..., gt=0, description="Quantity requested, must be positive integer")
    expected_version: Optional[int] = Field(
        None, ge=0, description="Version stamp read from catalog for Optimistic Concurrency Control (OCC)"
    )


class CheckoutRequest(BaseModel):
    """
    Checkout request payload.
    Directs transactional checkout with Optimistic Concurrency Control (OCC) and predictive safety stock checks.
    """

    model_config = ConfigDict(from_attributes=True)

    item_id: str = Field(..., description="Unique SKU or product UUID in PostgreSQL catalog")
    quantity: int = Field(..., gt=0, description="Quantity requested, must be positive integer")
    user_session_id: str = Field(
        ..., description="User session identifier used for Redis cart velocity tracking and cleanup"
    )
    user_id: Optional[str] = Field(
        None, description="Optional authenticated customer ID (defaults to user_session_id if omitted)"
    )
    expected_version: Optional[int] = Field(
        None, ge=0, description="Optional OCC version stamp. If omitted, uses the currently read database version"
    )
    applied_promo_code: Optional[str] = Field(
        None, description="Optional promo code (used as a probabilistic feature, not deterministic match)"
    )
    payment_method: str = Field("credit_card", description="Selected payment method (e.g., credit_card, cod)")


class CheckoutItemResult(BaseModel):
    """Outcome for a single item in the transaction."""

    item_id: str
    quantity: int
    success: bool
    new_version: Optional[int] = None
    remaining_stock: Optional[int] = None
    error_message: Optional[str] = None


class CheckoutResponse(BaseModel):
    """
    Outcome of the transactional checkout.
    Returns transaction metadata and pre-computed risk levels with 0ms added user latency.
    """

    order_id: Optional[str] = None
    status: str = Field(..., description="'SUCCESS', 'OCC_CONFLICT', or 'RESERVED_SAFETY_BUFFER'")
    message: str
    item_id: Optional[str] = None
    quantity: Optional[int] = None
    new_version: Optional[int] = None
    remaining_stock: Optional[int] = None
    items: List[CheckoutItemResult] = []
    precomputed_return_risk_level: int = Field(
        1, ge=1, le=5, description="Pre-computed return risk level (1-5) cached from Postgres (0ms user latency)"
    )
