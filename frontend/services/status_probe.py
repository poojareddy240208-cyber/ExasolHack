"""
Patient system-status probe for the sidebar.

`GET /system/status` checks PostgreSQL, Redis and Exasol one after another. When Exasol is switched off the API waits
about four seconds for it, which is longer than the shared bridge's read timeout, so the sidebar used to report every
service as DOWN. This file asks the same endpoint with a longer read timeout and leaves `live_bridge.py` untouched.
"""

from typing import Any, Dict

import requests
import streamlit as st

from frontend.services.live_bridge import API_BASE_URL, API_PREFIX

STATUS_TIMEOUT = (2, 20)  # (connect, read) seconds; the API itself gives up on a dead Exasol after about 4 s
CACHE_TTL_SECONDS = 3

__all__ = ["get_system_health"]


def _unreachable(message: str) -> Dict[str, Any]:
    return {"status": "UNKNOWN", "error": message, "services": {}, "data_freshness": {}}


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_system_health() -> Dict[str, Any]:
    """Same shape as the bridge's `get_system_health`: `error` is None when the API answered."""
    url = f"{API_BASE_URL}{API_PREFIX}/system/status"
    try:
        response = requests.get(url, timeout=STATUS_TIMEOUT)
    except requests.exceptions.Timeout:
        return _unreachable("The backend did not answer the status check in time.")
    except requests.exceptions.RequestException:
        return _unreachable("Cannot reach the backend.")
    if response.status_code != 200:
        return _unreachable(f"The status check returned HTTP {response.status_code}.")
    try:
        data = response.json()
    except ValueError:
        return _unreachable("The status check did not return JSON.")
    if not isinstance(data, dict):
        return _unreachable("The status check returned an unexpected answer.")
    data.setdefault("services", {})
    data["error"] = None
    return data
