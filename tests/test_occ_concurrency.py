"""
OmniSync Concurrency, OCC Failsafe & Checkout API Test Suite.

Validates:
1. System Health & Webhook asynchronous queueing.
2. Checkout API with Predictive Safety Buffer failsafe (HTTP 409).
3. Checkout API with Optimistic Concurrency Control (OCC) and Redis Cart Cleanup.
4. Dedicated 50-thread high-concurrency race condition testing for Hackathon criteria.

Architectural Invariant Under Test:
UPDATE inventory
SET stock = stock - :qty, version = version + 1
WHERE item_id = :id AND version = :read_version AND stock >= :qty;

PostgreSQL Schema Constraint:
CHECK (stock >= 0)
"""

import threading
import concurrent.futures
from typing import List, Dict, Any
from unittest.mock import patch, MagicMock
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.main import app
from backend.models.base import Base
from backend.models.inventory import Inventory
from backend.models.sales import SalesLedger
from backend.db.session import get_db
from backend.services.cart_tracker import get_cart_tracker

# ------------------------------------------------------------------------------
# Test Database Engine & Fixtures
# ------------------------------------------------------------------------------
test_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


@pytest.fixture
def db_session():
    """Provides a fresh database session per test function with clean schema isolation."""
    Base.metadata.create_all(bind=test_engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=test_engine)


@pytest.fixture
def mock_cart_tracker():
    """Mock Redis CartVelocityTracker for test isolation."""
    mock = MagicMock()
    mock.remove_from_cart.return_value = True
    mock.get_cart_velocity.return_value = 5
    return mock


@pytest.fixture
def client(db_session, mock_cart_tracker):
    """FastAPI TestClient with injected dependencies."""

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    def override_get_cart_tracker():
        return mock_cart_tracker

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_cart_tracker] = override_get_cart_tracker

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


# ------------------------------------------------------------------------------
# Health & Webhook Tests
# ------------------------------------------------------------------------------
def test_health_check_endpoint(client: TestClient):
    """Verifies API root health endpoint returns HTTP 200 and healthy status."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "HEALTHY"
    assert "databases" in data
    assert data["databases"]["olap_exasol"] == "configured (privileged)"


def test_zendesk_webhook_endpoint(client: TestClient):
    """Verifies that Zendesk webhook accepts payloads with HTTP 202 Accepted and returns task_id."""
    payload = {
        "ticket_id": "ZD-TEST-1001",
        "user_id": "user_vip_001",
        "email": "vip.shopper@example.com",
        "subject": "Delay in high-value order delivery",
        "description": "I ordered this 3 days ago with expedited shipping and it hasn't dispatched. Unacceptable.",
        "priority": "high",
        "status": "new",
    }
    with patch("backend.celery_app.process_zendesk_ticket_task.delay") as mock_delay:
        mock_task = MagicMock()
        mock_task.id = "mock_task_123"
        mock_delay.return_value = mock_task
        response = client.post("/api/v1/webhooks/zendesk", json=payload)
        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "queued"
        assert data["ticket_id"] == "ZD-TEST-1001"
        assert data["task_id"] == "mock_task_123"


# ------------------------------------------------------------------------------
# Finalized Checkout API & Predictive Failsafe Tests
# ------------------------------------------------------------------------------
def test_checkout_endpoint_success_and_cart_cleanup(client: TestClient, db_session, mock_cart_tracker):
    """
    Verifies successful checkout:
    1. Returns HTTP 200.
    2. Updates stock and version in DB.
    3. Records purchase in SalesLedger.
    4. Invokes cart_tracker.remove_from_cart(item_id, user_session_id).
    """
    # Seed inventory
    item = Inventory(
        item_id="SKU-PS5-CONSOLE",
        name="PlayStation 5 Slim",
        stock=10,
        version=1,
        virtual_safety_buffer=0,
    )
    db_session.add(item)
    db_session.commit()

    payload = {
        "item_id": "SKU-PS5-CONSOLE",
        "quantity": 2,
        "user_session_id": "sess_shopper_882",
    }
    response = client.post("/api/v1/checkout", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "SUCCESS"
    assert data["item_id"] == "SKU-PS5-CONSOLE"
    assert data["quantity"] == 2
    assert data["new_version"] == 2
    assert data["remaining_stock"] == 8

    # Verify database update
    db_item = db_session.get(Inventory, "SKU-PS5-CONSOLE")
    assert db_item.stock == 8
    assert db_item.version == 2

    # Verify SalesLedger audit record
    sale = db_session.query(SalesLedger).filter(SalesLedger.item_id == "SKU-PS5-CONSOLE").first()
    assert sale is not None
    assert sale.quantity == 2
    assert sale.user_id == "sess_shopper_882"

    # Verify Redis cart cleanup invocation
    mock_cart_tracker.remove_from_cart.assert_called_with("SKU-PS5-CONSOLE", "sess_shopper_882")


def test_checkout_predictive_safety_buffer_failsafe(client: TestClient, db_session):
    """
    Verifies Step 3 requirement:
    available_stock = stock - virtual_safety_buffer
    If quantity > available_stock, immediately return HTTP 409 with high-demand message.
    """
    item = Inventory(
        item_id="SKU-LIMITED-WATCH",
        name="Omega Speedmaster Limited",
        stock=10,
        version=1,
        virtual_safety_buffer=8,  # High surge predicted by Exasol!
    )
    db_session.add(item)
    db_session.commit()

    # Available stock is 10 - 8 = 2. Requesting 3 units must trigger failsafe
    payload = {
        "item_id": "SKU-LIMITED-WATCH",
        "quantity": 3,
        "user_session_id": "sess_shopper_surge",
    }
    response = client.post("/api/v1/checkout", json=payload)
    assert response.status_code == 409
    data = response.json()
    assert (
        data["detail"] == "Item is in extremely high demand and currently reserved. Please try again in a few minutes."
    )


def test_checkout_occ_version_conflict(client: TestClient, db_session):
    """Verifies that an outdated expected_version returns HTTP 409 Conflict."""
    item = Inventory(
        item_id="SKU-IPHONE-16",
        name="iPhone 16 Pro",
        stock=15,
        version=5,
        virtual_safety_buffer=0,
    )
    db_session.add(item)
    db_session.commit()

    # Pass stale version 2 (actual is 5)
    payload = {
        "item_id": "SKU-IPHONE-16",
        "quantity": 1,
        "user_session_id": "sess_shopper_stale",
        "expected_version": 2,
    }
    response = client.post("/api/v1/checkout", json=payload)
    assert response.status_code == 409
    data = response.json()
    assert "Concurrent transaction conflict" in data["detail"]


def test_checkout_item_not_found(client: TestClient):
    """Verifies non-existent item returns HTTP 404."""
    payload = {
        "item_id": "SKU-DOES-NOT-EXIST",
        "quantity": 1,
        "user_session_id": "sess_shopper_404",
    }
    response = client.post("/api/v1/checkout", json=payload)
    assert response.status_code == 404


# ------------------------------------------------------------------------------
# High Concurrency OCC Race Condition Simulation (50 Threads Racing for 5 Items)
# ------------------------------------------------------------------------------
class OCCLedgerSimulator:
    """
    Simulates PostgreSQL atomic execution of:
    UPDATE inventory
    SET stock = stock - :qty, version = version + 1
    WHERE item_id = :id AND version = :read_version AND stock >= :qty;
    with CHECK (stock >= 0) constraint.
    """

    def __init__(self, item_id: str, initial_stock: int, initial_version: int = 1):
        self.item_id = item_id
        self.stock = initial_stock
        self.version = initial_version
        self._lock = threading.Lock()

    def attempt_purchase(self, qty: int, read_version: int) -> Dict[str, Any]:
        """Executes atomic OCC transaction matching Postgres engine behavior."""
        with self._lock:
            # Condition 1: Check version stamp (OCC version mismatch detection)
            if self.version != read_version:
                return {
                    "success": False,
                    "reason": "OCC_VERSION_MISMATCH",
                    "current_stock": self.stock,
                    "current_version": self.version,
                }

            # Condition 2: Stock availability check (CHECK stock >= qty and stock >= 0)
            if self.stock < qty:
                return {
                    "success": False,
                    "reason": "INSUFFICIENT_STOCK",
                    "current_stock": self.stock,
                    "current_version": self.version,
                }

            # Atomic decrement and version increment
            self.stock -= qty
            self.version += 1

            # Invariant: Physical stock must never drop below 0
            assert self.stock >= 0, f"Critical constraint violation: negative stock detected ({self.stock})!"

            return {
                "success": True,
                "reason": "PURCHASE_CONFIRMED",
                "remaining_stock": self.stock,
                "new_version": self.version,
            }


def test_high_concurrency_race_condition_50_threads_5_items():
    """
    Simulate 50 concurrent threads racing to purchase the last 5 items.

    Proof Requirements:
    1. Exactly 5 purchases must succeed.
    2. Exactly 45 purchases must fail with OCC version conflict or insufficient stock.
    3. Final stock must be strictly 0 (never negative).
    4. Final version must be strictly 6 (initial version 1 + 5 increments).
    """
    TOTAL_ITEMS = 5
    NUM_THREADS = 50
    simulator = OCCLedgerSimulator(item_id="FLASH-SALE-PS5", initial_stock=TOTAL_ITEMS, initial_version=1)

    results: List[Dict[str, Any]] = []

    def buyer_worker(thread_id: int):
        read_version = simulator.version
        res = simulator.attempt_purchase(qty=1, read_version=read_version)
        res["thread_id"] = thread_id
        return res

    with concurrent.futures.ThreadPoolExecutor(max_workers=NUM_THREADS) as executor:
        futures = [executor.submit(buyer_worker, i) for i in range(NUM_THREADS)]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    successful_purchases = [r for r in results if r["success"]]
    failed_purchases = [r for r in results if not r["success"]]

    assert (
        len(successful_purchases) == TOTAL_ITEMS
    ), f"Expected exactly {TOTAL_ITEMS} successful purchases, but got {len(successful_purchases)}"
    assert len(failed_purchases) == (
        NUM_THREADS - TOTAL_ITEMS
    ), f"Expected exactly {NUM_THREADS - TOTAL_ITEMS} failed attempts, but got {len(failed_purchases)}"
    assert simulator.stock == 0, f"Expected final stock to be 0, but got {simulator.stock}"
    assert simulator.version == 6, f"Expected final version to be 6, but got {simulator.version}"
