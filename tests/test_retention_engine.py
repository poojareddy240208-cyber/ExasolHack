"""
Unit tests for backend/services/retention_engine.py.
Validates Golden JOIN SQL generation, threshold parameters, and VIP audit logging.
"""

from unittest.mock import MagicMock, patch

from backend.services.retention_engine import (
    get_golden_retention_join_sql,
    run_retention_vip_join,
)


def test_golden_retention_join_sql_contains_thresholds():
    """Verifies that the Golden JOIN SQL correctly incorporates churn and LTV percentile thresholds."""
    sql = get_golden_retention_join_sql(churn_threshold=0.85, ltv_percentile=0.80)
    assert "PREDICT_CHURN_SCORE" in sql
    assert "0.85" in sql
    assert "0.8" in sql
    assert "STAGING_ZENDESK_TICKETS" in sql
    assert "RAW_PURCHASES" in sql
    assert "PERCENT_RANK()" in sql


def test_golden_retention_join_sql_custom_thresholds():
    """Verifies that custom thresholds update the generated SQL correctly."""
    sql = get_golden_retention_join_sql(churn_threshold=0.90, ltv_percentile=0.95)
    assert "0.9" in sql
    assert "0.95" in sql


@patch("backend.services.retention_engine.connect_exasol")
def test_run_retention_vip_join_mock_execution(mock_connect):
    """Verifies that run_retention_vip_join parses results from Exasol export."""
    mock_conn = MagicMock()
    mock_connect.return_value = mock_conn

    # Simulated row format:
    # (ticket_id, user_id, sentiment_excerpt, churn_score, total_spend, ltv_percent_rank, ltv_tier, action_triggered)
    mock_conn.export_to_list.return_value = [
        [
            "ZD-83921",
            "user_gold_vip_42",
            "Coat stitching came undone. Totally unacceptable...",
            0.94,
            42100.0,
            0.99,
            "Top 1% VIP",
            "ACTION TRIGGERED: Concierge Replacement",
        ]
    ]

    records = run_retention_vip_join(churn_threshold=0.85, ltv_percentile=0.80)
    assert len(records) == 1
    rec = records[0]
    assert rec["ticket_id"] == "ZD-83921"
    assert rec["user_id"] == "user_gold_vip_42"
    assert rec["churn_score"] == 0.94
    assert rec["ltv_tier"] == "Top 1% VIP"
    assert "Concierge" in rec["action_triggered"]
    mock_conn.close.assert_called_once()
