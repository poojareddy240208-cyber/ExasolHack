"""
Unit tests for PostgreSQL SQLAlchemy Models.
Validates Inventory constraints, OCC version defaults, and SalesLedger transactions.
"""

import uuid
from datetime import datetime, timezone
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import IntegrityError

from backend.models.base import Base
from backend.models.inventory import Inventory
from backend.models.sales import SalesLedger


@pytest.fixture
def db_session():
    """Provides an isolated SQLite in-memory database session with constraints enabled."""
    engine = create_engine("sqlite:///:memory:", echo=False)

    # Enable SQLite CHECK constraints enforcement
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)
    session = TestingSession()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def test_inventory_creation_and_defaults(db_session):
    """Verifies that an Inventory record sets version=1 and virtual_safety_buffer=0 by default."""
    item = Inventory(item_id="SKU-RTX-5090", name="NVIDIA GeForce RTX 5090", stock=25)
    db_session.add(item)
    db_session.commit()

    saved_item = db_session.get(Inventory, "SKU-RTX-5090")
    assert saved_item is not None
    assert saved_item.item_id == "SKU-RTX-5090"
    assert saved_item.name == "NVIDIA GeForce RTX 5090"
    assert saved_item.stock == 25
    assert saved_item.version == 1, "OCC version must default to 1"
    assert (
        saved_item.virtual_safety_buffer == 0
    ), "Virtual safety stock buffer must default to 0"


def test_inventory_check_constraint_negative_stock(db_session):
    """Verifies that the CheckConstraint prevents negative physical stock."""
    invalid_item = Inventory(
        item_id="SKU-DEFECTIVE", name="Invalid Stock Item", stock=-5
    )
    db_session.add(invalid_item)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_sales_ledger_foreign_key_and_uuid(db_session):
    """Verifies that SalesLedger properly links to Inventory and auto-generates UUID."""
    # 1. Create product inventory
    item = Inventory(item_id="SKU-AIR-JORDAN", name="Nike Air Jordan 1", stock=10)
    db_session.add(item)
    db_session.commit()

    # 2. Record checkout transaction
    tx_id = uuid.uuid4()
    sale = SalesLedger(
        transaction_id=tx_id,
        item_id="SKU-AIR-JORDAN",
        user_id="user_gold_981",
        quantity=2,
        timestamp=datetime.now(timezone.utc),
    )
    db_session.add(sale)
    db_session.commit()

    # 3. Query and verify relationships
    saved_sale = db_session.get(SalesLedger, tx_id)
    assert saved_sale is not None
    assert saved_sale.item_id == "SKU-AIR-JORDAN"
    assert saved_sale.user_id == "user_gold_981"
    assert saved_sale.quantity == 2
    assert saved_sale.inventory_item.name == "Nike Air Jordan 1"


def test_sales_ledger_foreign_key_violation(db_session):
    """Verifies that inserting a sales record with non-existent item_id raises IntegrityError."""
    sale = SalesLedger(
        transaction_id=uuid.uuid4(),
        item_id="NON_EXISTENT_SKU",
        user_id="user_gold_981",
        quantity=1,
    )
    db_session.add(sale)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_retention_campaign_audit_creation(db_session):
    """Verifies that RetentionCampaignAudit records VIP saves with correct fields and UUID."""
    from backend.models.sales import RetentionCampaignAudit

    audit_id = uuid.uuid4()
    audit = RetentionCampaignAudit(
        audit_id=audit_id,
        ticket_id="ZD-83921",
        user_id="user_gold_vip_42",
        churn_score=0.94,
        ltv_tier="Top 1% VIP",
        action_triggered="ACTION TRIGGERED: Concierge Replacement",
        status="DISPATCHED",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(audit)
    db_session.commit()

    saved_audit = db_session.get(RetentionCampaignAudit, audit_id)
    assert saved_audit is not None
    assert saved_audit.ticket_id == "ZD-83921"
    assert saved_audit.churn_score == 0.94
    assert saved_audit.ltv_tier == "Top 1% VIP"
    assert "Concierge" in saved_audit.action_triggered


def test_inventory_phase1_fields_and_available_stock_invariant(db_session):
    """Verifies reserved_stock default, forecast timestamps, and Available = Physical - Reserved invariant."""
    now = datetime.now(timezone.utc)
    item = Inventory(
        item_id="SKU-RESERVE-TEST",
        name="Reserved Stock Item",
        stock=50,
        reserved_stock=15,
        forecasted_depletion_timestamp=now,
        forecast_valid_until=now,
    )
    db_session.add(item)
    db_session.commit()

    saved = db_session.get(Inventory, "SKU-RESERVE-TEST")
    assert saved is not None
    assert saved.stock == 50
    assert saved.reserved_stock == 15
    assert saved.physical_stock == 50
    # Invariant: Available = Physical - Reserved
    assert saved.available_stock == 35
    assert saved.forecasted_depletion_timestamp is not None
    assert saved.forecast_valid_until is not None


def test_inventory_reservation_creation_and_lifecycle(db_session):
    """Verifies InventoryReservation model creation, status tracking, and relationships."""
    from backend.models.inventory import InventoryReservation
    from datetime import timedelta

    item = Inventory(
        item_id="SKU-HOLD-TEST", name="Hold Item", stock=20, reserved_stock=0
    )
    db_session.add(item)
    db_session.commit()

    res_id = str(uuid.uuid4())
    res = InventoryReservation(
        id=res_id,
        item_id="SKU-HOLD-TEST",
        user_session_id="sess_hold_123",
        quantity=5,
        expires_at=datetime.utcnow() + timedelta(minutes=5),
        status="PENDING",
    )
    db_session.add(res)
    item.reserved_stock += 5
    db_session.commit()

    saved_res = db_session.get(InventoryReservation, res_id)
    assert saved_res is not None
    assert saved_res.status == "PENDING"
    assert saved_res.quantity == 5
    assert saved_res.inventory_item.item_id == "SKU-HOLD-TEST"
    assert item.available_stock == 15


def test_sales_ledger_phase1_columns(db_session):
    """Verifies return_reason_code and supplier_batch_id nullable fields in SalesLedger."""
    item = Inventory(item_id="SKU-BATCH-TEST", name="Batch Item", stock=100)
    db_session.add(item)
    db_session.commit()

    tx_id = uuid.uuid4()
    sale = SalesLedger(
        transaction_id=tx_id,
        item_id="SKU-BATCH-TEST",
        user_id="user_batch_user",
        quantity=3,
        return_reason_code="DEFECTIVE",
        supplier_batch_id="BATCH-2026-X89",
    )
    db_session.add(sale)
    db_session.commit()

    saved = db_session.get(SalesLedger, tx_id)
    assert saved is not None
    assert saved.return_reason_code == "DEFECTIVE"
    assert saved.supplier_batch_id == "BATCH-2026-X89"


def test_user_risk_cache_model(db_session):
    """Verifies UserRiskCache model creation with computed_at and source_watermark."""
    from backend.models.sales import UserRiskCache

    now = datetime.now(timezone.utc)
    risk_entry = UserRiskCache(
        user_id="user_risk_model_1",
        golden_customer_id="GOLDEN-42",
        risk_level=3,
        return_rate=0.35,
        purchases_6m=10,
        returns_6m=3,
        computed_at=now,
        source_watermark=now,
    )
    db_session.add(risk_entry)
    db_session.commit()

    saved = db_session.get(UserRiskCache, "user_risk_model_1")
    assert saved is not None
    assert saved.risk_level == 3
    assert saved.return_rate == 0.35
    assert saved.computed_at is not None
    assert saved.source_watermark is not None


def test_release_expired_reservations_reaper(db_session):
    """Verifies that release_expired_reservations reaps expired PENDING reservations and decrements reserved_stock."""
    from datetime import timedelta
    from backend.models.inventory import InventoryReservation
    from backend.services.inventory_manager import release_expired_reservations

    item = Inventory(
        item_id="SKU-EXPIRE-TEST", name="Expiring Item", stock=30, reserved_stock=10
    )
    db_session.add(item)
    db_session.commit()

    # Create an expired reservation
    expired_res = InventoryReservation(
        id=str(uuid.uuid4()),
        item_id="SKU-EXPIRE-TEST",
        user_session_id="sess_expired",
        quantity=6,
        expires_at=datetime.utcnow() - timedelta(minutes=1),
        status="PENDING",
    )
    # Create an active reservation (not expired)
    active_res = InventoryReservation(
        id=str(uuid.uuid4()),
        item_id="SKU-EXPIRE-TEST",
        user_session_id="sess_active",
        quantity=4,
        expires_at=datetime.utcnow() + timedelta(minutes=5),
        status="PENDING",
    )
    db_session.add_all([expired_res, active_res])
    db_session.commit()

    released_count = release_expired_reservations(db_session)
    assert released_count == 1

    db_session.refresh(expired_res)
    db_session.refresh(active_res)
    db_session.refresh(item)

    assert expired_res.status == "EXPIRED"
    assert active_res.status == "PENDING"
    # 10 reserved - 6 released = 4 reserved remaining
    assert item.reserved_stock == 4
    # Available stock restored: 30 - 4 = 26
    assert item.available_stock == 26
