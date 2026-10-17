"""
Offline tests for the Serial Returner (Return Risk Level 1-5) batch job. No Exasol connection needed.

They protect the PRD thresholds and the architectural rule that Exasol stays passive.
"""

import pytest

from backend.services.exasol_client import load_sql_statements
from backend.services.return_risk import RETURN_RISK_PARAMS, SQL_FILE, run_return_risk_scoring


def test_sql_renders_with_all_placeholders_filled():
    statements = load_sql_statements(SQL_FILE, RETURN_RISK_PARAMS)
    assert len(statements) == 2  # DELETE + INSERT
    assert all("${" not in stmt for stmt in statements)


def test_prd_thresholds_are_respected():
    """PRD: level 1 below 10%, level 3 covers 25-40%, level 5 above 60%."""
    assert RETURN_RISK_PARAMS["LEVEL1_MAX"] == 0.10
    assert RETURN_RISK_PARAMS["LEVEL2_MAX"] == 0.25  # level 3 starts at 25%
    assert RETURN_RISK_PARAMS["LEVEL3_MAX"] == 0.40  # level 3 ends at 40%
    assert RETURN_RISK_PARAMS["LEVEL4_MAX"] == 0.60  # level 5 is everything above 60%
    assert RETURN_RISK_PARAMS["WINDOW_MONTHS"] == 6


def test_job_never_exports_or_schedules():
    """Exasol is passive: the export to PostgreSQL is triggered by Celery Beat, not by this job."""
    sql = " ".join(load_sql_statements(SQL_FILE, RETURN_RISK_PARAMS)).upper()
    assert "EXPORT" not in sql and "JDBC" not in sql


def test_out_of_order_bounds_are_rejected_before_connecting():
    with pytest.raises(ValueError):
        run_return_risk_scoring(params={"LEVEL2_MAX": 0.05})
