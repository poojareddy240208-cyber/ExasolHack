"""
End-to-end test of the predictive inventory safety net, with Exasol and Redis faked:

    PostgreSQL stock + Redis cart velocity -> Exasol telemetry -> computed buffer -> PostgreSQL
    inventory.virtual_safety_buffer -> POST /api/v1/checkout rejects quantity > stock - buffer (HTTP 409)
"""

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db.session import get_db
from backend.main import app
from backend.models.base import Base
from backend.models.inventory import Inventory
from backend.services import reverse_etl
from backend.services.cart_tracker import get_cart_tracker
from backend.services.reverse_etl import (
    INVENTORY_BUFFER_QUERY,
    TELEMETRY_COLUMNS,
    refresh_inventory_telemetry,
    sync_inventory_buffer_to_postgres,
)

SCHEMA_SQL = Path(__file__).resolve().parent.parent / "sql" / "exasol" / "001_module3_schema.sql"


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    session.add_all(
        [
            Inventory(item_id="SKU-A", name="A", stock=10, version=1, virtual_safety_buffer=0),
            Inventory(item_id="SKU-B", name="B", stock=5, version=1, virtual_safety_buffer=0),
        ]
    )
    session.commit()
    yield session
    session.close()
    engine.dispose()


def _fake_exasol(rows):
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = rows
    return conn


def test_buffer_query_reads_only_columns_that_exist_in_the_telemetry_table():
    """Regression: the query once selected a `virtual_safety_buffer` column the table never had."""
    ddl = SCHEMA_SQL.read_text(encoding="utf-8").upper()
    table_ddl = ddl.split("CREATE TABLE IF NOT EXISTS LIVE_SKU_TELEMETRY")[1].split(");")[0]
    for column in ("ITEM_ID", "CURRENT_PHYSICAL_STOCK", "CART_VELOCITY_5M", "TELEMETRY_TIMESTAMP"):
        assert column in table_ddl
    assert "VIRTUAL_SAFETY_BUFFER" not in table_ddl  # computed, never stored
    sql = INVENTORY_BUFFER_QUERY.upper()
    assert "GREATEST(0" in sql and "PREDICT_DEMAND_15M" in sql
    assert "- CURRENT_PHYSICAL_STOCK + CART_VELOCITY_5M" in sql  # PRD formula


def test_sync_writes_buffer_and_counts_real_rows(db):
    fake = _fake_exasol([("SKU-A", 9), ("SKU-NOT-IN-POSTGRES", 4)])
    with patch.object(reverse_etl, "connect_exasol", return_value=fake):
        result = sync_inventory_buffer_to_postgres(session=db)

    assert fake.execute.call_args[0][0] == INVENTORY_BUFFER_QUERY
    assert result["records_updated"] == 1  # the unknown SKU is not counted as an update
    assert result["unknown_items"] == 1
    assert db.execute(text("SELECT virtual_safety_buffer FROM inventory WHERE item_id='SKU-A'")).scalar() == 9
    assert db.execute(text("SELECT virtual_safety_buffer FROM inventory WHERE item_id='SKU-B'")).scalar() == 0


def test_sync_rolls_back_on_failure(db):
    fake = _fake_exasol([("SKU-A", "not-a-number")])
    with patch.object(reverse_etl, "connect_exasol", return_value=fake):
        with pytest.raises(ValueError):
            sync_inventory_buffer_to_postgres(session=db)
    assert db.execute(text("SELECT virtual_safety_buffer FROM inventory WHERE item_id='SKU-A'")).scalar() == 0


def test_checkout_rejects_when_quantity_exceeds_stock_minus_buffer(db):
    with patch.object(reverse_etl, "connect_exasol", return_value=_fake_exasol([("SKU-A", 9)])):
        sync_inventory_buffer_to_postgres(session=db)  # stock 10, buffer 9 -> only 1 available

    app.dependency_overrides[get_db] = lambda: (yield db)
    app.dependency_overrides[get_cart_tracker] = lambda: MagicMock()
    try:
        client = TestClient(app, raise_server_exceptions=False)
        post = lambda qty: client.post(  # noqa: E731
            "/api/v1/checkout", json={"item_id": "SKU-A", "quantity": qty, "user_session_id": "s1"}
        )
        too_many = post(2)  # 2 > 10 - 9
        boundary = post(1)  # 1 > 1 is false -> allowed
    finally:
        app.dependency_overrides.clear()

    assert too_many.status_code == 409
    assert "high demand" in too_many.json()["detail"]
    assert boundary.status_code == 200


def test_telemetry_refresh_loads_stock_and_cart_velocity_into_exasol(db):
    tracker = MagicMock()
    tracker.get_cart_velocity.side_effect = lambda item_id: {"SKU-A": 3}.get(item_id) or (_ for _ in ()).throw(
        ConnectionError("redis down")
    )
    fake = MagicMock()
    with patch.object(reverse_etl, "connect_exasol", return_value=fake):
        result = refresh_inventory_telemetry(session=db, cart_tracker=tracker)

    assert result == {"telemetry_rows": 2}
    assert fake.execute.call_args[0][0].startswith("DELETE FROM OMNISYNC_ANALYTICS.LIVE_SKU_TELEMETRY")
    rows, table = fake.import_from_iterable.call_args[0][:2]
    assert table == ("OMNISYNC_ANALYTICS", "LIVE_SKU_TELEMETRY")
    assert fake.import_from_iterable.call_args.kwargs["import_params"] == {"columns": TELEMETRY_COLUMNS}
    by_item = {row[0]: row for row in rows}
    assert by_item["SKU-A"][1:3] == (10, 3)  # stock 10, 3 active carts
    assert by_item["SKU-B"][1:3] == (5, 0)  # Redis failure degrades to 0 carts instead of failing the sync
    assert isinstance(by_item["SKU-A"][3], datetime)
