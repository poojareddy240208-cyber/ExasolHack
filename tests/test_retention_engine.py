"""
Unit tests for backend/services/retention_engine.py.
Validates Golden JOIN SQL generation, threshold parameters, and VIP audit logging.
"""

from unittest.mock import MagicMock, patch

from backend.services.retention_engine import (
    get_golden_retention_join_sql,
    get_vip_churn_radar_sql,
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
    assert "GOLDEN_CUSTOMERS" in sql
    assert "IDENTITY_NODES" in sql
    assert "TOTAL_SPEND_USD" in sql
    assert "LATERAL" not in sql
    assert "EXECUTE SCRIPT" not in sql


def test_get_vip_churn_radar_sql_scalar_and_identity_stitching():
    """Verifies that get_vip_churn_radar_sql calls scalar UDF in SELECT and joins identity nodes."""
    sql = get_vip_churn_radar_sql(churn_threshold=0.85, ltv_percentile=0.80)
    assert "OMNISYNC_ANALYTICS.PREDICT_CHURN_SCORE(t.CLEANED_TEXT)" in sql
    assert "LATERAL" not in sql
    assert "EXECUTE SCRIPT" not in sql
    assert "IDENTITY_NODES" in sql
    assert "GOLDEN_CUSTOMERS" in sql
    assert "TOTAL_SPEND_USD" in sql


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


def _position(sql, token):
    assert token in sql, f"{token} missing from the Golden JOIN"
    return sql.index(token)


def test_ticket_resolves_through_identity_nodes_members_and_golden_customers():
    """STAGING_ZENDESK_TICKETS -> IDENTITY_NODES -> GOLDEN_CUSTOMER_MEMBERS -> GOLDEN_CUSTOMERS, in that order."""
    sql = get_vip_churn_radar_sql()
    path = [
        "FROM OMNISYNC_ANALYTICS.STAGING_ZENDESK_TICKETS",
        "OMNISYNC_ANALYTICS.IDENTITY_NODES",
        "OMNISYNC_ANALYTICS.GOLDEN_CUSTOMER_MEMBERS",
        "JOIN OMNISYNC_ANALYTICS.GOLDEN_CUSTOMERS gc",
    ]
    positions = [_position(sql, token) for token in path]
    assert positions == sorted(positions)


def test_golden_join_has_no_bypass_around_the_members_table():
    sql = get_vip_churn_radar_sql()
    assert "PRIMARY_EMAIL" not in sql  # the old "OR gc.PRIMARY_EMAIL = n.EMAIL" shortcut skipped the members table
    join_block = sql.split("ticket_nodes AS")[1].split("vip_tickets AS")[0]
    code_only = "\n".join(line for line in join_block.splitlines() if not line.strip().startswith("--"))
    assert " OR " not in code_only  # one plain equality per rule, so Exasol can hash-join each


def test_ltv_comes_only_from_golden_customers_total_spend():
    sql = get_vip_churn_radar_sql()
    assert "TOTAL_SPEND_USD AS total_spend" in sql
    assert "FROM OMNISYNC_ANALYTICS.RAW_PURCHASES" not in sql
    assert "SUM(QUANTITY" not in sql


def test_udf_is_called_in_the_select_list_and_only_for_vip_tickets():
    sql = get_vip_churn_radar_sql()
    assert "OMNISYNC_ANALYTICS.PREDICT_CHURN_SCORE(t.CLEANED_TEXT) AS CHURN_SCORE" in sql
    # the model is applied after the VIP filter, so it never scores tickets that cannot trigger an action
    assert _position(sql, "vip_tickets AS") < _position(sql, "PREDICT_CHURN_SCORE(")


def test_ticket_with_no_identity_is_excluded_explicitly():
    assert "WHERE USER_ID IS NOT NULL" in get_vip_churn_radar_sql()


@patch("backend.services.retention_engine.connect_exasol")
def test_existing_campaigns_are_not_duplicated(mock_connect):
    def row(ticket):
        return [ticket, "CUST-GOLDEN-1", "text", 0.95, 1000.0, 0.99, "Top 1% VIP", "ACTION TRIGGERED: X"]

    mock_connect.return_value.export_to_list.return_value = [row("ZD-1"), row("ZD-2")]
    session = MagicMock()
    session.query.return_value.filter.return_value.all.return_value = [("ZD-1",)]  # ZD-1 already has a campaign

    records = run_retention_vip_join(db_session=session)

    assert len(records) == 2  # both tickets are still reported
    audited = [call.args[0].ticket_id for call in session.add.call_args_list]
    assert audited == ["ZD-2"]  # but only the new one gets a campaign
    session.commit.assert_called_once()
