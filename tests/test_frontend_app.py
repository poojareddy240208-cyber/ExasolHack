"""
Headless tests for frontend/app.py using Streamlit's AppTest. The live bridge is faked, so these prove what the page
shows and which bridge calls its buttons make; they do not prove the backend answers (see tests/test_live_bridge.py).
"""

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parent.parent / "frontend" / "app.py")
BRIDGE = "frontend.services.live_bridge"
STATUS_PROBE = "frontend.services.status_probe"

HEALTH_UP = {
    "status": "HEALTHY",
    "error": None,
    "services": {
        "postgres_oltp": {"status": "UP", "latency_ms": 3.1},
        "redis_cache": {"status": "UP", "latency_ms": 1.4},
        "exasol_olap": {"status": "UP", "latency_ms": 42.0},
    },
    "data_freshness": {"age_seconds": 180.0, "stale": False},
}
HEALTH_DOWN = {
    "status": "DEGRADED",
    "error": "Backend unreachable",
    "services": {
        "postgres_oltp": {"status": "DOWN"},
        "redis_cache": {"status": "DOWN"},
        "exasol_olap": {"status": "DOWN"},
    },
    "data_freshness": {"age_seconds": None, "stale": True},
}
INVENTORY = {
    "error": None,
    "redis_available": True,
    "total_active_carts_5m": 12,
    "items": [
        {
            "item_id": "SKU-A",
            "name": "RTX 4090",
            "stock": 50,
            "version": 4,
            "virtual_safety_buffer": 20,
            "available_stock": 30,
            "active_carts_5m": 9,
        },
        {
            "item_id": "SKU-B",
            "name": "Air Jordan",
            "stock": 150,
            "version": 1,
            "virtual_safety_buffer": 0,
            "available_stock": 150,
            "active_carts_5m": 3,
        },
    ],
}
INVESTIGATION = {
    "error": None,
    "found": True,
    "confidence_score": 95,
    "decision_status": "STRONG_CANDIDATE_PENDING_REVIEW",
    "supporting_evidence": [
        "Same promo code used on both devices",
        "Cart abandoned on the laptop, bought on the phone",
    ],
    "counter_evidence": ["Different browser language"],
}
POLICY_FREE = {
    "name": "Standard Free Returns",
    "customer_message": "Free returns with an instant prepaid label.",
    "restocking_fee_usd": 0.0,
    "cash_on_delivery_allowed": True,
    "keep_it_discount_pct": 0,
}
POLICY_PENALTY = {
    "name": "Penalty Guard",
    "customer_message": "A $5 restocking fee applies to returns, and Cash on Delivery is not available.",
    "restocking_fee_usd": 5.0,
    "cash_on_delivery_allowed": False,
    "keep_it_discount_pct": 0,
}
RISK_A = {
    "error": None,
    "found": True,
    "user_id": "Customer A",
    "risk_level": 1,
    "return_rate": 0.8333,
    "purchases_6m": 6,
    "returns_6m": 6,
    "golden_customer_id": "GOLDEN-Customer-A",
    "computed_at": "2026-10-24T10:00:00Z",
    "staleness_warning": False,
    "human_review_required": False,
    "xai_explanation": "Forgiven: 5 of 6 returns (83%) came from defective supplier batch BATCH-8992.",
    "policy": POLICY_FREE,
}
RISK_B = {
    **RISK_A,
    "user_id": "Customer B",
    "risk_level": 5,
    "return_rate": 1.0,
    "purchases_6m": 4,
    "returns_6m": 4,
    "golden_customer_id": None,
    "human_review_required": True,
    "xai_explanation": "Penalized: all 4 returns were flagged as suspected wardrobing.",
    "policy": POLICY_PENALTY,
}
RISKS = {"Customer A": RISK_A, "Customer B": RISK_B}


def _alert(ticket, score, urgency, cause, action, status="PENDING_REVIEW", hours=1, why="Because.", **extra):
    return {
        "ticket_id": ticket,
        "golden_customer_id": "DEMO-EVE-001",
        "customer_email": "eve@example.com",
        "churn_score": score,
        "ltv_tier": "Top 1% VIP",
        "action_triggered": action,
        "status": status,
        "created_at": (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(),
        "xai_explanation": why,
        "urgency": urgency,
        "root_cause": cause,
        "approved_by": None,
        "executed_at": None,
        **extra,
    }


FIT = _alert(
    "ZD-DEMO-FIT-001",
    0.30,
    "MEDIUM",
    "PRODUCT_PREFERENCE",
    "AUTO_RETURN_LABEL",
    hours=3,
    why="Product preference issue detected. Automated prepaid return label recommended.",
)
WEDDING = _alert(
    "ZD-DEMO-WEDDING-002",
    0.99,
    "CRITICAL",
    "FULFILLMENT_FAILURE",
    "ESCALATE_HUMAN_CONCIERGE_AND_OVERNIGHT_REPLACEMENT",
    why="Critical urgency detected due to fulfillment failure. Human concierge escalation required.",
)
ALERTS = {"error": None, "since_hours": 24, "count_in_window": 2, "total_alerts": 2, "alerts": [FIT, WEDDING]}
APPROVED = {
    "success": True,
    "error": None,
    "status": "ACTION_EXECUTED",
    "message": "Action approved and executed successfully.",
}
ALREADY = {"success": True, "error": None, "status": "ACTION_EXECUTED", "message": "Action already executed."}
EMPTY_INVENTORY = {"error": None, "redis_available": True, "total_active_carts_5m": 0, "items": []}


def run_app(
    stack,
    health=HEALTH_UP,
    inventory=INVENTORY,
    investigation=INVESTIGATION,
    surge=None,
    checkout=None,
    risks=None,
    alerts=ALERTS,
    approval=None,
):
    """Patch the bridge (undone when `stack` closes), run the app once and return (AppTest, mocks)."""
    mocks = {
        "get_system_health": MagicMock(return_value=health),
        "get_inventory": MagicMock(return_value=inventory),
        "get_identity_investigation": MagicMock(return_value=investigation),
        "trigger_surge": MagicMock(return_value=surge or {"success": True, "attempted": 15, "successful": 15}),
        "execute_checkout": MagicMock(
            return_value=checkout or {"success": True, "order_id": "ORD-1", "remaining_stock": 49}
        ),
        "get_return_risk": MagicMock(side_effect=lambda uid, *a, **k: (risks or RISKS)[uid]),
        "get_churn_alerts": MagicMock(return_value=alerts),
        "approve_churn_action": MagicMock(return_value=approval or APPROVED),
    }
    for name, mock in mocks.items():
        target = STATUS_PROBE if name == "get_system_health" else BRIDGE
        stack.enter_context(patch(f"{target}.{name}", mock))
    app = AppTest.from_file(APP, default_timeout=20)
    app.run()
    return app, mocks


@pytest.fixture
def launch():
    """
    Start the app against a faked bridge. A test may launch more than once; all patches live on ONE ExitStack so
    they are undone in reverse order. (Undoing stacked patches first-in-first-out leaves a fake installed in the
    real bridge module and breaks every test that runs afterwards.)
    """
    with ExitStack() as stack:
        yield lambda **kwargs: run_app(stack, **kwargs)


def texts(elements):
    return " ".join(str(e.value) for e in elements)


def button(app, label):
    return next(b for b in app.button if b.label == label)


# ------------------------------------------------------------------ shell, sidebar, tabs
def test_app_runs_and_has_the_four_tabs(launch):
    app, _ = launch()
    assert not app.exception
    assert [t.label for t in app.tabs] == [
        "Inventory Surge",
        "Identity Detective",
        "Context Returns",
        "NLP Ticket Radar",
    ]


def test_sidebar_has_the_brand_and_green_lights_when_everything_is_up(launch):
    app, _ = launch()
    sidebar = texts(app.sidebar.markdown)
    assert "OmniSync XAI" in sidebar
    assert sidebar.count("🟢") == 3 and "🔴" not in sidebar
    assert all(name in sidebar for name in ("PostgreSQL", "Redis", "Exasol"))
    assert "Last Exasol sync 3 min ago." in sidebar


def test_sidebar_shows_red_lights_and_a_fix_when_the_backend_is_down(launch):
    app, _ = launch(health=HEALTH_DOWN)
    sidebar = texts(app.sidebar.markdown)
    assert sidebar.count("🔴") == 3 and "🟢" not in sidebar
    assert "Cannot reach the backend" in sidebar and "API container" in sidebar


def test_a_stale_sync_tells_the_operator_what_to_check(launch):
    stale = {**HEALTH_UP, "data_freshness": {"age_seconds": 1500.0, "stale": True}}
    app, _ = launch(health=stale)
    assert "Last Exasol sync was 25 min ago" in texts(app.sidebar.markdown) and "Celery Beat" in texts(
        app.sidebar.markdown
    )


def test_theme_follows_the_design_rules(launch):
    app, _ = launch()
    css = next(m.value for m in app.markdown if "<style>" in m.value)
    for color in ("#1a2332", "#2d8b8b", "#a8dadc", "#f1faee"):  # Ocean Depths
        assert color in css
    assert "DejaVu Sans" in css
    assert "uppercase" not in css  # the design guidance rules out all-caps labels
    assert "prefers-reduced-motion" in css and ":focus-visible" in css


# ------------------------------------------------------------------ inventory tab
def test_inventory_metrics_and_table(launch):
    app, _ = launch()
    metrics = {m.label: m.value for m in app.tabs[0].metric}
    assert metrics == {
        "Physical stock": "200",
        "Reserved holds": "20",
        "Available to sell": "180",
        "Active carts (5 min)": "12",
    }
    table = texts(app.tabs[0].markdown)
    assert "RTX 4090" in table and "SKU-B" in table and "30 of 50 available, 20 held back" in table


def test_backend_text_is_escaped_in_the_table(launch):
    hostile = {**INVENTORY, "items": [{**INVENTORY["items"][0], "name": "<script>alert(1)</script>"}]}
    app, _ = launch(inventory=hostile)
    table = texts(app.tabs[0].markdown)
    assert "<script>" not in table and "&lt;script&gt;" in table


def test_inventory_error_and_empty_states_explain_the_fix(launch):
    app, _ = launch(inventory={**EMPTY_INVENTORY, "error": "Backend unreachable"})
    assert "python -m scripts.bootstrap" in app.tabs[0].error[0].value and not app.exception
    app, _ = launch(inventory=EMPTY_INVENTORY)
    assert "No inventory yet" in app.tabs[0].info[0].value


def test_redis_outage_keeps_the_page_and_warns(launch):
    app, _ = launch(inventory={**INVENTORY, "redis_available": False, "total_active_carts_5m": None})
    assert "Redis is not answering" in app.tabs[0].warning[0].value
    assert {m.label: m.value for m in app.tabs[0].metric}["Active carts (5 min)"] == "n/a"


def test_surge_button_fires_several_bursts_then_reports(launch):
    app, mocks = launch()
    app.selectbox(key="surge_item").select("SKU-A")
    app.button(key="surge").click().run()
    assert not app.exception
    calls = mocks["trigger_surge"].call_args_list
    assert len(calls) == 3 and all(c.args == ("SKU-A",) and c.kwargs == {"count": 15} for c in calls)
    message = app.tabs[0].success[0].value
    assert "Sent 45 of 45 simulated shoppers to RTX 4090" in message and "from 9 to 9" in message


def test_surge_shows_the_cart_spike_after_the_redraw(launch):
    app, mocks = launch()
    spiked = {
        **INVENTORY,
        "total_active_carts_5m": 57,
        "items": [{**INVENTORY["items"][0], "active_carts_5m": 54}, INVENTORY["items"][1]],
    }
    # like the real bridge, the inventory only changes once the surge has been sent
    mocks["get_inventory"].side_effect = lambda: spiked if mocks["trigger_surge"].call_count else INVENTORY
    app.button(key="surge").click().run()
    assert "Active carts went from 9 to 54" in app.tabs[0].success[0].value
    assert {m.label: m.value for m in app.tabs[0].metric}["Active carts (5 min)"] == "57"


def test_surge_failure_is_explained(launch):
    failed = {"success": False, "attempted": 15, "successful": 0, "error": "Failed to reach Redis"}
    app, _ = launch(surge=failed)
    app.button(key="surge").click().run()
    assert (
        "The surge did not start" in app.tabs[0].error[0].value
        and "Failed to reach Redis" in app.tabs[0].error[0].value
    )


# ------------------------------------------------------------------ quick checkout
def place_order(app, sku="SKU-A", quantity=1, stale=False):
    app.selectbox(key="checkout_item").select(sku)
    app.number_input[0].set_value(quantity)
    if stale:
        app.checkbox[0].check()
    button(app, "Place order").click().run()


def test_successful_order_sends_the_right_payload(launch):
    app, mocks = launch()
    place_order(app, quantity=2)
    payload = mocks["execute_checkout"].call_args.args[0]
    assert (
        payload["item_id"] == "SKU-A"
        and payload["quantity"] == 2
        and payload["user_session_id"].startswith("dashboard-")
    )
    assert "expected_version" not in payload
    assert "Order ORD-1 placed for 2." in app.tabs[0].success[0].value


def test_virtual_safety_buffer_rejection_explains_the_numbers(launch):
    conflict = {
        "success": False,
        "status": "CONFLICT",
        "error": "Item is in extremely high demand and currently reserved.",
    }
    app, _ = launch(checkout=conflict)
    place_order(app, sku="SKU-A", quantity=40)
    message = app.tabs[0].error[0].value
    assert "virtual safety buffer" in message and "only 30 can be sold" in message
    assert "20 of the 50 in stock are reserved" in message


def test_optimistic_lock_rejection_and_the_stale_version_option(launch):
    conflict = {"success": False, "status": "CONFLICT", "error": "Concurrent transaction conflict. Please retry."}
    app, mocks = launch(checkout=conflict)
    place_order(app, sku="SKU-A", quantity=1, stale=True)
    assert mocks["execute_checkout"].call_args.args[0]["expected_version"] == 3  # current version 4, minus one
    assert "optimistic lock" in app.tabs[0].error[0].value


@pytest.mark.parametrize(
    "result,expected",
    [
        ({"success": False, "status": "NOT_FOUND", "error": "gone"}, "not in the catalog"),
        ({"success": False, "status": "VALIDATION_ERROR", "error": "quantity: bad"}, "not valid: quantity: bad"),
        ({"success": False, "status": "UNREACHABLE", "error": "refused"}, "Cannot reach the backend"),
        ({"success": False, "status": "HTTP_500", "error": "boom"}, "The order failed: boom"),
    ],
    ids=["missing", "invalid", "down", "other"],
)
def test_other_checkout_failures_have_plain_messages(launch, result, expected):
    app, _ = launch(checkout=result)
    place_order(app)
    assert expected in app.tabs[0].error[0].value and not app.exception


# ------------------------------------------------------------------ identity detective
def test_identity_investigates_the_demo_customer_by_default(launch):
    app, mocks = launch()
    assert app.text_input(key="investigation_customer").value == "DEMO-EVE-001"
    mocks["get_identity_investigation"].assert_called_with("DEMO-EVE-001")


def test_identity_shows_the_confidence_prominently(launch):
    app, _ = launch()
    page = texts(app.tabs[1].markdown)
    assert "95%" in page and "match confidence" in page and "Match confidence 95 percent" in page
    assert "Very likely the same person" in page and "Strong candidate, waiting for review" in page


def test_identity_evidence_columns(launch):
    app, _ = launch()
    columns = app.tabs[1].columns
    assert "Supporting evidence (2)" in columns[-2].markdown[0].value
    assert "Same promo code used on both devices" in columns[-2].markdown[0].value
    assert (
        "Counter-evidence (1)" in columns[-1].markdown[0].value
        and "Different browser language" in columns[-1].markdown[0].value
    )


@pytest.mark.parametrize("score,verdict", [(95, "Very likely"), (70, "Possibly"), (20, "Probably different")])
def test_confidence_verdict_follows_the_score(launch, score, verdict):
    app, _ = launch(investigation={**INVESTIGATION, "confidence_score": score})
    assert verdict in texts(app.tabs[1].markdown) and f"{score}%" in texts(app.tabs[1].markdown)


def test_identity_empty_evidence_and_unknown_status(launch):
    bare = {**INVESTIGATION, "supporting_evidence": [], "counter_evidence": [], "decision_status": "NEEDS_MORE_DATA"}
    app, _ = launch(investigation=bare)
    page = texts(app.tabs[1].markdown)
    assert "No counter-evidence was found." in page and "No supporting evidence was recorded." in page
    assert "Needs more data" in page


def test_identity_errors_are_helpful(launch):
    app, _ = launch(investigation={"error": "Customer 'X' not found in Golden Record.", "found": False})
    assert "No investigation exists for DEMO-EVE-001" in app.tabs[1].info[0].value and not app.exception
    app, _ = launch(investigation={"error": "Backend unreachable at http://api:8000", "found": False})
    assert "Cannot load the investigation" in app.tabs[1].error[0].value


def test_evidence_text_from_the_backend_is_escaped(launch):
    app, _ = launch(investigation={**INVESTIGATION, "supporting_evidence": ["<img src=x onerror=alert(1)>"]})
    page = texts(app.tabs[1].columns[-2].markdown)
    assert "<img" not in page and "&lt;img" in page


# ------------------------------------------------------------------ theme configuration
def test_streamlit_theme_config_is_dark_ocean_depths_and_shipped_in_the_image():
    """Without this file, widgets follow the visitor's light/dark mode and turn white on the navy page."""
    root = Path(APP).parent.parent
    config = (root / ".streamlit" / "config.toml").read_text(encoding="utf-8")
    assert 'base = "dark"' in config
    for color in ("#1a2332", "#2d8b8b", "#f1faee"):
        assert color in config
    assert "COPY .streamlit" in (root / "Dockerfile.frontend").read_text(encoding="utf-8")


def test_icon_font_is_not_overridden_by_the_theme(launch):
    """Regression: the global font rule once turned Streamlit's icon names into visible text."""
    app, _ = launch()
    css = next(m.value for m in app.markdown if "<style>" in m.value)
    assert 'not([data-testid="stIconMaterial"])' in css


def test_launching_twice_in_one_test_leaves_the_real_bridge_untouched():
    """Regression: stacked patches undone in the wrong order once left fakes installed for every later test."""
    from frontend.services import live_bridge

    real = {name: getattr(live_bridge, name) for name in ("get_inventory", "get_system_health", "trigger_surge")}
    with ExitStack() as stack:
        run_app(stack)
        run_app(stack, health=HEALTH_DOWN)
    assert {name: getattr(live_bridge, name) for name in real} == real


# ------------------------------------------------------------------ tab 3: context returns
def metric_values(tab):
    return [(m.label, m.value) for m in tab.metric]


def test_returns_tab_loads_both_demo_customers(launch):
    app, mocks = launch()
    assert not app.exception
    assert [c.args[0] for c in mocks["get_return_risk"].call_args_list] == ["Customer A", "Customer B"]
    page = texts(app.tabs[2].markdown)
    assert "Customer A (Unlucky VIP)" in page and "Customer B (Serial Wardrober)" in page
    assert "Context-aware returns" in page


def test_raw_return_rate_is_shown_next_to_the_risk_level(launch):
    app, _ = launch()
    assert metric_values(app.tabs[2]) == [
        ("Raw return rate", "83%"),
        ("Risk level", "Level 1"),
        ("Raw return rate", "100%"),
        ("Risk level", "Level 5"),
    ]
    page = texts(app.tabs[2].markdown)
    assert "By raw rate alone this shopper would be <b>Level 5</b>. With context they are <b>Level 1</b>." in page
    assert "Risk level 1 of 5" in page and "Risk level 5 of 5" in page


def test_the_explanation_is_prominent_for_both_customers(launch):
    app, _ = launch()
    page = texts(app.tabs[2].markdown)
    assert "Why this level" in page
    assert "Forgiven: 5 of 6 returns (83%) came from defective supplier batch BATCH-8992." in page
    assert "Penalized: all 4 returns were flagged as suspected wardrobing." in page
    assert "Forgiven by context" in page and "Penalty applied" in page


def test_human_review_is_flagged_for_level_five_only(launch):
    app, _ = launch()
    page = texts(app.tabs[2].markdown)
    assert page.count("Human review required.") == 1
    assert [c.value for c in app.tabs[2].caption].count("No human review needed.") == 1


def test_returns_tab_has_expanders_with_policy_and_numbers(launch):
    app, _ = launch()
    labels = [e.label for e in app.tabs[2].expander]
    assert labels.count("What this level means for the shopper") == 2
    assert labels.count("The numbers behind it") == 2 and "How the model weighs a return" in labels
    details = " ".join(m.value for e in app.tabs[2].expander for m in e.markdown)
    assert "Restocking fee: $5.00" in details and "Cash on delivery: not available" in details
    assert "Returns in the last 6 months: 6" in details and "counts as **0**" in details


def test_returns_error_never_shows_a_level(launch):
    down = {"error": "Backend unreachable", "found": False, "risk_level": 1, "user_id": "x"}
    app, _ = launch(risks={"Customer A": down, "Customer B": down})
    assert len(app.tabs[2].error) == 2 and "Backend unreachable" in app.tabs[2].error[0].value
    assert metric_values(app.tabs[2]) == []  # the bridge's fallback says level 1; it must not be drawn as a verdict
    assert not app.exception


def test_returns_unknown_customer_explains_the_fix(launch):
    unknown = {"error": None, "found": False, "risk_level": 1, "user_id": "x"}
    app, _ = launch(risks={"Customer A": unknown, "Customer B": RISK_B})
    assert "No return history for Customer A" in app.tabs[2].info[0].value
    assert metric_values(app.tabs[2]) == [("Raw return rate", "100%"), ("Risk level", "Level 5")]


def test_returns_edge_cases(launch):
    odd = {**RISK_A, "return_rate": None, "xai_explanation": None, "staleness_warning": True}
    app, _ = launch(risks={"Customer A": odd, "Customer B": RISK_B})
    page = texts(app.tabs[2].markdown)
    assert ("Raw return rate", "n/a") in metric_values(app.tabs[2])
    assert "No explanation was recorded for this score." in page
    assert page.count("By raw rate alone this shopper") == 1  # only Customer B: without a rate there is no comparison
    assert "more than 24 hours old" in app.tabs[2].warning[0].value


def test_returns_text_from_the_backend_is_escaped(launch):
    hostile = {**RISK_A, "xai_explanation": "<img src=x onerror=alert(1)>"}
    app, _ = launch(risks={"Customer A": hostile, "Customer B": RISK_B})
    page = texts(app.tabs[2].markdown)
    assert "<img" not in page and "&lt;img" in page


# ------------------------------------------------------------------ tab 4: ticket radar
def test_radar_shows_every_seeded_ticket_with_its_findings(launch):
    app, _ = launch()
    page = texts(app.tabs[3].markdown)
    for expected in (
        "ZD-DEMO-FIT-001",
        "ZD-DEMO-WEDDING-002",
        "Critical",
        "Medium",
        "Fulfillment failure",
        "Product preference",
        "Very negative",
        "Mostly neutral",
        "Waiting for approval",
        "eve@example.com",
        "Top 1% VIP",
    ):
        assert expected in page, expected


def test_radar_shows_the_recommended_action_and_the_reason(launch):
    app, _ = launch()
    page = texts(app.tabs[3].markdown)
    assert "Escalate to a human concierge and ship an overnight replacement" in page
    assert "Send a prepaid return label automatically" in page
    assert "Why the AI chose this" in page
    assert "Critical urgency detected due to fulfillment failure. Human concierge escalation required." in page
    assert "Product preference issue detected. Automated prepaid return label recommended." in page


def test_radar_puts_the_most_urgent_ticket_first(launch):
    app, _ = launch()
    page = texts(app.tabs[3].markdown)
    assert page.index("ZD-DEMO-WEDDING-002") < page.index("ZD-DEMO-FIT-001")


def test_radar_headline_numbers(launch):
    app, _ = launch()
    assert metric_values(app.tabs[3]) == [
        ("Waiting for approval", "2"),
        ("Executed", "0"),
        ("Critical urgency", "1"),
        ("Alerts in the last 24 h", "2"),
    ]


def test_each_ticket_has_a_governance_button_and_an_audit_expander(launch):
    app, _ = launch()
    keys = [b.key for b in app.tabs[3].button]
    assert keys == ["approve_ZD-DEMO-WEDDING-002", "approve_ZD-DEMO-FIT-001"]
    assert all(b.label == "Approve & Execute Action" for b in app.tabs[3].button)
    assert [e.label for e in app.tabs[3].expander] == ["Audit trail", "Audit trail"]


def test_approving_calls_the_backend_and_confirms(launch):
    app, mocks = launch()
    app.button(key="approve_ZD-DEMO-WEDDING-002").click().run()
    assert not app.exception
    mocks["approve_churn_action"].assert_called_once_with("ZD-DEMO-WEDDING-002", "supervisor")
    assert "Action for ZD-DEMO-WEDDING-002 approved and executed by supervisor." in app.tabs[3].success[0].value
    assert any("Action executed" in t.value for t in app.toast)


def test_the_approver_name_is_sent_to_the_backend(launch):
    app, mocks = launch()
    app.text_input(key="approver").set_value("ana").run()
    app.button(key="approve_ZD-DEMO-FIT-001").click().run()
    mocks["approve_churn_action"].assert_called_once_with("ZD-DEMO-FIT-001", "ana")


def test_a_repeat_approval_proves_it_is_idempotent(launch):
    app, mocks = launch(approval=ALREADY)
    app.button(key="approve_ZD-DEMO-WEDDING-002").click().run()
    assert "was already executed, so nothing ran a second time" in app.tabs[3].success[0].value
    assert any("No duplicate action was sent" in t.value for t in app.toast)


def test_an_executed_alert_says_so_and_stays_safe_to_press(launch):
    done = _alert(
        "ZD-DONE-1",
        0.95,
        "HIGH",
        "BILLING_ERROR",
        "AUTO_RETURN_LABEL",
        status="ACTION_EXECUTED",
        approved_by="supervisor",
        executed_at="2026-10-24T11:00:00Z",
    )
    app, _ = launch(alerts={**ALERTS, "alerts": [done], "count_in_window": 1})
    page = texts(app.tabs[3].markdown)
    assert "Action executed" in page and "Already executed. Pressing the button again is safe" in page
    assert "Approved by: supervisor" in page and "Billing error" in page
    assert [b.key for b in app.tabs[3].button] == ["approve_ZD-DONE-1"]  # still pressable on purpose
    assert metric_values(app.tabs[3])[:2] == [("Waiting for approval", "0"), ("Executed", "1")]


def test_a_failed_approval_is_explained(launch):
    failed = {"success": False, "status": "NOT_FOUND", "error": "Alert ticket 'ZD-X' not found."}
    app, _ = launch(approval=failed)
    app.button(key="approve_ZD-DEMO-FIT-001").click().run()
    assert "Could not approve ZD-DEMO-FIT-001" in app.tabs[3].error[0].value
    assert "not found" in app.tabs[3].error[0].value


def test_radar_error_and_empty_states(launch):
    app, _ = launch(alerts={"error": "Backend unreachable", "alerts": []})
    assert "python -m scripts.bootstrap" in app.tabs[3].error[0].value and not app.exception
    app, _ = launch(alerts={"error": None, "alerts": [], "count_in_window": 0, "since_hours": 24})
    assert "No tickets need attention" in app.tabs[3].info[0].value


def test_radar_tolerates_missing_fields_and_escapes_text(launch):
    bare = {
        "ticket_id": "ZD-BARE",
        "churn_score": None,
        "status": "WEIRD_STATUS",
        "action_triggered": None,
        "xai_explanation": "<script>alert(1)</script>",
    }
    app, _ = launch(alerts={**ALERTS, "alerts": [bare], "count_in_window": 1})
    page = texts(app.tabs[3].markdown)
    assert not app.exception
    assert "Not rated" in page and "Not specified" in page and "Weird status" in page
    assert "<script>" not in page and "&lt;script&gt;" in page


# ------------------------------------------------------------------ guide, activity log and new controls
def test_surge_ticks_the_demo_guide_and_is_logged(launch):
    app, _ = launch()
    assert app.checkbox(key="guide_surge").value is False
    app.button(key="surge").click().run()
    assert app.checkbox(key="guide_surge").value is True
    assert any("Surge: 45 simulated shoppers" in line for line in app.session_state["activity_log"])
    assert "1 of 5 steps done" in texts(app.sidebar.markdown) + str([p.text for p in app.sidebar.get("progress")])


def test_a_refused_order_ticks_the_blocked_step(launch):
    app, _ = launch(checkout={"success": False, "status": "CONFLICT", "error": "Item is in high demand, reserved."})
    place_order(app)
    assert app.checkbox(key="guide_blocked").value is True


def test_inventory_chart_view_replaces_the_table(launch):
    app, _ = launch()
    app.get("segmented_control")[0].set_value("Chart").run()
    assert not app.exception
    assert "osx-table" not in texts(app.tabs[0].markdown)
    assert any("chart" in str(getattr(el, "type", "")) for el in app.tabs[0])


def test_radar_search_and_sort_narrow_the_list(launch):
    app, _ = launch()
    app.text_input(key="radar_search").set_value("wedding").run()
    page = texts(app.tabs[3].markdown)
    assert "ZD-DEMO-WEDDING-002" in page and "ZD-DEMO-FIT-001" not in page
    app.text_input(key="radar_search").set_value("nobody-matches").run()
    assert "No tickets match this filter." in texts(app.tabs[3].info)
    app.text_input(key="radar_search").set_value("").run()
    app.selectbox(key="radar_sort").select("Highest churn score").run()
    assert not app.exception
