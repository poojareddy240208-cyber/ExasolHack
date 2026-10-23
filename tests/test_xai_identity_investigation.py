"""
Tests for Phase 2: Identity Detective Graph Engine (Task 2 & Task 3).
Verifies:
- generate_identity_investigation for DEMO-EVE-001 (confidence 95%, supporting/counter evidence)
- generate_identity_investigation for unknown id returns None
- API endpoint GET /api/v1/golden-customers/{id}/investigation returns structured JSON response
- 404 response for unknown golden customer
"""

from datetime import datetime, timezone, timedelta
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from backend.main import app
from backend.db.session import get_db
from backend.models.base import Base
from backend.services.identity_stitching import generate_identity_investigation


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()

    # Create identity tables
    session.execute(text("""
        CREATE TABLE IF NOT EXISTS golden_customers (
            golden_customer_id TEXT PRIMARY KEY,
            primary_email TEXT,
            member_count INTEGER NOT NULL,
            total_spend_usd NUMERIC(12, 2) NOT NULL DEFAULT 0,
            first_seen_at TIMESTAMPTZ,
            last_seen_at TIMESTAMPTZ,
            synced_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """))
    session.execute(text("""
        CREATE TABLE IF NOT EXISTS golden_customer_members (
            golden_customer_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            node_type TEXT NOT NULL,
            source_id TEXT NOT NULL,
            confidence NUMERIC(5, 4) NOT NULL,
            synced_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (golden_customer_id, node_id)
        )
    """))
    session.execute(text("""
        CREATE TABLE IF NOT EXISTS identity_evidence (
            source_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            confidence_weight NUMERIC(5, 4) NOT NULL,
            linked_to_golden_id TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """))
    session.execute(text("""
        CREATE TABLE IF NOT EXISTS candidate_links (
            node_a TEXT NOT NULL,
            node_b TEXT NOT NULL,
            model_score NUMERIC(5, 4) NOT NULL,
            human_review_status TEXT NOT NULL DEFAULT 'PENDING',
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """))

    now = datetime.now(timezone.utc)
    eve_id = "DEMO-EVE-001"

    # Seed Golden Customer Eve
    session.execute(
        text("""
            INSERT INTO golden_customers VALUES (:id, 'eve.vip@example.com', 3, 4850.00, :f, :l, :s)
        """),
        {"id": eve_id, "f": now - timedelta(days=90), "l": now, "s": now},
    )
    # Seed Members
    session.execute(
        text("""
            INSERT INTO golden_customer_members VALUES
            (:id, 'SHOPIFY:eve_vip_001', 'SHOPIFY', 'eve_vip_001', 1.0, :s),
            (:id, 'DEVICE:laptop_anon_eve', 'DEVICE', 'laptop_anon_eve', 0.95, :s),
            (:id, 'DEVICE:iphone_anon_eve', 'DEVICE', 'iphone_anon_eve', 0.95, :s)
        """),
        {"id": eve_id, "s": now},
    )
    # Seed Candidate Links
    session.execute(
        text("""
            INSERT INTO candidate_links VALUES
            (:id, 'DEVICE:laptop_anon_eve', 0.95, 'STRONG_CANDIDATE_PENDING_REVIEW', :s),
            (:id, 'DEVICE:iphone_anon_eve', 0.95, 'STRONG_CANDIDATE_PENDING_REVIEW', :s)
        """),
        {"id": eve_id, "s": now},
    )
    # Seed Identity Evidence
    session.execute(
        text("""
            INSERT INTO identity_evidence VALUES
            ('DEVICE:laptop_anon_eve', 'Identical specific SKU search intent', 0.95, :id, :s),
            ('DEVICE:iphone_anon_eve', 'Leaked VIP Promo Code EVES_VIP_2026', 0.95, :id, :s),
            ('DEVICE:laptop_anon_eve', 'Cart Abandon: High-value designer item abandoned in guest session', 0.90, :id, :s),
            ('SHOPIFY:eve_vip_001', 'VIP Customer Profile Match', 1.0, :id, :s),
            ('DEVICE:laptop_anon_eve', 'No authenticated login', -0.05, :id, :s),
            ('DEVICE:iphone_anon_eve', 'Promo code could be shared via public forum', -0.10, :id, :s)
        """),
        {"id": eve_id, "s": now},
    )

    session.commit()
    yield session
    session.close()
    engine.dispose()


def test_generate_identity_investigation_demo_eve(db_session):
    """Verify DEMO-EVE-001 yields 95% confidence with expected supporting/counter evidence."""
    result = generate_identity_investigation("DEMO-EVE-001", db_session)
    assert result is not None
    assert result["confidence_score"] == 95
    assert result["decision_status"] == "STRONG_CANDIDATE_PENDING_REVIEW"

    # Supporting evidence checks
    supporting = result["supporting_evidence"]
    assert any("Identical specific SKU search intent" in s for s in supporting)
    assert any("Leaked VIP Promo Code EVES_VIP_2026" in s for s in supporting)
    assert any("Cart Abandon" in s for s in supporting)

    # Counter evidence checks
    counter = result["counter_evidence"]
    assert any("No authenticated login" in c for c in counter)
    assert any("public forum" in c for c in counter)


def test_generate_identity_investigation_unknown_returns_none(db_session):
    """Unknown nodes without links, evidence, or customer record return None."""
    result = generate_identity_investigation("NON-EXISTENT-NODE", db_session)
    assert result is None


def test_api_golden_customer_investigation_endpoint(db_session):
    """Test GET /api/v1/golden-customers/{id}/investigation."""
    app.dependency_overrides[get_db] = lambda: db_session
    client = TestClient(app)
    try:
        resp = client.get("/api/v1/golden-customers/DEMO-EVE-001/investigation")
        assert resp.status_code == 200
        data = resp.json()
        assert data["confidence_score"] == 95
        assert data["decision_status"] == "STRONG_CANDIDATE_PENDING_REVIEW"
        assert len(data["supporting_evidence"]) >= 2
        assert len(data["counter_evidence"]) >= 2

        # Test 404 for unknown
        resp_404 = client.get("/api/v1/golden-customers/UNKNOWN-CUST/investigation")
        assert resp_404.status_code == 404
    finally:
        app.dependency_overrides.clear()
