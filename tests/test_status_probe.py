"""The sidebar's patient status probe: answers the same shape as the bridge and never raises."""

from unittest.mock import MagicMock, patch

import requests

from frontend.services import status_probe


def fetch(response=None, error=None):
    status_probe.get_system_health.clear()
    with patch.object(status_probe.requests, "get", side_effect=error, return_value=response):
        return status_probe.get_system_health()


def reply(status=200, payload=None, json_error=False):
    r = MagicMock(status_code=status)
    if json_error:
        r.json.side_effect = ValueError("no json")
    else:
        r.json.return_value = payload
    return r


def test_success_passes_the_api_answer_through():
    health = fetch(reply(payload={"status": "DEGRADED", "services": {"exasol_olap": {"status": "DOWN"}}}))
    assert health["error"] is None
    assert health["services"]["exasol_olap"]["status"] == "DOWN"


def test_uses_a_longer_read_timeout_than_the_bridge():
    from frontend.services.live_bridge import READ_TIMEOUT

    assert status_probe.STATUS_TIMEOUT[1] > READ_TIMEOUT[1]


def test_timeout_is_reported_not_raised():
    health = fetch(error=requests.exceptions.Timeout())
    assert "in time" in health["error"] and health["services"] == {}


def test_connection_failure_is_reported_not_raised():
    assert "Cannot reach" in fetch(error=requests.exceptions.ConnectionError())["error"]


def test_bad_http_status_and_bad_body_are_reported():
    assert "HTTP 500" in fetch(reply(status=500))["error"]
    assert "JSON" in fetch(reply(json_error=True))["error"]
    assert "unexpected" in fetch(reply(payload=["not", "a", "dict"]))["error"]
