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
    assert saved_item.virtual_safety_buffer == 0, "Virtual safety stock buffer must default to 0"


def test_inventory_check_constraint_negative_stock(db_session):
    """Verifies that the CheckConstraint prevents negative physical stock."""
    invalid_item = Inventory(item_id="SKU-DEFECTIVE", name="Invalid Stock Item", stock=-5)
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
    sale = SalesLedger(transaction_id=uuid.uuid4(), item_id="NON_EXISTENT_SKU", user_id="user_gold_981", quantity=1)
    db_session.add(sale)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
