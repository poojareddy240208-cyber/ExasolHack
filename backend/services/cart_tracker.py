"""
Cart Velocity Service - In-Memory Event Cache (Redis).

Tracks active cart events across customer sessions using Redis Sorted Sets (ZSET).
Enables the Virtual Safety Stock Buffer formula:
  Buffer = MAX(0, Predicted_Demand_15m - Current_Physical_Stock + Active_Carts_5m)

ZSET Mechanics:
- Key: `cart:{item_id}`
- Member: `user_session_id` (Anonymous guest cookie or authenticated user UUID)
- Score: UNIX timestamp (epoch float/integer) of the add-to-cart event
- Window: 5 minutes (300 seconds) rolling cutoff via ZREMRANGEBYSCORE
"""

import time
import logging
from typing import Optional, Union, Any, List
import redis

from backend.core.config import settings

logger = logging.getLogger("omnisync.cart_tracker")


class CartVelocityTracker:
    """
    Manages live shopping cart velocity using Redis Sorted Sets (ZSETs).
    Provides real-time active cart metrics for predictive stock buffering.
    """

    DEFAULT_WINDOW_SECONDS: int = 300  # 5-minute rolling window

    def __init__(self, redis_client: Optional[Union[redis.Redis, Any]] = None, redis_url: Optional[str] = None):
        """
        Initialize the cart tracker with a Redis client or connection URL.
        If no client is supplied, connects using settings.REDIS_URL.
        """
        if redis_client is not None:
            self.redis = redis_client
        else:
            url = redis_url or settings.REDIS_URL
            self.redis = redis.from_url(url, decode_responses=True)

    @staticmethod
    def _format_key(item_id: str) -> str:
        """Standardized Redis key format for item cart sets."""
        return f"cart:{item_id}"

    def add_to_cart(self, item_id: str, user_session_id: str, timestamp: Optional[float] = None) -> bool:
        """
        Add or update a user session in the item's cart ZSET with a UNIX timestamp score.

        Args:
            item_id: Unique SKU or product identifier.
            user_session_id: Unique identifier for the user session or guest cookie.
            timestamp: Optional epoch timestamp (defaults to current time.time()).

        Returns:
            bool: True if the operation succeeded.
        """
        key = self._format_key(item_id)
        score = timestamp if timestamp is not None else time.time()

        try:
            # ZADD key score member
            self.redis.zadd(key, {user_session_id: score})
            logger.debug(f"Added session {user_session_id} to {key} with score {score}")
            return True
        except Exception as e:
            logger.error(f"Error adding {user_session_id} to {key}: {e}")
            raise

    def remove_from_cart(self, item_id: str, user_session_id: str) -> bool:
        """
        Remove a user session from the item's cart ZSET.

        Args:
            item_id: Unique SKU or product identifier.
            user_session_id: Unique identifier for the user session or guest cookie.

        Returns:
            bool: True if the member was removed (or was already absent).
        """
        key = self._format_key(item_id)
        try:
            # ZREM key member
            removed = self.redis.zrem(key, user_session_id)
            logger.debug(f"Removed session {user_session_id} from {key} (count: {removed})")
            return bool(removed > 0)
        except Exception as e:
            logger.error(f"Error removing {user_session_id} from {key}: {e}")
            raise

    def get_cart_velocity(self, item_id: str, window_seconds: Optional[int] = None) -> int:
        """
        Computes the number of active shopping carts holding this item in the last 5 minutes.

        Execution Steps:
        1. Executes ZREMRANGEBYSCORE to drop entries older than window_seconds (default 300s).
        2. Executes ZCARD to return the active count.

        Args:
            item_id: Unique SKU or product identifier.
            window_seconds: Rolling window in seconds (defaults to 300 / 5 minutes).

        Returns:
            int: Number of active sessions currently holding this item in cart.
        """
        key = self._format_key(item_id)
        window = window_seconds if window_seconds is not None else self.DEFAULT_WINDOW_SECONDS
        cutoff = time.time() - window

        try:
            # Drop scores older than (now - window_seconds)
            self.redis.zremrangebyscore(key, "-inf", cutoff)
            # Count remaining active sessions
            active_count = self.redis.zcard(key)
            logger.debug(f"Cart velocity for {item_id} (window {window}s): {active_count}")
            return int(active_count)
        except Exception as e:
            logger.error(f"Error calculating cart velocity for {key}: {e}")
            raise

    def get_active_sessions(self, item_id: str, window_seconds: Optional[int] = None) -> List[str]:
        """
        Retrieves all active user session IDs currently holding the item.
        Prunes expired sessions prior to returning list.
        """
        key = self._format_key(item_id)
        window = window_seconds if window_seconds is not None else self.DEFAULT_WINDOW_SECONDS
        cutoff = time.time() - window

        try:
            self.redis.zremrangebyscore(key, "-inf", cutoff)
            sessions = self.redis.zrange(key, 0, -1)
            return list(sessions)
        except Exception as e:
            logger.error(f"Error fetching active sessions for {key}: {e}")
            raise

    def clear_cart(self, item_id: str) -> bool:
        """Clears all sessions from the item's cart set."""
        key = self._format_key(item_id)
        try:
            self.redis.delete(key)
            return True
        except Exception as e:
            logger.error(f"Error deleting key {key}: {e}")
            raise


# Alias for flexible naming convention
CartTracker = CartVelocityTracker


def get_cart_tracker() -> CartVelocityTracker:
    """FastAPI dependency yielding CartVelocityTracker instance."""
    return CartVelocityTracker()
