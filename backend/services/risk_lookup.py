"""
Pre-computed Return Risk Level lookup for the transactional path.

The level (1-5) is calculated in Exasol by the serial-returner batch job and copied into the PostgreSQL
`user_risk_cache` table by Celery Beat. At checkout this module does ONE primary-key read of that table:
no analytics query runs and Exasol is never contacted, so the request adds no model or warehouse latency.
"""

import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

logger = logging.getLogger("omnisync.risk_lookup")

# Level 1 = standard free returns. Used for customers with no cached score yet (new shoppers, or the
# cache has not been synced), so a missing score never adds friction.
DEFAULT_RISK_LEVEL = 1
MIN_RISK_LEVEL = 1
MAX_RISK_LEVEL = 5


def get_return_risk_level(db: Session, user_id: Optional[str]) -> int:
    """
    Read the pre-computed Return Risk Level for a Shopify user id or guest device id.

    Never raises: if the cache table is unavailable the default level is returned and the failure is
    logged, because a cache problem must not break checkout.
    """
    if not user_id:
        return DEFAULT_RISK_LEVEL
    try:
        level = db.execute(
            text("SELECT risk_level FROM user_risk_cache WHERE user_id = :user_id"),
            {"user_id": user_id},
        ).scalar()
    except SQLAlchemyError as exc:
        db.rollback()  # leave the session usable for the rest of the request
        logger.warning("Return risk lookup failed, using level %d: %s", DEFAULT_RISK_LEVEL, exc)
        return DEFAULT_RISK_LEVEL
    if level is None:
        return DEFAULT_RISK_LEVEL
    return min(MAX_RISK_LEVEL, max(MIN_RISK_LEVEL, int(level)))
