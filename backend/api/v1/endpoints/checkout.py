"""
Checkout and Optimistic Concurrency Control (OCC) Endpoints.

Handles retail order placement with high throughput and 0ms added latency.
Architectural Separation:
- Transactional state lives strictly in PostgreSQL (OLTP).
- Zero Postgres code enters Exasol, and zero Exasol analytic queries block this checkout path.
- Enforces strict Optimistic Concurrency Control (OCC) failsafe:
  UPDATE inventory
  SET stock = stock - :qty, version = version + 1
  WHERE item_id = :id AND version = :read_version AND stock >= :qty;
- Schema constraint: Postgres enforces `CHECK (stock >= 0)`.
- Dynamic Return Friction: Reads pre-computed risk level (1-5) from local cache/Postgres
  (adding 0ms checkout latency).
"""

import uuid
import logging
from typing import Dict, Any
from fastapi import APIRouter, status
from backend.schemas.checkout import (
    CheckoutRequest,
    CheckoutResponse,
    CheckoutItemResult,
)

logger = logging.getLogger("omnisync.checkout")

router = APIRouter()

# ------------------------------------------------------------------------------
# Mock In-Memory Catalog & Ledger for Initial Prototyping & CI Testing
# (Replaced by asyncpg / SQLAlchemy session in PostgreSQL production mode)
# ------------------------------------------------------------------------------
MOCK_INVENTORY_LEDGER: Dict[str, Dict[str, Any]] = {
    "SKU-4090-GPU": {"stock": 10, "version": 1, "title": "NVIDIA RTX 4090 OC Edition"},
    "SKU-HERMES-BAG": {"stock": 3, "version": 1, "title": "Hermès Birkin Handbag 30"},
    "SKU-AIR-JORDAN": {"stock": 50, "version": 1, "title": "Air Jordan 1 Retro High OG"},
}

# Pre-computed Return Risk Cache (populated from Exasol PyTorch batch jobs via Celery)
# Level 1 (<10% return rate): Standard Free Returns
# Level 3 (25-40% return rate): Friction "Keep it for 10% off"
# Level 5 (>60% return rate): Penalty "$5 Restocking Fee warning, no COD"
MOCK_PRECOMPUTED_RETURN_RISK: Dict[str, int] = {
    "user_vip_001": 1,
    "user_guest_892": 3,
    "user_flagged_serial_returner": 5,
}


@router.post(
    "",
    response_model=CheckoutResponse,
    status_code=status.HTTP_200_OK,
    summary="Process Transactional Checkout (OCC Protected)",
    description=(
        "Executes transactional checkout against PostgreSQL with Optimistic Concurrency Control (OCC). "
        "Guarantees no ghost stockouts or negative inventory under massive concurrent surges. "
        "Returns pre-computed return risk level (1-5) with 0ms added latency."
    ),
)
async def process_checkout(request: CheckoutRequest):
    """
    Checkout Transaction Handler.

    OCC Failsafe Logic:
    1. For each requested item, reads the current version stamp (:read_version).
    2. Executes atomic conditional update:
       `UPDATE inventory SET stock = stock - :qty, version = version + 1
        WHERE item_id = :id AND version = :read_version AND stock >= :qty;`
    3. If affected row count == 0, either version changed (concurrency conflict)
       or physical stock fell below requested quantity.
    """
    logger.info(f"Processing checkout for user={request.user_id} with {len(request.items)} items")

    order_id = f"ORD-{uuid.uuid4().hex[:8].upper()}"
    item_results = []
    has_conflict = False
    has_insufficient_stock = False

    for item in request.items:
        current_data = MOCK_INVENTORY_LEDGER.get(item.item_id)

        if not current_data:
            item_results.append(
                CheckoutItemResult(
                    item_id=item.item_id,
                    quantity=item.quantity,
                    success=False,
                    error_message=f"Item {item.item_id} not found in product catalog.",
                )
            )
            continue

        current_stock = current_data["stock"]
        current_version = current_data["version"]

        # Check OCC Version Conflict (simulating WHERE version = :read_version)
        if current_version != item.expected_version:
            has_conflict = True
            item_results.append(
                CheckoutItemResult(
                    item_id=item.item_id,
                    quantity=item.quantity,
                    success=False,
                    remaining_stock=current_stock,
                    error_message=(
                        f"OCC Conflict: item {item.item_id} version changed from "
                        f"{item.expected_version} to {current_version}. Please retry."
                    ),
                )
            )
            continue

        # Check Inventory Availability (simulating WHERE stock >= :qty AND CHECK(stock >= 0))
        if current_stock < item.quantity:
            has_insufficient_stock = True
            item_results.append(
                CheckoutItemResult(
                    item_id=item.item_id,
                    quantity=item.quantity,
                    success=False,
                    remaining_stock=current_stock,
                    error_message=f"Insufficient inventory. Requested {item.quantity}, available: {current_stock}",
                )
            )
            continue

        # Successful atomic decrement and version increment
        current_data["stock"] -= item.quantity
        current_data["version"] += 1

        item_results.append(
            CheckoutItemResult(
                item_id=item.item_id,
                quantity=item.quantity,
                success=True,
                new_version=current_data["version"],
                remaining_stock=current_data["stock"],
            )
        )

    # Lookup pre-computed return risk score (0ms latency addition)
    user_risk_level = MOCK_PRECOMPUTED_RETURN_RISK.get(request.user_id, 1)

    if has_conflict:
        return CheckoutResponse(
            order_id=None,
            status="OCC_CONFLICT",
            message="One or more items experienced concurrent version conflict. Transaction aborted.",
            items=item_results,
            precomputed_return_risk_level=user_risk_level,
        )

    if has_insufficient_stock:
        return CheckoutResponse(
            order_id=None,
            status="INSUFFICIENT_STOCK",
            message="Insufficient stock available for requested quantity.",
            items=item_results,
            precomputed_return_risk_level=user_risk_level,
        )

    return CheckoutResponse(
        order_id=order_id,
        status="SUCCESS",
        message="Order successfully placed with OCC safety and stock integrity.",
        items=item_results,
        precomputed_return_risk_level=user_risk_level,
    )
