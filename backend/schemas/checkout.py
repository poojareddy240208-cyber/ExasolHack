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
    expected_version: int = Field(
        ..., ge=0, description="Version stamp read from catalog for Optimistic Concurrency Control (OCC)"
    )


class CheckoutRequest(BaseModel):
    """
    Checkout request payload.
    Used for 0ms-added-latency checkout with OCC failsafe validation.
    """

    model_config = ConfigDict(from_attributes=True)

    user_id: str = Field(..., description="Customer ID or anonymous guest session ID")
    items: List[CartItem] = Field(..., min_length=1, description="List of items to purchase")
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
    If OCC fails (version mismatch or stock shortage), returns detailed conflict diagnostics.
    """

    order_id: Optional[str] = None
    status: str = Field(..., description="'SUCCESS', 'OCC_CONFLICT', or 'INSUFFICIENT_STOCK'")
    message: str
    items: List[CheckoutItemResult] = []
    precomputed_return_risk_level: int = Field(
        1, ge=1, le=5, description="Pre-computed return risk level (1-5) cached from Postgres (0ms user latency)"
    )
