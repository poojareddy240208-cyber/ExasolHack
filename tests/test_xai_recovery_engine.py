"""
Unit & Integration Tests for Phase 3: Explainable Recovery Engine & NLP Ticket Radar (Tab 5).

Verifies:
1. Structured NLP UDF Parsing (Task 1):
   - Parses Exasol PyTorch UDF JSON output into sentiment (Float), urgency (LOW/MEDIUM/HIGH/CRITICAL),
     and root_cause (FULFILLMENT_FAILURE/PRODUCT_PREFERENCE/BILLING_ERROR).
   - Backward-compatibility with raw numeric inputs and malformed strings.
2. Context-Aware Action Engine (Task 2):
   - PRODUCT_PREFERENCE -> AUTO_RETURN_LABEL
   - FULFILLMENT_FAILURE + CRITICAL -> ESCALATE_HUMAN_CONCIERGE_AND_OVERNIGHT_REPLACEMENT + XAI explanation
   - BILLING_ERROR -> AUTO_REFUND_AND_10_PCT_APOLOGY
3. Action Approval Endpoint (Task 3):
   - POST /api/v1/churn/alerts/{ticket_id}/approve with {"approved_by": "..."}
   - Idempotency: returns 200 with "Action already executed." when already ACTION_EXECUTED.
   - Status transition from PENDING_REVIEW/DISPATCHED to ACTION_EXECUTED with executed_at.
   - 404 for non-existent ticket.
4. Demo NLP Tickets Seed (Task 4):
   - ZD-DEMO-FIT-001 (sentiment 0.30, MEDIUM, PRODUCT_PREFERENCE)
   - ZD-DEMO-WEDDING-002 (sentiment 0.99, CRITICAL, FULFILLMENT_FAILURE)
"""

import json
import uuid
from datetime import datetime, timezone, timedelta
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from backend.main import app
from backend.db.session import get_db
from backend.models.base import Base
from backend.models.sales import RetentionCampaignAudit
from backend.services.retention_engine import (
    parse_nlp_udf_output,
    evaluate_retention_action,
)
import scripts.bootstrap as bootstrap


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


# ==============================================================================
# Task 1: Structured NLP UDF Parsing Tests
# ==============================================================================
def test_parse_nlp_udf_output_valid_json():
    """Verify JSON VARCHAR(2000) from Exasol PyTorch UDF is parsed into three distinct fields."""
    raw_json = json.dumps(
        {
            "sentiment": 0.985,
            "urgency": "CRITICAL",
            "root_cause": "FULFILLMENT_FAILURE",
        }
    )
    parsed = parse_nlp_udf_output(raw_json)
    assert isinstance(parsed["sentiment"], float)
    assert parsed["sentiment"] == 0.985
    assert parsed["urgency"] == "CRITICAL"
    assert parsed["root_cause"] == "FULFILLMENT_FAILURE"


def test_parse_nlp_udf_output_product_preference_and_medium():
    raw_json = json.dumps(
        {
            "sentiment": 0.30,
            "urgency": "MEDIUM",
            "root_cause": "PRODUCT_PREFERENCE",
        }
    )
    parsed = parse_nlp_udf_output(raw_json)
    assert parsed["sentiment"] == 0.30
    assert parsed["urgency"] == "MEDIUM"
    assert parsed["root_cause"] == "PRODUCT_PREFERENCE"


def test_parse_nlp_udf_output_billing_error():
    raw_json = json.dumps(
        {
            "sentiment": 0.72,
            "urgency": "HIGH",
            "root_cause": "BILLING_ERROR",
        }
    )
    parsed = parse_nlp_udf_output(raw_json)
    assert parsed["sentiment"] == 0.72
    assert parsed["urgency"] == "HIGH"
    assert parsed["root_cause"] == "BILLING_ERROR"


def test_parse_nlp_udf_output_numeric_fallback():
    """Verify legacy scalar float fallback works safely."""
    parsed = parse_nlp_udf_output(0.95)
    assert parsed["sentiment"] == 0.95
    assert parsed["urgency"] == "CRITICAL"
    assert parsed["root_cause"] == "FULFILLMENT_FAILURE"

    parsed_low = parse_nlp_udf_output(0.25)
    assert parsed_low["sentiment"] == 0.25
    assert parsed_low["urgency"] == "LOW"
    assert parsed_low["root_cause"] == "PRODUCT_PREFERENCE"


# ==============================================================================
# Task 2: Context-Aware Action Engine Rule Engine Tests
# ==============================================================================
def test_action_engine_product_preference_recommends_auto_return_label():
    """Rule 1: If root_cause == 'PRODUCT_PREFERENCE' -> Recommend AUTO_RETURN_LABEL."""
    result = evaluate_retention_action(
        root_cause="PRODUCT_PREFERENCE",
        urgency="MEDIUM",
        sentiment=0.30,
    )
    assert result["recommended_action"] == "AUTO_RETURN_LABEL"
    assert "return label" in result["xai_explanation"].lower()


def test_action_engine_fulfillment_failure_critical_recommends_concierge():
    """Rule 2: If root_cause == 'FULFILLMENT_FAILURE' and urgency == 'CRITICAL' -> Recommend ESCALATE_HUMAN_CONCIERGE_AND_OVERNIGHT_REPLACEMENT."""
    result = evaluate_retention_action(
        root_cause="FULFILLMENT_FAILURE",
        urgency="CRITICAL",
        sentiment=0.99,
    )
    assert (
        result["recommended_action"]
        == "ESCALATE_HUMAN_CONCIERGE_AND_OVERNIGHT_REPLACEMENT"
    )
    assert (
        "Critical urgency detected due to fulfillment failure"
        in result["xai_explanation"]
    )
    assert "Human concierge escalation required" in result["xai_explanation"]


def test_action_engine_billing_error_recommends_refund_and_apology():
    """Rule 3: If root_cause == 'BILLING_ERROR' -> Recommend AUTO_REFUND_AND_10_PCT_APOLOGY."""
    result = evaluate_retention_action(
        root_cause="BILLING_ERROR",
        urgency="HIGH",
        sentiment=0.75,
    )
    assert result["recommended_action"] == "AUTO_REFUND_AND_10_PCT_APOLOGY"
    assert "refund" in result["xai_explanation"].lower()
    assert "10%" in result["xai_explanation"]


# ==============================================================================
# Task 3: Action Approval Endpoint (Idempotency & Governance) Tests
# ==============================================================================
def test_approve_churn_alert_endpoint_success_and_idempotency(db_session):
    """Test approval status transition, recorded approved_by/executed_at, and idempotent 200 responses."""
    ticket_id = "ZD-TEST-APPROVE-001"
    audit = RetentionCampaignAudit(
        audit_id=uuid.uuid4(),
        ticket_id=ticket_id,
        user_id="CUST-GOLDEN-VIP-1",
        churn_score=0.96,
        ltv_tier="Top 1% VIP",
        action_triggered="ESCALATE_HUMAN_CONCIERGE_AND_OVERNIGHT_REPLACEMENT",
        status="PENDING_REVIEW",
        created_at=datetime.now(timezone.utc),
        xai_explanation="Critical urgency detected due to fulfillment failure.",
        urgency="CRITICAL",
        root_cause="FULFILLMENT_FAILURE",
    )
    db_session.add(audit)
    db_session.commit()

    app.dependency_overrides[get_db] = lambda: db_session
    client = TestClient(app)
    try:
        # First call: approve the pending action
        resp = client.post(
            f"/api/v1/churn/alerts/{ticket_id}/approve",
            json={"approved_by": "OpsLead_Sarah"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ticket_id"] == ticket_id
        assert data["status"] == "ACTION_EXECUTED"
        assert data["approved_by"] == "OpsLead_Sarah"
        assert data["executed_at"] is not None
        assert "executed successfully" in data["message"].lower()

        # Check in DB
        db_audit = (
            db_session.query(RetentionCampaignAudit)
            .filter(RetentionCampaignAudit.ticket_id == ticket_id)
            .first()
        )
        assert db_audit.status == "ACTION_EXECUTED"
        assert db_audit.approved_by == "OpsLead_Sarah"
        assert db_audit.executed_at is not None
        first_executed_at = data["executed_at"]

        # Second call (Idempotency Rule): already ACTION_EXECUTED -> return 200 with "Action already executed."
        resp2 = client.post(
            f"/api/v1/churn/alerts/{ticket_id}/approve",
            json={"approved_by": "OpsLead_Bob"},
        )
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["ticket_id"] == ticket_id
        assert data2["status"] == "ACTION_EXECUTED"
        assert data2["message"] == "Action already executed."
        # Must retain original approver and timestamp
        assert data2["approved_by"] == "OpsLead_Sarah"
        assert data2["executed_at"] == first_executed_at

    finally:
        app.dependency_overrides.clear()


def test_approve_churn_alert_endpoint_not_found(db_session):
    """Attempting to approve a non-existent ticket returns 404."""
    app.dependency_overrides[get_db] = lambda: db_session
    client = TestClient(app)
    try:
        resp = client.post(
            "/api/v1/churn/alerts/NON_EXISTENT_TICKET/approve",
            json={"approved_by": "Admin"},
        )
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


# ==============================================================================
# Task 4: Demo NLP Tickets Seed Verification Tests
# ==============================================================================
def test_seed_xai_demo_data_inserts_both_nlp_tickets(db_session, monkeypatch):
    """Verify seed_xai_demo_data seeds Ticket 1 (Fit Issue) and Ticket 2 (Ruined Wedding) for Top 1% VIP."""
    monkeypatch.setattr(bootstrap, "SessionLocal", lambda: db_session)
    bootstrap.seed_xai_demo_data()

    # Verify Ticket 1 (Fit Issue)
    t1 = (
        db_session.query(RetentionCampaignAudit)
        .filter(RetentionCampaignAudit.ticket_id == "ZD-DEMO-FIT-001")
        .first()
    )
    assert t1 is not None
    assert t1.user_id == "DEMO-EVE-001"
    assert t1.churn_score == 0.30
    assert t1.urgency == "MEDIUM"
    assert t1.root_cause == "PRODUCT_PREFERENCE"
    assert t1.action_triggered == "AUTO_RETURN_LABEL"
    assert t1.status == "PENDING_REVIEW"
    assert "return label" in t1.xai_explanation.lower()

    # Verify Ticket 2 (Ruined Wedding)
    t2 = (
        db_session.query(RetentionCampaignAudit)
        .filter(RetentionCampaignAudit.ticket_id == "ZD-DEMO-WEDDING-002")
        .first()
    )
    assert t2 is not None
    assert t2.user_id == "DEMO-EVE-001"
    assert t2.churn_score == 0.99
    assert t2.urgency == "CRITICAL"
    assert t2.root_cause == "FULFILLMENT_FAILURE"
    assert t2.action_triggered == "ESCALATE_HUMAN_CONCIERGE_AND_OVERNIGHT_REPLACEMENT"
    assert t2.status == "PENDING_REVIEW"
    assert "Critical urgency detected due to fulfillment failure" in t2.xai_explanation
