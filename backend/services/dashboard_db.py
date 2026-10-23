"""
Small PostgreSQL read helpers shared by the dashboard (read-only) API endpoints.

The dashboard tables (`user_risk_cache`, `golden_customers`, `golden_customer_members`) are filled by the Celery
Beat sync from Exasol, so these helpers only ever read. If a table does not exist yet (bootstrap not run) or the
database is unreachable, they raise HTTP 503 with a clear message instead of a stack trace.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

logger = logging.getLogger("omnisync.dashboard")

UNAVAILABLE_DETAIL = (
    "Dashboard data is unavailable: the database is unreachable or its tables are not initialised. "
    "Run `python -m scripts.bootstrap` and wait for the first Exasol -> PostgreSQL sync."
)


def _unavailable(db: Session, exc: SQLAlchemyError) -> HTTPException:
    db.rollback()  # leave the session usable
    logger.warning("Dashboard query failed: %s", exc)
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=UNAVAILABLE_DETAIL)


def fetch_all(db: Session, sql: str, params: Optional[Dict[str, Any]] = None) -> List[Any]:
    try:
        return list(db.execute(text(sql), params or {}).fetchall())
    except SQLAlchemyError as exc:
        raise _unavailable(db, exc)


def fetch_one(db: Session, sql: str, params: Optional[Dict[str, Any]] = None) -> Optional[Any]:
    try:
        return db.execute(text(sql), params or {}).fetchone()
    except SQLAlchemyError as exc:
        raise _unavailable(db, exc)


def ltv_tier(percent_rank: float) -> str:
    """
    Lifetime-value tier from a customer's percent rank by spend (0 = lowest spender, 1 = highest).
    Same breakpoints as the VIP churn radar; the PRD's VIP threshold is the top 20% (rank >= 0.80).
    """
    if percent_rank >= 0.99:
        return "Top 1% VIP"
    if percent_rank >= 0.95:
        return "Top 5% VIP"
    if percent_rank >= 0.90:
        return "Top 10% VIP"
    if percent_rank >= 0.80:
        return "Top 20% VIP"
    return "Standard"


def percent_rank_for_spend(db: Session, total_spend: float) -> float:
    """PERCENT_RANK of a spend value among all golden customers: customers spending less / (customers - 1)."""
    row = fetch_one(
        db,
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN total_spend_usd < :spend THEN 1 ELSE 0 END) AS below FROM golden_customers",
        {"spend": total_spend},
    )
    total, below = int(row[0] or 0), int(row[1] or 0)
    return below / (total - 1) if total > 1 else 1.0
