"""
Tests for scripts/bootstrap.py: it must import, create every table, seed the 3 demo items idempotently and
extract the Exasol UDF definitions. PostgreSQL is replaced by in-memory SQLite.
"""

from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.models.inventory import Inventory
import scripts.bootstrap as bootstrap


@pytest.fixture
def sqlite_stack():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    session_factory = sessionmaker(bind=engine)
    with patch.object(bootstrap, "engine", engine), patch.object(
        bootstrap, "SessionLocal", session_factory
    ), patch.object(bootstrap, "apply_postgres") as apply_postgres:
        yield engine, session_factory, apply_postgres
    engine.dispose()


def test_bootstrap_imports_the_models_it_needs():
    """Regression: it once imported a `UserRiskCache` class that does not exist, so it crashed on import."""
    assert hasattr(bootstrap, "init_db") and hasattr(bootstrap, "seed_inventory")


def test_init_db_creates_tables_runs_module3_sql_and_seeds_three_items(sqlite_stack):
    engine, session_factory, apply_postgres = sqlite_stack
    bootstrap.init_db(host="somehost")

    tables = set(inspect(engine).get_table_names())
    assert {"inventory", "sales_ledger", "retention_campaign_audit"} <= tables
    apply_postgres.assert_called_once_with("somehost")  # creates user_risk_cache, golden_customers, ... from SQL

    items = {row.item_id: row for row in session_factory().query(Inventory).all()}
    assert set(items) == {"SKU-4090-GPU", "SKU-HERMES-BAG", "SKU-AIR-JORDAN"}
    assert all(item.version == 1 and item.virtual_safety_buffer == 0 for item in items.values())


def test_bootstrap_is_idempotent(sqlite_stack):
    _, session_factory, _ = sqlite_stack
    bootstrap.init_db()
    bootstrap.init_db()
    assert session_factory().query(Inventory).count() == 3


def test_module3_sql_defines_the_tables_the_app_reads():
    sql = (
        bootstrap.Path(bootstrap.__file__).resolve().parent.parent / "sql" / "postgres" / "001_module3_schema.sql"
    ).read_text()
    for table in ("user_risk_cache", "golden_customers", "user_risk_cache_stage", "golden_customers_stage"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql


def test_udf_statements_are_extracted_without_script_markers():
    inventory = bootstrap.udf_statements(bootstrap.UDF_FILES[0])
    churn = bootstrap.udf_statements(bootstrap.UDF_FILES[1])
    assert len(inventory) == 1 and "predict_demand_15m" in inventory[0]
    assert len(churn) == 1 and "PREDICT_CHURN_SCORE" in churn[0] and "RETURNS DOUBLE" in churn[0]
    for statement in inventory + churn:
        assert statement.startswith("CREATE OR REPLACE PYTHON3 SCALAR SCRIPT")
        assert "--/" not in statement and not statement.rstrip().endswith("/")
