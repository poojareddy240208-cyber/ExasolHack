"""
Pytest configuration for OmniSync test suite.
"""

import pytest
from backend.celery_app import celery


@pytest.fixture(autouse=True)
def configure_celery_for_testing():
    """
    Configures Celery to run in synchronous eager mode during pytest execution,
    preventing network broker connection delays when Redis is offline.
    """
    original_eager = celery.conf.task_always_eager
    original_propagates = celery.conf.task_eager_propagates

    celery.conf.task_always_eager = True
    celery.conf.task_eager_propagates = True
    yield
    celery.conf.task_always_eager = original_eager
    celery.conf.task_eager_propagates = original_propagates
