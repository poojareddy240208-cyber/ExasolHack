"""
Offline tests for the Exasol -> PostgreSQL reverse ETL and its Celery Beat wiring.

Both databases are mocked, so these tests prove the control flow (lock, stage, verify, upsert, rollback)
without Docker. They do NOT prove the SQL runs on the real engines.
"""

from unittest.mock import MagicMock, patch

import pytest

from backend.celery_app import celery
from backend.services import reverse_etl
from backend.services.reverse_etl import EXPORTS, UPSERT_SQL, build_export_statement, sync_module3_to_postgres


def _fake_postgres(lock_granted=True, staged_counts=(5, 7)):
    """A fake psycopg2 connection that records every executed statement."""
    executed = []
    cursor = MagicMock()
    cursor.execute.side_effect = lambda sql, *args: executed.append(" ".join(str(sql).split()))
    cursor.fetchone.side_effect = [(lock_granted,), *[(count,) for count in staged_counts]]
    pg = MagicMock()
    pg.cursor.return_value.__enter__.return_value = cursor
    return pg, executed


def _fake_exasol(counts=(5, 7)):
    executed = []
    conn = MagicMock()
    values = iter(counts)
    conn.execute.side_effect = lambda sql: (
        executed.append(sql),
        MagicMock(fetchval=MagicMock(side_effect=lambda: next(values))),
    )[1]
    return conn, executed


def test_export_statements_use_export_into_jdbc_with_matching_placeholders():
    for name, spec in EXPORTS.items():
        statement = build_export_statement(name)
        assert statement.startswith("EXPORT (")
        assert "INTO JDBC AT POSTGRES_OMNISYNC STATEMENT" in statement
        assert f"INSERT INTO {spec['stage']}" in statement
        assert statement.count("?") == len(spec["columns"])


def test_beat_schedule_syncs_every_5_minutes_and_tasks_are_registered():
    entries = {entry["task"]: entry for entry in celery.conf.beat_schedule.values()}
    assert entries["backend.celery_app.trigger_exasol_return_risk_sync"]["schedule"] == 300.0
    assert "backend.celery_app.trigger_identity_stitching_batch" in entries
    for task_name in entries:
        assert task_name in celery.tasks, f"{task_name} is scheduled but not registered"


def test_successful_sync_stages_then_upserts_in_one_commit():
    pg, pg_sql = _fake_postgres()
    exasol, exasol_sql = _fake_exasol()
    with patch.object(reverse_etl, "connect_postgres", return_value=pg), patch.object(
        reverse_etl, "connect_exasol", return_value=exasol
    ):
        result = sync_module3_to_postgres()

    assert result["status"] == "SYNC_COMPLETE"
    assert sum(sql.startswith("EXPORT (") for sql in exasol_sql) == 2
    truncates = [i for i, sql in enumerate(pg_sql) if sql.startswith("TRUNCATE")]
    upserts = [i for i, sql in enumerate(pg_sql) if "ON CONFLICT" in sql]
    assert truncates and upserts and max(truncates) < min(upserts)
    assert len([sql for sql in pg_sql if sql.startswith(("INSERT INTO", "DELETE FROM"))]) == len(UPSERT_SQL)
    pg.rollback.assert_not_called()


def test_empty_stage_aborts_without_touching_serving_tables():
    pg, pg_sql = _fake_postgres(staged_counts=(0, 7))
    exasol, _ = _fake_exasol()
    with patch.object(reverse_etl, "connect_postgres", return_value=pg), patch.object(
        reverse_etl, "connect_exasol", return_value=exasol
    ):
        with pytest.raises(RuntimeError, match="Stage check failed"):
            sync_module3_to_postgres()

    assert not any("ON CONFLICT" in sql or sql.startswith("DELETE FROM") for sql in pg_sql)
    pg.rollback.assert_called()


def test_second_run_is_skipped_while_the_lock_is_held():
    pg, _ = _fake_postgres(lock_granted=False)
    with patch.object(reverse_etl, "connect_postgres", return_value=pg), patch.object(
        reverse_etl, "connect_exasol"
    ) as connect_exasol:
        assert sync_module3_to_postgres() == {"status": "SKIPPED"}
    connect_exasol.assert_not_called()


def test_sync_inventory_buffer_to_postgres_success():
    """Verifies that inventory sync queries Exasol and executes updates in Postgres via SQLAlchemy."""
    from backend.services.reverse_etl import sync_inventory_buffer_to_postgres

    mock_exasol = MagicMock()
    mock_stmt = MagicMock()
    mock_stmt.fetchall.return_value = [
        ("SKU-4090-GPU", 15),
        ("SKU-AIR-JORDAN", 8),
    ]
    mock_exasol.execute.return_value = mock_stmt

    mock_session = MagicMock()
    mock_session.execute.return_value.rowcount = 1  # each UPDATE matched one inventory row

    with patch("backend.services.reverse_etl.connect_exasol", return_value=mock_exasol):
        result = sync_inventory_buffer_to_postgres(session=mock_session)

    assert result["status"] == "SYNC_COMPLETE"
    assert result["records_updated"] == 2
    assert mock_exasol.execute.call_count == 1
    query_executed = mock_exasol.execute.call_args[0][0]
    assert "OMNISYNC_ANALYTICS.LIVE_SKU_TELEMETRY" in query_executed
    assert "VIRTUAL_SAFETY_BUFFER" in query_executed  # computed by the UDF query, not a stored column

    assert mock_session.execute.call_count == 2
    mock_session.commit.assert_called_once()
    mock_exasol.close.assert_called_once()


def test_sync_inventory_buffer_to_postgres_rollback_on_error():
    """Verifies that an exception during Postgres update triggers rollback."""
    from backend.services.reverse_etl import sync_inventory_buffer_to_postgres

    mock_exasol = MagicMock()
    mock_stmt = MagicMock()
    mock_stmt.fetchall.return_value = [("SKU-FAIL", 5)]
    mock_exasol.execute.return_value = mock_stmt

    mock_session = MagicMock()
    mock_session.execute.side_effect = RuntimeError("DB connection dropped")

    with patch("backend.services.reverse_etl.connect_exasol", return_value=mock_exasol):
        with pytest.raises(RuntimeError, match="DB connection dropped"):
            sync_inventory_buffer_to_postgres(session=mock_session)

    mock_session.rollback.assert_called_once()


def test_trigger_exasol_inventory_sync_celery_task():
    """The Celery task refreshes the Exasol telemetry first, then syncs the computed buffer to Postgres."""
    from backend.celery_app import trigger_exasol_inventory_sync

    calls = []
    with patch("backend.services.reverse_etl.refresh_inventory_telemetry") as mock_refresh, patch(
        "backend.services.reverse_etl.sync_inventory_buffer_to_postgres"
    ) as mock_sync:
        mock_refresh.side_effect = lambda: calls.append("refresh") or {"telemetry_rows": 3}
        mock_sync.side_effect = lambda: calls.append("sync") or {
            "status": "SYNC_COMPLETE",
            "target": "POSTGRES_INVENTORY_BUFFER",
            "records_updated": 5,
        }
        result = trigger_exasol_inventory_sync()

    assert calls == ["refresh", "sync"]  # inputs must be loaded before the buffer is computed
    assert result["status"] == "SYNC_COMPLETE"
    assert result["telemetry_rows"] == 3
