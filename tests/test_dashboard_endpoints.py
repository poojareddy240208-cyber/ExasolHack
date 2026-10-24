"""
Tests for every dashboard (read-only) API endpoint, plus a check that endpoints.md matches the real routes.

PostgreSQL is replaced by in-memory SQLite and Redis by a fake cart tracker, so the tests prove request/response
behaviour and the SQL used (kept portable on purpose), not the real engines.
"""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.v1.endpoints import system
from backend.db.session import get_db
from backend.main import app
from backend.models.base import Base
from backend.models.inventory import Inventory
from backend.models.sales import RetentionCampaignAudit
from backend.services.cart_tracker import get_cart_tracker

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime.now(timezone.utc)

TABLES = """
CREATE TABLE user_risk_cache (user_id TEXT PRIMARY KEY, golden_customer_id TEXT, risk_level INTEGER NOT NULL,
    return_rate REAL, purchases_6m INTEGER, returns_6m INTEGER, computed_at TEXT, synced_at TEXT);
CREATE TABLE golden_customers (golden_customer_id TEXT PRIMARY KEY, primary_email TEXT, member_count INTEGER NOT NULL,
    total_spend_usd REAL NOT NULL, first_seen_at TEXT, last_seen_at TEXT, synced_at TEXT);
CREATE TABLE golden_customer_members (golden_customer_id TEXT NOT NULL, node_id TEXT NOT NULL, node_type TEXT NOT NULL,
    source_id TEXT NOT NULL, confidence REAL NOT NULL, synced_at TEXT, PRIMARY KEY (golden_customer_id, node_id));
"""


def _build_session(with_dashboard_tables=True):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    session.execute(text("DROP TABLE IF EXISTS user_risk_cache"))
    if with_dashboard_tables:
        for statement in filter(None, (s.strip() for s in TABLES.split(";"))):
            session.execute(text(statement))
        fresh = NOW.isoformat()
        # five customers with rising spend: CUST-E is the top spender (percent rank 1.0)
        for index, (cid, email, members, spend) in enumerate(
            [
                ("CUST-A", "ann@example.com", 3, 100.0),
                ("CUST-B", "bob@example.com", 1, 200.0),
                ("CUST-C", "cy@example.com", 1, 300.0),
                ("CUST-D", "di@example.com", 2, 400.0),
                ("CUST-E", "eve@example.com", 4, 500.0),
            ]
        ):
            session.execute(
                text("INSERT INTO golden_customers VALUES (:i, :e, :m, :s, NULL, NULL, :t)"),
                {"i": cid, "e": email, "m": members, "s": spend, "t": fresh},
            )
        for row in [
            ("CUST-E", "SHOPIFY:user_000005", "SHOPIFY", "user_000005", 1.0),
            ("CUST-E", "DEVICE:dev_aaa", "DEVICE", "dev_aaa", 0.98),
            ("CUST-E", "ZENDESK:eve@example.com", "ZENDESK", "eve@example.com", 0.95),
            ("CUST-A", "SHOPIFY:user_000001", "SHOPIFY", "user_000001", 1.0),
        ]:
            session.execute(
                text("INSERT INTO golden_customer_members VALUES (:a,:b,:c,:d,:e,NULL)"),
                dict(zip("abcde", row)),
            )
        session.execute(
            text("INSERT INTO user_risk_cache VALUES ('user_000005','CUST-E',5,0.75,8,6,:t,:t)"),
            {"t": fresh},
        )
        session.execute(
            text("INSERT INTO user_risk_cache VALUES ('user_000001','CUST-A',3,0.3,10,3,:t,:t)"),
            {"t": fresh},
        )
    session.add_all(
        [
            Inventory(
                item_id="SKU-A",
                name="Item A",
                stock=10,
                version=4,
                virtual_safety_buffer=9,
            ),
            Inventory(
                item_id="SKU-B",
                name="Item B",
                stock=5,
                version=1,
                virtual_safety_buffer=0,
            ),
        ]
    )
    session.add_all(
        [
            RetentionCampaignAudit(
                ticket_id="ZD-1",
                user_id="CUST-E",
                churn_score=0.97,
                ltv_tier="Top 1% VIP",
                action_triggered="ACTION TRIGGERED: Concierge Replacement",
                created_at=NOW - timedelta(hours=1),
            ),
            RetentionCampaignAudit(
                ticket_id="ZD-2",
                user_id="CUST-D",
                churn_score=0.88,
                ltv_tier="Top 10% VIP",
                action_triggered="ACTION TRIGGERED: Priority Escalation",
                created_at=NOW - timedelta(hours=5),
            ),
            RetentionCampaignAudit(
                ticket_id="ZD-OLD",
                user_id="CUST-A",
                churn_score=0.9,
                ltv_tier="Top 20% VIP",
                action_triggered="ACTION TRIGGERED: VIP Win-Back Offer",
                created_at=NOW - timedelta(days=10),
            ),
        ]
    )
    session.commit()
    return session


def _tracker(velocities=None, fail=False):
    tracker = MagicMock()
    tracker.DEFAULT_WINDOW_SECONDS = 300
    counts = dict(velocities or {})

    def velocity(item_id, *_):
        if fail:
            raise ConnectionError("redis down")
        return counts.get(item_id, 0)

    def add(item_id, session_id, *_):
        if fail:
            raise ConnectionError("redis down")
        counts[item_id] = counts.get(item_id, 0) + 1
        return True

    tracker.get_cart_velocity.side_effect = velocity
    tracker.add_to_cart.side_effect = add
    return tracker


@pytest.fixture
def make_client():
    created = []

    def _make(with_dashboard_tables=True, tracker=None):
        session = _build_session(with_dashboard_tables)
        created.append(session)
        app.dependency_overrides[get_db] = lambda: (yield session)
        app.dependency_overrides[get_cart_tracker] = lambda: tracker or _tracker({"SKU-A": 3})
        return TestClient(app, raise_server_exceptions=False), session

    yield _make
    app.dependency_overrides.clear()
    for session in created:
        session.close()


# ------------------------------------------------------------------ docs match reality
def test_endpoints_md_lists_exactly_the_real_routes():
    documented = set(
        re.findall(
            r"^\| `(GET|POST)` \| `(/[^`]*)` \|",
            (ROOT / "endpoints.md").read_text(encoding="utf-8"),
            re.M,
        )
    )
    schema = TestClient(app).get("/openapi.json").json()["paths"]
    real = {(method.upper(), path) for path, ops in schema.items() for method in ops}
    real.add(("GET", "/"))  # the root index is deliberately hidden from the OpenAPI schema
    # endpoints.md writes path parameters as {name}, same as OpenAPI
    assert (
        documented == real
    ), f"undocumented: {sorted(real - documented)} | documented but missing: {sorted(documented - real)}"


# ------------------------------------------------------------------ system status
def _probes(postgres="ok", redis_="ok", exasol="ok"):
    def make(result):
        def probe():
            if isinstance(result, Exception):
                raise result
            return result

        return probe

    return (
        patch.object(system, "probe_postgres", make(postgres)),
        patch.object(system, "probe_redis", make(redis_)),
        patch.object(system, "probe_exasol", make(exasol)),
    )


def test_system_status_healthy(make_client):
    client, _ = make_client()
    p1, p2, p3 = _probes()
    with p1, p2, p3:
        body = client.get("/api/v1/system/status").json()
    assert body["status"] == "HEALTHY"
    assert {name: s["status"] for name, s in body["services"].items()} == {
        "postgres_oltp": "UP",
        "redis_cache": "UP",
        "exasol_olap": "UP",
    }
    assert body["data_freshness"]["stale"] is False
    assert body["row_counts"]["golden_customers"] == 5 and body["row_counts"]["inventory"] == 2


def test_system_status_degraded_when_a_service_is_down(make_client):
    client, _ = make_client()
    p1, p2, p3 = _probes(exasol=ConnectionRefusedError("refused"))
    with p1, p2, p3:
        response = client.get("/api/v1/system/status")
    assert response.status_code == 200  # the UI renders the failure; the endpoint itself does not error
    body = response.json()
    assert body["status"] == "DEGRADED"
    assert (
        body["services"]["exasol_olap"]["status"] == "DOWN"
        and "ConnectionRefusedError" in body["services"]["exasol_olap"]["detail"]
    )


def test_system_status_flags_a_stale_or_missing_sync(make_client):
    client, session = make_client()
    old = (NOW - timedelta(hours=2)).isoformat()
    session.execute(text("UPDATE user_risk_cache SET synced_at = :t"), {"t": old})
    session.commit()
    p1, p2, p3 = _probes()
    with p1, p2, p3:
        assert client.get("/api/v1/system/status").json()["data_freshness"]["stale"] is True
    client, _ = make_client(with_dashboard_tables=False)
    with p1, p2, p3:
        body = client.get("/api/v1/system/status").json()
    assert body["status"] == "DEGRADED" and body["row_counts"]["user_risk_cache"] is None


# ------------------------------------------------------------------ inventory & cart
def test_inventory_list_with_live_carts(make_client):
    client, _ = make_client()
    body = client.get("/api/v1/inventory").json()
    assert body["total_items"] == 2 and body["redis_available"] is True and body["total_active_carts_5m"] == 3
    a, b = body["items"]
    assert (
        a["item_id"],
        a["stock"],
        a["virtual_safety_buffer"],
        a["available_stock"],
    ) == ("SKU-A", 10, 9, 1)
    assert a["active_carts_5m"] == 3 and a["predicted_demand_15m_estimate"] == 16  # 9 + 10 - 3
    assert b["available_stock"] == 5 and b["predicted_demand_15m_estimate"] is None  # buffer 0 is clamped


def test_inventory_list_survives_redis_outage(make_client):
    client, _ = make_client(tracker=_tracker(fail=True))
    response = client.get("/api/v1/inventory")
    body = response.json()
    assert response.status_code == 200 and body["redis_available"] is False
    assert body["total_active_carts_5m"] is None and all(i["active_carts_5m"] is None for i in body["items"])


def test_inventory_item_found_and_missing(make_client):
    client, _ = make_client()
    assert client.get("/api/v1/inventory/SKU-B").json()["name"] == "Item B"
    missing = client.get("/api/v1/inventory/NOPE")
    assert missing.status_code == 404 and "not found" in missing.json()["detail"]


def test_inventory_503_when_tables_are_missing():
    session = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session = sessionmaker(bind=session)()  # no tables at all: bootstrap has not run
    app.dependency_overrides[get_db] = lambda: (yield session)
    app.dependency_overrides[get_cart_tracker] = lambda: _tracker()
    try:
        response = TestClient(app, raise_server_exceptions=False).get("/api/v1/inventory")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 503 and "bootstrap" in response.json()["detail"]


def test_cart_add_then_velocity(make_client):
    client, _ = make_client(tracker=_tracker())
    first = client.post("/api/v1/cart/add", json={"item_id": "SKU-A", "user_session_id": "s1"})
    second = client.post("/api/v1/cart/add", json={"item_id": "SKU-A", "user_session_id": "s2"})
    assert first.status_code == 200 and first.json()["active_carts_5m"] == 1 and first.json()["window_seconds"] == 300
    assert second.json()["active_carts_5m"] == 2
    assert client.get("/api/v1/cart/velocity/SKU-A").json() == {
        "item_id": "SKU-A",
        "window_seconds": 300,
        "active_carts_5m": 2,
    }


def test_cart_add_unknown_item_validation_and_redis_down(make_client):
    client, _ = make_client()
    assert client.post("/api/v1/cart/add", json={"item_id": "NOPE", "user_session_id": "s"}).status_code == 404
    assert client.post("/api/v1/cart/add", json={"item_id": "SKU-A"}).status_code == 422
    down, _ = make_client(tracker=_tracker(fail=True))
    assert down.post("/api/v1/cart/add", json={"item_id": "SKU-A", "user_session_id": "s"}).status_code == 503
    assert down.get("/api/v1/cart/velocity/SKU-A").status_code == 503


# ------------------------------------------------------------------ return risk
def test_return_risk_policies(make_client):
    client, _ = make_client()
    policies = {p["level"]: p for p in client.get("/api/v1/return-risk/policies").json()["policies"]}
    assert sorted(policies) == [1, 2, 3, 4, 5]
    assert [policies[level]["defined_in_prd"] for level in (1, 2, 3, 4, 5)] == [
        True,
        False,
        True,
        False,
        True,
    ]
    assert policies[3]["keep_it_discount_pct"] == 10
    assert policies[5]["restocking_fee_usd"] == 5.0 and policies[5]["cash_on_delivery_allowed"] is False
    assert policies[1]["cash_on_delivery_allowed"] is True


def test_return_risk_for_known_and_unknown_shopper(make_client):
    client, _ = make_client()
    known = client.get("/api/v1/return-risk/user_000005").json()
    assert known["found"] is True and known["risk_level"] == 5 and known["return_rate"] == 0.75
    assert (
        known["purchases_6m"],
        known["returns_6m"],
        known["golden_customer_id"],
    ) == (8, 6, "CUST-E")
    assert known["policy"]["cash_on_delivery_allowed"] is False
    new = client.get("/api/v1/return-risk/brand_new_user").json()
    assert new["found"] is False and new["risk_level"] == 1 and new["policy"]["restocking_fee_usd"] == 0.0


def test_return_risk_503_when_cache_table_missing(make_client):
    client, _ = make_client(with_dashboard_tables=False)
    assert client.get("/api/v1/return-risk/anyone").status_code == 503


# ------------------------------------------------------------------ golden record
def test_golden_stats(make_client):
    client, _ = make_client()
    body = client.get("/api/v1/golden-customers/stats").json()
    assert body["golden_customers"] == 5 and body["stitched_nodes"] == 11  # 3+1+1+2+4
    assert body["multi_identity_customers"] == 3 and body["avg_members_per_customer"] == 2.2


@pytest.mark.parametrize(
    "query",
    ["eve@example", "CUST-E", "user_000005", "dev_aaa", "EVE@EXAMPLE.COM"],
    ids=["email", "golden-id", "shopify", "device", "case"],
)
def test_golden_search_finds_a_customer_by_any_identity(make_client, query):
    client, _ = make_client()
    body = client.get("/api/v1/golden-customers", params={"q": query}).json()
    assert body["count"] == 1 and body["results"][0]["golden_customer_id"] == "CUST-E"
    assert body["results"][0]["ltv_tier"] == "Top 1% VIP"  # highest spender


def test_golden_search_no_match_limit_and_validation(make_client):
    client, _ = make_client()
    assert client.get("/api/v1/golden-customers", params={"q": "nobody-at-all"}).json()["count"] == 0
    assert client.get("/api/v1/golden-customers", params={"q": "@example.com", "limit": 2}).json()["count"] == 2
    assert client.get("/api/v1/golden-customers", params={"q": "x"}).status_code == 422
    assert client.get("/api/v1/golden-customers").status_code == 422


def test_golden_profile_has_members_ltv_and_return_risk(make_client):
    client, _ = make_client()
    body = client.get("/api/v1/golden-customers/CUST-E").json()
    assert body["primary_email"] == "eve@example.com" and body["total_spend_usd"] == 500.0
    assert body["ltv_percent_rank"] == 1.0 and body["ltv_tier"] == "Top 1% VIP"
    assert [(m["node_type"], m["source_id"], m["confidence"]) for m in body["members"]] == [
        ("SHOPIFY", "user_000005", 1.0),
        ("DEVICE", "dev_aaa", 0.98),
        ("ZENDESK", "eve@example.com", 0.95),
    ]
    assert body["return_risk"]["risk_level"] == 5 and body["return_risk"]["policy"]["name"] == "Penalty Guard"
    low = client.get("/api/v1/golden-customers/CUST-A").json()
    assert low["ltv_tier"] == "Standard" and low["ltv_percent_rank"] == 0.0 and low["return_risk"]["risk_level"] == 3


def test_golden_profile_without_risk_and_unknown_id(make_client):
    client, _ = make_client()
    assert client.get("/api/v1/golden-customers/CUST-D").json()["return_risk"] is None  # no members/risk synced
    assert client.get("/api/v1/golden-customers/CUST-NOPE").status_code == 404


# ------------------------------------------------------------------ churn alerts
def test_churn_alerts_newest_first_with_window_count(make_client):
    client, _ = make_client()
    body = client.get("/api/v1/churn/alerts").json()
    assert body["total_alerts"] == 3 and body["count_in_window"] == 2 and body["since_hours"] == 24
    assert [a["ticket_id"] for a in body["alerts"]] == ["ZD-1", "ZD-2", "ZD-OLD"]
    first = body["alerts"][0]
    assert (
        first["golden_customer_id"],
        first["customer_email"],
        first["churn_score"],
    ) == (
        "CUST-E",
        "eve@example.com",
        0.97,
    )
    assert first["ltv_tier"] == "Top 1% VIP" and first["status"] == "DISPATCHED"


def test_churn_alert_filters_and_missing_golden_record(make_client):
    client, _ = make_client()
    assert [
        a["ticket_id"] for a in client.get("/api/v1/churn/alerts", params={"min_score": 0.95}).json()["alerts"]
    ] == ["ZD-1"]
    assert len(client.get("/api/v1/churn/alerts", params={"limit": 1}).json()["alerts"]) == 1
    assert client.get("/api/v1/churn/alerts", params={"since_hours": 24 * 30}).json()["count_in_window"] == 3
    assert client.get("/api/v1/churn/alerts", params={"min_score": 2}).status_code == 422
    # alerts still list when the Golden Record has not been synced yet (email is simply unknown)
    no_golden, _ = make_client(with_dashboard_tables=False)
    alerts = no_golden.get("/api/v1/churn/alerts").json()["alerts"]
    assert len(alerts) == 3 and all(a["customer_email"] is None for a in alerts)


# ------------------------------------------------------------------ existing endpoints still behave as documented
def test_existing_endpoints_unchanged(make_client):
    client, session = make_client()
    assert client.get("/health").json()["status"] == "HEALTHY"
    assert client.get("/api/v1/health").json()["services"]["api"] == "UP"
    assert client.get("/").json()["documentation"] == "/docs"
    ok = client.post(
        "/api/v1/checkout",
        json={
            "item_id": "SKU-B",
            "quantity": 1,
            "user_session_id": "s",
            "user_id": "user_000005",
        },
    )
    assert ok.status_code == 200 and ok.json()["precomputed_return_risk_level"] == 5  # reads the same cache
    assert (
        client.post(
            "/api/v1/checkout",
            json={"item_id": "SKU-A", "quantity": 2, "user_session_id": "s"},
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/v1/checkout",
            json={"item_id": "NOPE", "quantity": 1, "user_session_id": "s"},
        ).status_code
        == 404
    )
    with patch(
        "backend.celery_app.process_zendesk_ticket_task.apply_async",
        return_value=MagicMock(id="t-1"),
    ):
        accepted = client.post(
            "/api/v1/webhooks/zendesk",
            json={"ticket_id": "T", "subject": "s", "description": "d"},
        )
    assert accepted.status_code == 202 and accepted.json()["task_id"] == "t-1"
    assert client.post("/api/v1/webhooks/zendesk", json={}).status_code == 422
