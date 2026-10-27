"""
Live data bridge between the Streamlit dashboard and the OmniSync FastAPI backend.

Every call the UI makes to the backend goes through this module, so the pages never deal with HTTP, timeouts or
error shapes themselves.

Error contract (nothing here ever raises)
-----------------------------------------
* Read functions (GET) return the backend's JSON on success. When the backend cannot be reached, answers with an
  error, or sends something unreadable, they return an *empty structure with the same keys the UI reads*
  (for example ``{"items": [], "total_items": 0, ...}``) plus an ``"_error"`` entry. Pages can render the empty
  structure directly and show ``error_message(result)`` when ``is_error(result)`` is true.
* Action functions (POST) return a result dict with ``"ok"`` and a human-readable ``"message"``. When ``ok`` is
  false an ``"_error"`` entry explains why. Business outcomes such as a 409 from checkout are normal results,
  not crashes, and ``execute_checkout`` classifies them in ``"outcome"``.

``_error`` is ``{"kind", "status_code", "message", "detail"}`` where ``kind`` is one of ``unreachable``,
``timeout``, ``http_error``, ``invalid_response`` or ``request_error``.

Caching: GET functions are wrapped in ``st.cache_data(ttl=2)`` so a page that reruns several times a second does not
flood the API. Successful actions clear the affected caches, so the screen reflects a checkout or an approval at once.
"""

import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Mapping, Optional, Tuple
from urllib.parse import quote

import requests
import streamlit as st
from requests.adapters import HTTPAdapter

logger = logging.getLogger("omnisync.frontend.bridge")

# --------------------------------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------------------------------
API_BASE_URL = os.getenv("BACKEND_API_BASE_URL", "http://api:8000").rstrip("/")
API_PREFIX = "/api/v1"

READ_TIMEOUT = (2, 5)  # (connect, read) seconds: a dead backend is reported within a few seconds
WRITE_TIMEOUT = (2, 15)  # checkout and approvals may legitimately take longer
CACHE_TTL_SECONDS = 2
MAX_SURGE_BOTS = 500
SURGE_WORKERS = 16

ERROR_KEY = "_error"

__all__ = [
    "API_BASE_URL",
    "approve_churn_action",
    "clear_all_caches",
    "error_message",
    "execute_checkout",
    "get_backend_url",
    "get_churn_alerts",
    "get_identity_investigation",
    "get_inventory",
    "get_return_risk",
    "get_system_health",
    "is_error",
    "set_backend_url",
    "trigger_surge",
]


# --------------------------------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------------------------------
def _make_error(kind: str, message: str, status_code: Optional[int] = None, detail: Any = None) -> Dict[str, Any]:
    return {"kind": kind, "status_code": status_code, "message": message, "detail": detail}


def is_error(result: Any) -> bool:
    """True when a bridge result carries an error marker."""
    return isinstance(result, Mapping) and ERROR_KEY in result


def error_message(result: Any, default: str = "") -> str:
    """Return a readable error message from a failed result."""
    if not isinstance(result, Mapping):
        return default

    error = result.get(ERROR_KEY)

    if isinstance(error, Mapping):
        return str(error.get("message") or "Unexpected error.")

    if isinstance(error, str) and error:
        return error

    plain_error = result.get("error")
    if isinstance(plain_error, Mapping):
        message = plain_error.get("message")
        if message:
            return str(message)
    elif plain_error:
        return str(plain_error)

    if result.get("ok") is False and result.get("message"):
        return str(result["message"])

    if result.get("success") is False and result.get("message"):
        return str(result["message"])

    return default


def _detail_message(body: Any, response: requests.Response) -> str:
    """Turn FastAPI's `{"detail": ...}` (a string, or a list of validation errors) into one readable sentence."""
    detail = body.get("detail") if isinstance(body, Mapping) else None
    if isinstance(detail, str) and detail:
        return detail
    if isinstance(detail, list):
        parts = []
        for item in detail:
            if isinstance(item, Mapping):
                location = ".".join(str(p) for p in item.get("loc", []) if p != "body")
                parts.append(f"{location}: {item.get('msg')}" if location else str(item.get("msg")))
            else:
                parts.append(str(item))
        if parts:
            return "; ".join(parts)
    return f"The backend answered HTTP {response.status_code} {response.reason or ''}".strip()


# --------------------------------------------------------------------------------------------------------------
# HTTP plumbing
# --------------------------------------------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def _session() -> requests.Session:
    """One pooled HTTP session for the whole app (keeps connections open; sized for the surge simulator)."""
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=4, pool_maxsize=SURGE_WORKERS * 2)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({"Accept": "application/json", "User-Agent": "omnisync-dashboard"})
    return session


def _url(path: str) -> str:
    return f"{API_BASE_URL}{API_PREFIX}{path}"


def _segment(value: str) -> str:
    """Make an id safe to place in a URL path (ids can contain '@', '/', ':' ...)."""
    return quote(str(value), safe="")


def _call(
    method: str,
    path: str,
    *,
    params: Optional[Mapping[str, Any]] = None,
    body: Optional[Mapping[str, Any]] = None,
    timeout: Tuple[float, float] = READ_TIMEOUT,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[int]]:
    """
    Send one request. Returns ``(data, error, status_code)``: exactly one of `data` / `error` is set.
    `status_code` is None when no HTTP answer was received.
    """
    url = _url(path)
    try:
        response = _session().request(method, url, params=params, json=body, timeout=timeout)
    except requests.exceptions.Timeout:
        logger.warning("Backend timed out: %s %s", method, url)
        return None, _make_error("timeout", "The backend did not answer in time. Try again in a moment."), None
    except requests.exceptions.ConnectionError:
        logger.warning("Backend unreachable: %s %s", method, url)
        return None, _make_error("unreachable", f"Cannot reach the backend at {API_BASE_URL}. Is it running?"), None
    except requests.exceptions.RequestException as exc:
        logger.warning("Request failed: %s %s: %s", method, url, exc)
        return None, _make_error("request_error", f"The request could not be sent: {type(exc).__name__}."), None

    try:
        parsed = response.json()
    except ValueError:
        parsed = None

    if not response.ok:
        message = _detail_message(parsed, response)
        logger.info("Backend returned %s for %s %s: %s", response.status_code, method, url, message)
        return None, _make_error("http_error", message, response.status_code, parsed), response.status_code

    if not isinstance(parsed, dict):
        logger.warning("Unreadable backend response for %s %s", method, url)
        return (
            None,
            _make_error(
                "invalid_response", "The backend sent a response the dashboard cannot read.", response.status_code
            ),
            response.status_code,
        )
    return parsed, None, response.status_code


def _read(path: str, empty: Callable[[], Dict[str, Any]], params: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """GET helper: the data on success, otherwise a fresh empty structure carrying the error."""
    data, error, _ = _call("GET", path, params=params)
    if error is None and data is not None:
        return data
    fallback = empty()
    fallback[ERROR_KEY] = error
    return fallback


def _invalidate(*cached: Any) -> None:
    """Drop cached reads after an action changed the data behind them."""
    for function in cached:
        try:
            function.clear()
        except Exception as exc:  # a cache problem must never fail an action that already succeeded
            logger.debug("Could not clear cache for %s: %s", getattr(function, "__name__", function), exc)


def clear_all_caches() -> None:
    """Clear all cached backend reads."""
    _invalidate(
        get_system_health,
        get_inventory,
        get_identity_investigation,
        get_return_risk,
        get_churn_alerts,
    )


def get_backend_url() -> str:
    """Return the configured backend base URL."""
    return API_BASE_URL


def set_backend_url(url: str) -> None:
    """Update the backend base URL and clear cached reads."""
    global API_BASE_URL

    API_BASE_URL = url.rstrip("/")
    clear_all_caches()


# --------------------------------------------------------------------------------------------------------------
# Empty structures (what the UI renders when the backend cannot answer)
# --------------------------------------------------------------------------------------------------------------
def _empty_health() -> Dict[str, Any]:
    return {
        "status": "UNREACHABLE",
        "version": None,
        "environment": None,
        "checked_at": None,
        "services": {},
        "data_freshness": {
            "user_risk_cache_synced_at": None,
            "golden_customers_synced_at": None,
            "age_seconds": None,
            "stale": True,
        },
        "row_counts": {},
    }


def _empty_inventory() -> Dict[str, Any]:
    return {
        "generated_at": None,
        "total_items": 0,
        "total_active_carts_5m": None,
        "redis_available": False,
        "items": [],
    }


def _empty_investigation() -> Dict[str, Any]:
    return {
        "found": False,
        "confidence_score": 0,
        "supporting_evidence": [],
        "counter_evidence": [],
        "decision_status": "UNAVAILABLE",
    }


def _empty_return_risk(user_id: str) -> Dict[str, Any]:
    # risk_level and policy are None, not level 1: showing "free returns" when we simply do not know would mislead.
    return {
        "user_id": user_id,
        "found": False,
        "risk_level": None,
        "return_rate": None,
        "purchases_6m": None,
        "returns_6m": None,
        "golden_customer_id": None,
        "computed_at": None,
        "synced_at": None,
        "policy": None,
        "xai_explanation": None,
        "staleness_warning": False,
        "human_review_required": False,
    }


def _empty_alerts(since_hours: int) -> Dict[str, Any]:
    return {"since_hours": since_hours, "count_in_window": 0, "total_alerts": 0, "alerts": []}


# --------------------------------------------------------------------------------------------------------------
# Reads (cached for CACHE_TTL_SECONDS)
# --------------------------------------------------------------------------------------------------------------
@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_system_health() -> Dict[str, Any]:
    """Live PostgreSQL / Redis / Exasol status, sync freshness and row counts (`GET /system/status`)."""
    return _read("/system/status", _empty_health)


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_inventory() -> Dict[str, Any]:
    """Every SKU with stock, OCC version, safety buffer and live cart velocity (`GET /inventory`)."""
    return _read("/inventory", _empty_inventory)


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_identity_investigation(customer_id: str) -> Dict[str, Any]:
    """Explainable identity evidence and confidence for one golden customer (`GET .../{id}/investigation`)."""
    return _read(f"/golden-customers/{_segment(customer_id)}/investigation", _empty_investigation)


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_return_risk(user_id: str) -> Dict[str, Any]:
    """Pre-computed return risk level, policy and rationale for a shopper (`GET /return-risk/{user_id}`)."""
    return _read(f"/return-risk/{_segment(user_id)}", lambda: _empty_return_risk(user_id))


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_churn_alerts(limit: int = 50, since_hours: int = 24, min_score: float = 0.0) -> Dict[str, Any]:
    """Recent VIP churn alerts, newest first (`GET /churn/alerts`)."""
    params = {"limit": limit, "since_hours": since_hours, "min_score": min_score}
    return _read("/churn/alerts", lambda: _empty_alerts(since_hours), params=params)


# --------------------------------------------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------------------------------------------
def _failed(error: Dict[str, Any], **extra: Any) -> Dict[str, Any]:
    return {"ok": False, "message": error["message"], ERROR_KEY: error, **extra}


def trigger_surge(item_id: str, bots: int = 1) -> Dict[str, Any]:
    """
    Simulate shoppers adding an item to their cart (`POST /cart/add`), one fresh bot session per request.

    Args:
        item_id: SKU to add.
        bots: number of simulated shoppers, 1 to 500.

    Returns:
        ``{"ok", "message", "requested", "succeeded", "active_carts_5m", "failures"}``. `ok` is true when at least one
        add succeeded; ``_error`` is only set when every request failed.
    """
    if not item_id or not str(item_id).strip():
        return _failed(_make_error("request_error", "Choose an item first."), requested=0, succeeded=0)
    count = max(1, min(int(bots), MAX_SURGE_BOTS))

    def add_one(_: int) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        payload = {"item_id": item_id, "user_session_id": f"bot-{uuid.uuid4().hex[:12]}"}
        data, error, _status = _call("POST", "/cart/add", body=payload, timeout=WRITE_TIMEOUT)
        return data, error

    with ThreadPoolExecutor(max_workers=min(SURGE_WORKERS, count)) as pool:
        outcomes = list(pool.map(add_one, range(count)))

    successes = [data for data, error in outcomes if error is None and data is not None]
    failures = [error for _, error in outcomes if error is not None]
    if successes:
        _invalidate(get_inventory)

    active = max((s.get("active_carts_5m", 0) for s in successes), default=None)
    result: Dict[str, Any] = {
        "ok": bool(successes),
        "message": (
            f"Added {len(successes)} of {count} simulated shoppers to the cart."
            if successes
            else failures[0]["message"]
        ),
        "requested": count,
        "succeeded": len(successes),
        "active_carts_5m": active,
        "failures": failures[:5],
    }
    if not successes:
        result[ERROR_KEY] = failures[0]
    return result


def _checkout_outcome(status_code: Optional[int], error: Optional[Dict[str, Any]]) -> str:
    if error is None:
        return "SUCCESS"
    if status_code is None:
        return "TIMEOUT" if error["kind"] == "timeout" else "UNREACHABLE"
    if status_code == 409:
        text = error["message"].lower()
        return "RESERVED_SAFETY_BUFFER" if ("high demand" in text or "reserved" in text) else "OCC_CONFLICT"
    return {404: "NOT_FOUND", 422: "INVALID_REQUEST"}.get(status_code, "SERVER_ERROR")


def execute_checkout(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """
    Place an order (`POST /checkout`).

    Args:
        payload: the CheckoutRequest fields, e.g. ``{"item_id", "quantity", "user_session_id", "user_id", ...}``.

    Returns:
        ``{"ok", "outcome", "status_code", "message", "data"}``. `outcome` is one of SUCCESS, RESERVED_SAFETY_BUFFER
        (HTTP 409, predictive buffer), OCC_CONFLICT (HTTP 409, version collision), NOT_FOUND, INVALID_REQUEST,
        SERVER_ERROR, TIMEOUT or UNREACHABLE. `data` holds the backend response on success.
    """
    if not isinstance(payload, Mapping):
        error = _make_error("request_error", "The checkout request must be a set of fields.")
        return _failed(error, outcome="INVALID_REQUEST", status_code=None, data={})

    data, error, status_code = _call("POST", "/checkout", body=payload, timeout=WRITE_TIMEOUT)
    outcome = _checkout_outcome(status_code, error)
    if error is not None:
        return _failed(error, outcome=outcome, status_code=status_code, data={})

    _invalidate(get_inventory, get_return_risk)
    return {
        "ok": True,
        "outcome": outcome,
        "status_code": status_code,
        "message": data.get("message", "Order placed."),
        "data": data,
    }


def approve_churn_action(ticket_id: str, user: str) -> Dict[str, Any]:
    """
    Approve and execute the recommended retention action for an alert (`POST /churn/alerts/{ticket_id}/approve`).

    Approving twice is safe: the backend answers 200 "Action already executed.", reported here as
    ``already_executed: True``.

    Returns:
        ``{"ok", "message", "already_executed", "data"}``; ``_error`` is set when the approval failed.
    """
    if not ticket_id or not str(ticket_id).strip() or not user or not str(user).strip():
        error = _make_error("request_error", "A ticket and the approving operator are both required.")
        return _failed(error, already_executed=False, data={})

    data, error, _status = _call(
        "POST", f"/churn/alerts/{_segment(ticket_id)}/approve", body={"approved_by": user}, timeout=WRITE_TIMEOUT
    )
    if error is not None:
        message = "That alert no longer exists." if error["status_code"] == 404 else error["message"]
        return _failed({**error, "message": message}, already_executed=False, data={})

    _invalidate(get_churn_alerts)
    message = data.get("message", "Action approved.")
    return {"ok": True, "message": message, "already_executed": "already executed" in message.lower(), "data": data}
