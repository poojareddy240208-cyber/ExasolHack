"""
Tests that checkout reads the pre-computed Return Risk Level from the `user_risk_cache` table.

The table is filled by the Celery Beat sync from Exasol; here it is seeded directly in an in-memory
SQLite database. They prove the lookup logic and the checkout wiring, not PostgreSQL itself.
"""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db.session import get_db
from backend.main import app
from backend.models.base import Base
from backend.models.inventory import Inventory
from backend.services.cart_tracker import get_cart_tracker
from backend.services.risk_lookup import DEFAULT_RISK_LEVEL, get_return_risk_level

engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
SessionForTests = sessionmaker(autocommit=False, autoflush=False, bind=engine)

CREATE_CACHE = "CREATE TABLE user_risk_cache (user_id TEXT PRIMARY KEY, risk_level INTEGER NOT NULL)"


@pytest.fixture
def db():
    Base.metadata.create_all(bind=engine)
    session = SessionForTests()
    try:
        yield session
    finally:
        session.close()
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS user_risk_cache"))
        Base.metadata.drop_all(bind=engine)


def _seed_cache(db, rows):
    db.execute(text("DROP TABLE IF EXISTS user_risk_cache"))
    db.execute(text(CREATE_CACHE))
    for user_id, level in rows.items():
        db.execute(
            text("INSERT INTO user_risk_cache VALUES (:u, :l)"),
            {"u": user_id, "l": level},
        )
    db.commit()


def test_returns_cached_level_for_known_user(db):
    _seed_cache(db, {"user_000001": 5, "user_000002": 3})
    assert get_return_risk_level(db, "user_000001") == 5
    assert get_return_risk_level(db, "user_000002") == 3


def test_unknown_or_missing_user_gets_default_level(db):
    _seed_cache(db, {"user_000001": 5})
    assert get_return_risk_level(db, "user_never_seen") == DEFAULT_RISK_LEVEL
    assert get_return_risk_level(db, None) == DEFAULT_RISK_LEVEL
    assert get_return_risk_level(db, "") == DEFAULT_RISK_LEVEL


def test_missing_cache_table_falls_back_and_keeps_session_usable(db):
    """If the sync has never run, checkout must still work."""
    assert get_return_risk_level(db, "user_000001") == DEFAULT_RISK_LEVEL
    assert db.execute(text("SELECT 1")).scalar() == 1  # session was rolled back cleanly


def test_out_of_range_values_are_clamped(db):
    _seed_cache(db, {"user_low": 0, "user_high": 9})
    assert get_return_risk_level(db, "user_low") == 1
    assert get_return_risk_level(db, "user_high") == 5


def test_checkout_returns_level_from_cache(db):
    """End to end: the checkout response carries the level read from user_risk_cache."""
    _seed_cache(db, {"user_serial": 5})
    db.add(
        Inventory(
            item_id="SKU-TEST",
            name="Test item",
            stock=10,
            version=1,
            virtual_safety_buffer=0,
        )
    )
    db.commit()

    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_cart_tracker] = lambda: MagicMock()
    try:
        with TestClient(app) as client:
            flagged = client.post(
                "/api/v1/checkout",
                json={
                    "item_id": "SKU-TEST",
                    "quantity": 1,
                    "user_session_id": "sess_1",
                    "user_id": "user_serial",
                },
            )
            new_shopper = client.post(
                "/api/v1/checkout",
                json={
                    "item_id": "SKU-TEST",
                    "quantity": 1,
                    "user_session_id": "sess_2",
                    "user_id": "user_new",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert (
        flagged.status_code == 200
        and flagged.json()["precomputed_return_risk_level"] == 5
    )
    assert (
        new_shopper.status_code == 200
        and new_shopper.json()["precomputed_return_risk_level"] == 1
    )
