"""
OmniSync Live Bridge - Streamlit to FastAPI Service Gateway.

This module provides a production-grade communication bridge between the Streamlit UI
and the OmniSync FastAPI backend (running at http://api:8000 by default).

Design Invariants:
1. Fast & Non-Blocking: All GET calls are wrapped in @st.cache_data(ttl=2) to prevent UI spamming.
2. Resilience: Unreachable backends or HTTP errors return safe fallback structures or error dicts;
   no unhandled exceptions crash the Streamlit interface.
3. Automatic Invalidation: State mutations (surge trigger, checkout, churn approval) clear relevant caches.
4. Configurable Base URL: Default is http://api:8000, overridable via BACKEND_API_BASE_URL env var.
"""

import logging
import os
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Union

import requests
import streamlit as st

logger = logging.getLogger("omnisync.frontend.live_bridge")

# -----------------------------------------------------------------------------
# Configuration & Session Management
# -----------------------------------------------------------------------------

DEFAULT_BASE_URL = os.getenv("BACKEND_API_BASE_URL", "http://api:8000").rstrip("/")
DEFAULT_CONNECT_TIMEOUT = float(os.getenv("API_CONNECT_TIMEOUT", "2.0"))
DEFAULT_READ_TIMEOUT = float(os.getenv("API_READ_TIMEOUT", "5.0"))
DEFAULT_TIMEOUT = (DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT)

_session: Optional[requests.Session] = None


def get_backend_url() -> str:
    """Return the currently configured backend API base URL."""
    return os.getenv("BACKEND_API_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def set_backend_url(url: str) -> None:
    """Override the backend API base URL at runtime."""
    global DEFAULT_BASE_URL
    DEFAULT_BASE_URL = url.rstrip("/")
    os.environ["BACKEND_API_BASE_URL"] = DEFAULT_BASE_URL


def _get_http_session() -> requests.Session:
    """Singleton requests session with connection pooling for low-latency calls."""
    global _session
    if _session is None:
        _session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=20,
            pool_maxsize=50,
            max_retries=1,
        )
        _session.mount("http://", adapter)
        _session.mount("https://", adapter)
    return _session


# -----------------------------------------------------------------------------
# Helper Utilities
# -----------------------------------------------------------------------------


def is_error(result: Any) -> bool:
    """Check if an API response dictionary denotes an error condition."""
    if not isinstance(result, dict):
        return True
    if result.get("error") is not None:
        return True
    if result.get("success") is False:
        return True
    if result.get("status") in ("DEGRADED", "FAILED", "CONFLICT", "NOT_FOUND", "UNREACHABLE"):
        return True
    return False


def error_message(result: Any) -> Optional[str]:
    """Extract a user-facing error message from a response dictionary."""
    if not isinstance(result, dict):
        return "Invalid response format from service bridge."
    return result.get("error") or result.get("detail") or result.get("message")


def _safe_quote(identifier: Union[str, int]) -> str:
    """Safely URL-encode path parameters."""
    return urllib.parse.quote(str(identifier).strip(), safe="")


def _make_error(message: str, code: int = 503, status_label: str = "UNREACHABLE") -> Dict[str, Any]:
    """Construct a standardized error structure."""
    return {
        "success": False,
        "status": status_label,
        "error": message,
        "detail": message,
        "code": code,
    }


def _empty_health(error_msg: str) -> Dict[str, Any]:
    """Safe fallback dictionary matching SystemStatusResponse."""
    return {
        "status": "DEGRADED",
        "version": "unknown",
        "environment": "unknown",
        "checked_at": None,
        "services": {
            "postgres_oltp": {"status": "DOWN", "latency_ms": 0.0, "detail": error_msg},
            "redis_cache": {"status": "DOWN", "latency_ms": 0.0, "detail": error_msg},
            "exasol_olap": {"status": "DOWN", "latency_ms": 0.0, "detail": error_msg},
        },
        "data_freshness": {
            "user_risk_cache_synced_at": None,
            "golden_customers_synced_at": None,
            "age_seconds": None,
            "stale": True,
        },
        "row_counts": {
            "inventory": None,
            "sales_ledger": None,
            "golden_customers": None,
            "user_risk_cache": None,
            "retention_campaign_audit": None,
        },
        "error": error_msg,
    }


def _empty_inventory(error_msg: str) -> Dict[str, Any]:
    """Safe fallback dictionary matching InventoryListResponse."""
    return {
        "generated_at": None,
        "total_items": 0,
        "total_active_carts_5m": 0,
        "redis_available": False,
        "items": [],
        "error": error_msg,
    }


def _empty_investigation(customer_id: str, error_msg: str) -> Dict[str, Any]:
    """Safe fallback dictionary matching IdentityInvestigationResponse."""
    return {
        "golden_customer_id": customer_id,
        "primary_email": None,
        "confidence_score": 0.0,
        "found": False,
        "stitched_identities": [],
        "evidence_trail": [],
        "graph_metrics": {"total_nodes": 0, "total_edges": 0, "density": 0.0},
        "xai_rationale": f"Investigation unavailable: {error_msg}",
        "error": error_msg,
    }


def _empty_return_risk(user_id: str, error_msg: str) -> Dict[str, Any]:
    """Safe fallback dictionary matching ReturnRiskResponse."""
    return {
        "user_id": user_id,
        "found": False,
        "risk_level": 1,
        "return_rate": 0.0,
        "purchases_6m": 0,
        "returns_6m": 0,
        "golden_customer_id": None,
        "computed_at": None,
        "synced_at": None,
        "policy": {
            "level": 1,
            "name": "Standard Free Returns",
            "customer_message": "Free returns with an instant prepaid label.",
            "restocking_fee_usd": 0.0,
            "cash_on_delivery_allowed": True,
            "keep_it_discount_pct": 0,
        },
        "xai_explanation": f"Return risk unavailable: {error_msg}",
        "staleness_warning": False,
        "human_review_required": False,
        "error": error_msg,
    }


def _empty_alerts(error_msg: str, since_hours: int = 24) -> Dict[str, Any]:
    """Safe fallback dictionary matching ChurnAlertsResponse."""
    return {
        "since_hours": since_hours,
        "count_in_window": 0,
        "total_alerts": 0,
        "alerts": [],
        "error": error_msg,
    }


# -----------------------------------------------------------------------------
# Cache Invalidation Helpers
# -----------------------------------------------------------------------------


def _safe_clear_cache(func_obj: Any) -> None:
    """Clear Streamlit cache function safely."""
    try:
        if hasattr(func_obj, "clear"):
            func_obj.clear()
    except Exception as exc:
        logger.debug(f"Could not clear cache for {func_obj}: {exc}")


def clear_all_caches() -> None:
    """Clear all Streamlit data caches used by this bridge."""
    _safe_clear_cache(get_system_health)
    _safe_clear_cache(get_inventory)
    _safe_clear_cache(get_identity_investigation)
    _safe_clear_cache(get_return_risk)
    _safe_clear_cache(get_churn_alerts)


# -----------------------------------------------------------------------------
# Function 1: get_system_health() -> GET /api/v1/system/status
# -----------------------------------------------------------------------------


@st.cache_data(ttl=2)
def get_system_health() -> Dict[str, Any]:
    """
    Fetch comprehensive system health, connectivity, and database row counts.

    Maps to: GET /api/v1/system/status
    Cached for 2 seconds to optimize UI responsiveness.
    """
    url = f"{get_backend_url()}/api/v1/system/status"
    session = _get_http_session()
    try:
        response = session.get(url, timeout=DEFAULT_TIMEOUT)
        if response.status_code == 200:
            data = response.json()
            data.setdefault("error", None)
            return data
        err = f"Backend returned HTTP {response.status_code}: {response.text[:200]}"
        logger.warning(f"get_system_health failed: {err}")
        return _empty_health(err)
    except requests.exceptions.RequestException as exc:
        err = f"Backend unreachable at {url}: {exc}"
        logger.warning(err)
        return _empty_health(err)


# -----------------------------------------------------------------------------
# Function 2: get_inventory() -> GET /api/v1/inventory
# -----------------------------------------------------------------------------


@st.cache_data(ttl=2)
def get_inventory() -> Dict[str, Any]:
    """
    Fetch the catalog with physical stock, OCC versions, safety buffers, and live cart counts.

    Maps to: GET /api/v1/inventory
    Cached for 2 seconds to optimize UI responsiveness.
    """
    url = f"{get_backend_url()}/api/v1/inventory"
    session = _get_http_session()
    try:
        response = session.get(url, timeout=DEFAULT_TIMEOUT)
        if response.status_code == 200:
            data = response.json()
            data.setdefault("error", None)
            return data
        err = f"Backend returned HTTP {response.status_code}: {response.text[:200]}"
        logger.warning(f"get_inventory failed: {err}")
        return _empty_inventory(err)
    except requests.exceptions.RequestException as exc:
        err = f"Backend unreachable at {url}: {exc}"
        logger.warning(err)
        return _empty_inventory(err)


# -----------------------------------------------------------------------------
# Function 3: trigger_surge(item_id) -> POST /api/v1/cart/add (simulates bot traffic)
# -----------------------------------------------------------------------------


def _post_single_cart_add(item_id: str, session_id: str) -> bool:
    """Helper worker to execute a single add-to-cart request."""
    url = f"{get_backend_url()}/api/v1/cart/add"
    payload = {"item_id": item_id, "user_session_id": session_id}
    try:
        resp = _get_http_session().post(url, json=payload, timeout=(1.0, 3.0))
        return resp.status_code == 200
    except Exception:
        return False


def trigger_surge(item_id: str, count: int = 10, max_workers: int = 5) -> Dict[str, Any]:
    """
    Simulate bot traffic surging into carts for a specific SKU.

    Maps to: POST /api/v1/cart/add
    Fires rapid concurrent cart additions with distinct bot session IDs to update
    Redis cart velocity and trigger the predictive safety buffer in real-time.

    Invalidates get_inventory() cache so the dashboard immediately shows new velocity.
    """
    item_id = str(item_id).strip()
    if not item_id:
        return _make_error("item_id is required to trigger surge.", code=400, status_label="BAD_REQUEST")

    burst_count = max(1, count)
    sessions = [f"bot_surge_{uuid.uuid4().hex[:8]}" for _ in range(burst_count)]
    successful = 0

    with ThreadPoolExecutor(max_workers=min(max_workers, burst_count)) as pool:
        futures = [pool.submit(_post_single_cart_add, item_id, s_id) for s_id in sessions]
        for future in as_completed(futures):
            if future.result():
                successful += 1

    # Invalidate cached inventory so UI renders new velocity instantly
    _safe_clear_cache(get_inventory)

    if successful == 0:
        err = f"Failed to simulate bot surge for '{item_id}'. Backend or Redis unreachable."
        logger.warning(err)
        return {
            "success": False,
            "status": "FAILED",
            "item_id": item_id,
            "attempted": burst_count,
            "successful": 0,
            "error": err,
        }

    return {
        "success": True,
        "status": "SURGE_SIMULATED",
        "item_id": item_id,
        "attempted": burst_count,
        "successful": successful,
        "message": f"Successfully simulated {successful}/{burst_count} bot add-to-cart requests for {item_id}.",
        "error": None,
    }


# -----------------------------------------------------------------------------
# Function 4: execute_checkout(payload) -> POST /api/v1/checkout
# -----------------------------------------------------------------------------


def execute_checkout(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Execute order placement under Optimistic Concurrency Control (OCC) and safety buffer rules.

    Maps to: POST /api/v1/checkout
    Payload typically contains: item_id, quantity, user_session_id, expected_version, payment_method.

    Returns the confirmation response on success (200), or a structured conflict/error dictionary
    if stock is depleted, version collision occurs (409), or service is unreachable.

    Invalidates get_inventory() cache on success.
    """
    if not isinstance(payload, dict):
        return _make_error("Invalid payload format; dictionary expected.", code=400, status_label="BAD_REQUEST")

    url = f"{get_backend_url()}/api/v1/checkout"
    session = _get_http_session()

    try:
        response = session.post(url, json=payload, timeout=DEFAULT_TIMEOUT)
        data = response.json() if response.content else {}

        if response.status_code == 200:
            data.setdefault("success", True)
            data.setdefault("error", None)
            # Invalidate inventory cache so remaining stock updates in UI
            _safe_clear_cache(get_inventory)
            return data

        detail = data.get("detail", f"Checkout failed with HTTP {response.status_code}.")
        if response.status_code == 409:
            return {
                "success": False,
                "status": "CONFLICT",
                "error": detail,
                "detail": detail,
                "code": 409,
            }
        elif response.status_code == 404:
            return {
                "success": False,
                "status": "NOT_FOUND",
                "error": detail,
                "detail": detail,
                "code": 404,
            }
        elif response.status_code == 422:
            return {
                "success": False,
                "status": "VALIDATION_ERROR",
                "error": str(detail),
                "detail": detail,
                "code": 422,
            }
        else:
            return {
                "success": False,
                "status": f"HTTP_{response.status_code}",
                "error": detail,
                "detail": detail,
                "code": response.status_code,
            }

    except requests.exceptions.RequestException as exc:
        err = f"Backend unreachable at {url}: {exc}"
        logger.warning(err)
        return _make_error(err, code=503, status_label="UNREACHABLE")


# -----------------------------------------------------------------------------
# Function 5: get_identity_investigation(customer_id) -> GET /api/v1/golden-customers/{customer_id}/investigation
# -----------------------------------------------------------------------------


@st.cache_data(ttl=2)
def get_identity_investigation(customer_id: str) -> Dict[str, Any]:
    """
    Fetch Explainable AI (XAI) Identity Detective investigation evidence and confidence.

    Maps to: GET /api/v1/golden-customers/{customer_id}/investigation
    Cached for 2 seconds to optimize UI responsiveness.
    """
    cust_id = str(customer_id).strip()
    if not cust_id:
        return _empty_investigation("", "Customer ID cannot be empty.")

    safe_id = _safe_quote(cust_id)
    url = f"{get_backend_url()}/api/v1/golden-customers/{safe_id}/investigation"
    session = _get_http_session()

    try:
        response = session.get(url, timeout=DEFAULT_TIMEOUT)
        if response.status_code == 200:
            data = response.json()
            data.setdefault("error", None)
            data.setdefault("found", True)
            return data
        elif response.status_code == 404:
            err = f"Customer '{cust_id}' not found in Golden Record."
            return _empty_investigation(cust_id, err)
        else:
            err = f"HTTP {response.status_code}: {response.text[:200]}"
            return _empty_investigation(cust_id, err)
    except requests.exceptions.RequestException as exc:
        err = f"Backend unreachable at {url}: {exc}"
        logger.warning(err)
        return _empty_investigation(cust_id, err)


# -----------------------------------------------------------------------------
# Function 6: get_return_risk(user_id) -> GET /api/v1/return-risk/{user_id}
# -----------------------------------------------------------------------------


@st.cache_data(ttl=2)
def get_return_risk(user_id: str) -> Dict[str, Any]:
    """
    Fetch pre-computed return risk level (1-5), XAI explanation, and dynamic policy for a shopper.

    Maps to: GET /api/v1/return-risk/{user_id}
    Cached for 2 seconds to optimize UI responsiveness.
    """
    uid = str(user_id).strip()
    if not uid:
        return _empty_return_risk("", "User ID cannot be empty.")

    safe_uid = _safe_quote(uid)
    url = f"{get_backend_url()}/api/v1/return-risk/{safe_uid}"
    session = _get_http_session()

    try:
        response = session.get(url, timeout=DEFAULT_TIMEOUT)
        if response.status_code == 200:
            data = response.json()
            data.setdefault("error", None)
            return data
        err = f"Backend returned HTTP {response.status_code}: {response.text[:200]}"
        logger.warning(f"get_return_risk failed: {err}")
        return _empty_return_risk(uid, err)
    except requests.exceptions.RequestException as exc:
        err = f"Backend unreachable at {url}: {exc}"
        logger.warning(err)
        return _empty_return_risk(uid, err)


# -----------------------------------------------------------------------------
# Function 7: get_churn_alerts() -> GET /api/v1/churn/alerts
# -----------------------------------------------------------------------------


@st.cache_data(ttl=2)
def get_churn_alerts(
    limit: int = 50,
    since_hours: int = 24,
    min_score: float = 0.0,
) -> Dict[str, Any]:
    """
    Fetch recent VIP churn alerts generated by Exasol NLP sentiment scoring.

    Maps to: GET /api/v1/churn/alerts
    Supports limit, since_hours, and min_score query parameters.
    Cached for 2 seconds to optimize UI responsiveness.
    """
    params = {
        "limit": max(1, min(limit, 200)),
        "since_hours": max(1, min(since_hours, 2160)),
        "min_score": max(0.0, min(min_score, 1.0)),
    }
    url = f"{get_backend_url()}/api/v1/churn/alerts"
    session = _get_http_session()

    try:
        response = session.get(url, params=params, timeout=DEFAULT_TIMEOUT)
        if response.status_code == 200:
            data = response.json()
            data.setdefault("error", None)
            return data
        err = f"Backend returned HTTP {response.status_code}: {response.text[:200]}"
        logger.warning(f"get_churn_alerts failed: {err}")
        return _empty_alerts(err, since_hours=since_hours)
    except requests.exceptions.RequestException as exc:
        err = f"Backend unreachable at {url}: {exc}"
        logger.warning(err)
        return _empty_alerts(err, since_hours=since_hours)


# -----------------------------------------------------------------------------
# Function 8: approve_churn_action(ticket_id, user) -> POST /api/v1/churn/alerts/{ticket_id}/approve
# -----------------------------------------------------------------------------


def approve_churn_action(ticket_id: str, user: str = "supervisor") -> Dict[str, Any]:
    """
    Approve and execute a recommended VIP retention action (governance & idempotency).

    Maps to: POST /api/v1/churn/alerts/{ticket_id}/approve
    Passes {"approved_by": user} in the JSON body.
    Invalidates get_churn_alerts() cache upon execution so the table updates immediately.
    """
    t_id = str(ticket_id).strip()
    if not t_id:
        return _make_error("ticket_id is required for approval.", code=400, status_label="BAD_REQUEST")

    safe_tid = _safe_quote(t_id)
    url = f"{get_backend_url()}/api/v1/churn/alerts/{safe_tid}/approve"
    payload = {"approved_by": str(user).strip() or "supervisor"}
    session = _get_http_session()

    try:
        response = session.post(url, json=payload, timeout=DEFAULT_TIMEOUT)
        data = response.json() if response.content else {}

        if response.status_code == 200:
            data.setdefault("success", True)
            data.setdefault("error", None)
            # Invalidate churn alerts cache so updated status renders instantly
            _safe_clear_cache(get_churn_alerts)
            return data
        elif response.status_code == 404:
            err = f"Alert ticket '{t_id}' not found."
            return {
                "ticket_id": t_id,
                "status": "NOT_FOUND",
                "success": False,
                "error": err,
                "message": err,
                "code": 404,
            }
        else:
            err = data.get("detail", f"Approval failed with HTTP {response.status_code}.")
            return {
                "ticket_id": t_id,
                "status": "FAILED",
                "success": False,
                "error": err,
                "message": err,
                "code": response.status_code,
            }
    except requests.exceptions.RequestException as exc:
        err = f"Backend unreachable at {url}: {exc}"
        logger.warning(err)
        return {
            "ticket_id": t_id,
            "status": "UNREACHABLE",
            "success": False,
            "error": err,
            "message": err,
            "code": 503,
        }
