"""
Webhook resilience tests: a dead Redis broker must produce a fast 503, never a hang and never a fake "queued".

Real-broker timing (not run here because it needs a reachable network stack): with these settings a dead
Redis answered 503 in about 3-7 s on a machine where the hostname `redis` does not resolve, versus 79 s before.
"""

import inspect
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from kombu.exceptions import OperationalError

import backend.api.v1.endpoints.webhooks as webhooks
from backend.celery_app import celery, process_zendesk_ticket_task
from backend.main import app

PAYLOAD = {"ticket_id": "ZD-RESILIENCE-1", "subject": "Late order", "description": "Where is it?"}


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


def test_handler_is_a_plain_function_so_the_event_loop_is_never_blocked():
    assert not inspect.iscoroutinefunction(webhooks.handle_zendesk_webhook)


@pytest.mark.parametrize(
    "error", [OperationalError("redis down"), TimeoutError("slow"), ConnectionRefusedError("refused"), OSError("net")]
)
def test_broker_failure_returns_503_not_a_fake_queued(client, error):
    with patch.object(process_zendesk_ticket_task, "apply_async", side_effect=error):
        response = client.post("/api/v1/webhooks/zendesk", json=PAYLOAD)
    assert response.status_code == 503
    assert "task_id" not in response.json()


def test_unexpected_error_does_not_leak_internal_details(client):
    with patch.object(process_zendesk_ticket_task, "apply_async", side_effect=RuntimeError("secret-internal-detail")):
        response = client.post("/api/v1/webhooks/zendesk", json=PAYLOAD)
    assert response.status_code == 503
    assert "secret-internal-detail" not in response.text


def test_publish_is_bounded_so_an_outage_cannot_hang_the_request(client):
    fake_task = MagicMock(id="task-123")
    with patch.object(process_zendesk_ticket_task, "apply_async", return_value=fake_task) as publish:
        response = client.post("/api/v1/webhooks/zendesk", json=PAYLOAD)
    assert response.status_code == 202
    assert response.json()["task_id"] == "task-123"
    kwargs = publish.call_args.kwargs
    assert kwargs["retry"] is True
    assert kwargs["retry_policy"]["max_retries"] <= 1
    assert kwargs["expires"] == 10


def test_task_ignores_results_and_broker_connects_fast():
    """ignore_result stops the publish from subscribing to the Redis result backend (the 79 s hang)."""
    assert process_zendesk_ticket_task.ignore_result is True
    assert celery.conf.broker_connection_timeout <= 4
