"""
Unit tests for Redis Cart Velocity Tracker Service.
Validates live cart event tracking, 5-minute rolling window pruning, and ZSET velocity metrics.
"""

import time
from typing import Dict
import pytest
from backend.services.cart_tracker import CartVelocityTracker


class MockRedisZSet:
    """
    Mock implementation of Redis Sorted Set operations for fast, hermetic unit tests.
    """

    def __init__(self):
        # Maps key -> dict of {member: score}
        self.data: Dict[str, Dict[str, float]] = {}

    def zadd(self, key: str, mapping: Dict[str, float]):
        if key not in self.data:
            self.data[key] = {}
        for member, score in mapping.items():
            self.data[key][member] = float(score)
        return len(mapping)

    def zrem(self, key: str, *members: str):
        if key not in self.data:
            return 0
        removed = 0
        for m in members:
            if m in self.data[key]:
                del self.data[key][m]
                removed += 1
        return removed

    def zremrangebyscore(self, key: str, min_score: float, max_score: float):
        if key not in self.data:
            return 0

        # Parse -inf
        min_val = float("-inf") if min_score == "-inf" else float(min_score)
        max_val = float("inf") if max_score == "+inf" else float(max_score)

        to_remove = [member for member, score in self.data[key].items() if min_val <= score <= max_val]
        for m in to_remove:
            del self.data[key][m]
        return len(to_remove)

    def zcard(self, key: str):
        if key not in self.data:
            return 0
        return len(self.data[key])

    def zrange(self, key: str, start: int, end: int):
        if key not in self.data:
            return []
        # Sorted by score ascending
        sorted_members = sorted(self.data[key].keys(), key=lambda m: self.data[key][m])
        if end == -1:
            return sorted_members[start:]
        return sorted_members[start : end + 1]

    def delete(self, key: str):
        if key in self.data:
            del self.data[key]
            return 1
        return 0


@pytest.fixture
def mock_redis():
    return MockRedisZSet()


@pytest.fixture
def tracker(mock_redis):
    return CartVelocityTracker(redis_client=mock_redis)


def test_add_to_cart(tracker, mock_redis):
    """Verifies adding a session to an item's cart ZSET with a timestamp score."""
    item_id = "SKU-IPHONE-16"
    session_id = "sess_user_001"
    now = 1700000000.0

    tracker.add_to_cart(item_id, session_id, timestamp=now)

    key = f"cart:{item_id}"
    assert key in mock_redis.data
    assert session_id in mock_redis.data[key]
    assert mock_redis.data[key][session_id] == now


def test_remove_from_cart(tracker, mock_redis):
    """Verifies removing a session from an item's cart ZSET."""
    item_id = "SKU-IPHONE-16"
    session_1 = "sess_user_001"
    session_2 = "sess_user_002"

    tracker.add_to_cart(item_id, session_1)
    tracker.add_to_cart(item_id, session_2)
    assert tracker.get_cart_velocity(item_id) == 2

    tracker.remove_from_cart(item_id, session_1)
    assert tracker.get_cart_velocity(item_id) == 1
    assert session_1 not in mock_redis.data[f"cart:{item_id}"]


def test_cart_velocity_5_minute_window_pruning(tracker, mock_redis):
    """
    Verifies that get_cart_velocity drops entries older than 300 seconds (5 minutes)
    using ZREMRANGEBYSCORE and returns the exact active count.
    """
    item_id = "SKU-FLASH-SALE-ITEM"
    current_time = time.time()

    # Add 3 active sessions within the last 5 minutes (e.g., now - 60s, now - 120s, now - 10s)
    tracker.add_to_cart(item_id, "active_sess_1", timestamp=current_time - 60)
    tracker.add_to_cart(item_id, "active_sess_2", timestamp=current_time - 120)
    tracker.add_to_cart(item_id, "active_sess_3", timestamp=current_time - 10)

    # Add 2 stale sessions older than 5 minutes (e.g., now - 301s, now - 600s)
    tracker.add_to_cart(item_id, "stale_sess_1", timestamp=current_time - 301)
    tracker.add_to_cart(item_id, "stale_sess_2", timestamp=current_time - 600)

    # Prior to get_cart_velocity, raw set has 5 entries
    assert mock_redis.zcard(f"cart:{item_id}") == 5

    # Calling get_cart_velocity should purge stale sessions and return exactly 3
    velocity = tracker.get_cart_velocity(item_id)
    assert velocity == 3

    # Ensure pruned items were removed from Redis
    remaining_members = mock_redis.zrange(f"cart:{item_id}", 0, -1)
    assert set(remaining_members) == {"active_sess_1", "active_sess_2", "active_sess_3"}
    assert "stale_sess_1" not in remaining_members
    assert "stale_sess_2" not in remaining_members


def test_get_active_sessions(tracker):
    """Verifies fetching active session list directly."""
    item_id = "SKU-HOODIE"
    current_time = time.time()

    tracker.add_to_cart(item_id, "user_a", timestamp=current_time - 10)
    tracker.add_to_cart(item_id, "user_b", timestamp=current_time - 50)
    tracker.add_to_cart(item_id, "user_c", timestamp=current_time - 400)  # stale

    active = tracker.get_active_sessions(item_id)
    assert len(active) == 2
    assert "user_a" in active
    assert "user_b" in active
    assert "user_c" not in active
