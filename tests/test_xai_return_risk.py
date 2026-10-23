"""
Tests for Phase 2: Context-Aware Return Intelligence (XAI Business Logic).
Verifies:
- Context-Aware penalty weights (supplier defect=0.0, discretion=1.0, abuse=3.0)
- evaluate_user_return_risk_xai for Customer A (Unlucky VIP, Level 1, 83% defect rationale)
- evaluate_user_return_risk_xai for Customer B (Wardrober, Level 5, abuse rationale, human_review_required=True)
- Staleness warning (>24h vs fresh)
- API endpoint GET /api/v1/return-risk/{user_id} contract
"""

import uuid
from datetime import datetime, timezone, timedelta
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from backend.main import app
from backend.db.session import get_db
from backend.models.base import Base
from backend.models.inventory import Inventory
from backend.models.sales import SalesLedger
from backend.services.return_risk import (
    CONTEXT_PENALTY_WEIGHTS,
    evaluate_user_return_risk_xai,
)


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    session.add(
        Inventory(
            item_id="SKU-DEMO-XAI",
            name="Demo Sneaker",
            stock=100,
            version=1,
            virtual_safety_buffer=0,
        )
    )
    session.commit()
    yield session
    session.close()
    engine.dispose()


def test_context_penalty_math_weights():
    """Verify weightings match specifications exactly."""
    assert CONTEXT_PENALTY_WEIGHTS["DEFECTIVE_BATCH"] == 0.0
    assert CONTEXT_PENALTY_WEIGHTS["STORE_ERROR"] == 0.0
    assert CONTEXT_PENALTY_WEIGHTS["WRONG_ITEM_SHIPPED"] == 0.0
    assert CONTEXT_PENALTY_WEIGHTS["CHANGED_MIND"] == 1.0
    assert CONTEXT_PENALTY_WEIGHTS["WRONG_SIZE"] == 1.0
    assert CONTEXT_PENALTY_WEIGHTS["WARDROBING_SUSPECTED"] == 3.0
    assert CONTEXT_PENALTY_WEIGHTS["SUSPECTED_ABUSE"] == 3.0


def test_customer_a_unlucky_vip_evaluates_to_level_1(db_session):
    """
    Customer A has 6 returns: 5 DEFECTIVE_BATCH (supplier_batch_id='BATCH-8992')
    and 1 WRONG_SIZE. Should evaluate to Risk Level 1 with 83% defect rationale.
    """
    now = datetime.now(timezone.utc)
    for i in range(5):
        db_session.add(
            SalesLedger(
                transaction_id=uuid.uuid4(),
                item_id="SKU-DEMO-XAI",
                user_id="Customer A",
                quantity=1,
                timestamp=now - timedelta(days=10 - i),
                return_reason_code="DEFECTIVE_BATCH",
                supplier_batch_id="BATCH-8992",
            )
        )
    db_session.add(
        SalesLedger(
            transaction_id=uuid.uuid4(),
            item_id="SKU-DEMO-XAI",
            user_id="Customer A",
            quantity=1,
            timestamp=now - timedelta(days=1),
            return_reason_code="WRONG_SIZE",
            supplier_batch_id=None,
        )
    )
    db_session.commit()

    result = evaluate_user_return_risk_xai("Customer A", db_session)
    assert result["risk_level"] == 1
    assert result["human_review_required"] is False
    assert result["total_returns"] == 6
    assert result["defective_count"] == 5
    assert result["abuse_count"] == 0
    assert result["weighted_score"] == 1.0
    assert "83%" in result["xai_explanation"]
    assert "supplier defect" in result["xai_explanation"].lower()


def test_customer_b_wardrober_evaluates_to_level_5(db_session):
    """
    Customer B has 4 returns: all 4 WARDROBING_SUSPECTED.
    Should evaluate to Risk Level 5 with abuse explanation and human_review_required=True.
    """
    now = datetime.now(timezone.utc)
    for i in range(4):
        db_session.add(
            SalesLedger(
                transaction_id=uuid.uuid4(),
                item_id="SKU-DEMO-XAI",
                user_id="Customer B",
                quantity=1,
                timestamp=now - timedelta(days=15 - i * 3),
                return_reason_code="WARDROBING_SUSPECTED",
                supplier_batch_id=None,
            )
        )
    db_session.commit()

    result = evaluate_user_return_risk_xai("Customer B", db_session)
    assert result["risk_level"] == 5
    assert result["human_review_required"] is True
    assert result["total_returns"] == 4
    assert result["abuse_count"] == 4
    assert result["weighted_score"] == 12.0
    assert "100%" in result["xai_explanation"]
    assert "wardrobing" in result["xai_explanation"].lower()
    assert "Human review required" in result["xai_explanation"]


def test_staleness_warning_triggered_after_24_hours(db_session):
    """If user_risk_cache computed_at is older than 24 hours, staleness_warning is True."""
    stale_time = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    db_session.execute(
        text("""
            INSERT INTO user_risk_cache (user_id, risk_level, return_rate, purchases_6m, returns_6m, computed_at, synced_at)
            VALUES ('stale_user', 2, 0.2, 10, 2, :comp_at, :now)
        """),
        {"comp_at": stale_time, "now": datetime.now(timezone.utc).isoformat()},
    )
    db_session.commit()

    result = evaluate_user_return_risk_xai("stale_user", db_session)
    assert result["staleness_warning"] is True

    fresh_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    db_session.execute(
        text("""
            INSERT INTO user_risk_cache (user_id, risk_level, return_rate, purchases_6m, returns_6m, computed_at, synced_at)
            VALUES ('fresh_user', 2, 0.2, 10, 2, :comp_at, :now)
        """),
        {"comp_at": fresh_time, "now": datetime.now(timezone.utc).isoformat()},
    )
    db_session.commit()

    result_fresh = evaluate_user_return_risk_xai("fresh_user", db_session)
    assert result_fresh["staleness_warning"] is False


def test_api_return_risk_endpoint_xai_payload(db_session):
    """Test GET /api/v1/return-risk/{user_id} returns xai_explanation, staleness_warning, human_review_required."""
    now = datetime.now(timezone.utc)
    for i in range(4):
        db_session.add(
            SalesLedger(
                transaction_id=uuid.uuid4(),
                item_id="SKU-DEMO-XAI",
                user_id="Customer B",
                quantity=1,
                timestamp=now - timedelta(days=10 - i),
                return_reason_code="WARDROBING_SUSPECTED",
                supplier_batch_id=None,
            )
        )
    db_session.commit()

    app.dependency_overrides[get_db] = lambda: db_session
    client = TestClient(app)
    try:
        resp = client.get("/api/v1/return-risk/Customer B")
        assert resp.status_code == 200
        body = resp.json()
        assert body["user_id"] == "Customer B"
        assert body["risk_level"] == 5
        assert body["human_review_required"] is True
        assert "xai_explanation" in body and body["xai_explanation"] is not None
        assert "wardrobing" in body["xai_explanation"].lower()
        assert body["staleness_warning"] is False
        assert body["policy"]["cash_on_delivery_allowed"] is False
        assert body["policy"]["human_review_required"] is True
    finally:
        app.dependency_overrides.clear()
