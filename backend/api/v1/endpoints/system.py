"""
System status endpoint (Architecture & Health dashboard page).

Unlike the static `/health` liveness probes, this one actually contacts PostgreSQL, Redis and Exasol (in parallel, each
with a short timeout) and reports how fresh the Exasol -> PostgreSQL sync is. It always answers HTTP 200 with a
`status` of HEALTHY or DEGRADED so the UI can render a failure instead of an error page.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timezone
from typing import Callable, Dict, Optional

import redis
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.core.config import settings
from backend.db.session import engine, get_db
from backend.schemas.dashboard import DataFreshness, ServiceStatus, SystemStatusResponse
from backend.services.exasol_client import connect_exasol

logger = logging.getLogger("omnisync.system")

router = APIRouter()

PROBE_TIMEOUT_SECONDS = 4
STALE_AFTER_SECONDS = 15 * 60  # the sync runs every 5 minutes; three missed runs means something is wrong


def probe_postgres() -> str:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return "SELECT 1 ok"


def probe_redis() -> str:
    client = redis.from_url(settings.REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
    client.ping()
    return "PING ok"


def probe_exasol() -> str:
    conn = connect_exasol(connection_timeout=2)
    try:
        conn.execute("SELECT 1").fetchval()
    finally:
        conn.close()
    return "SELECT 1 ok"


def _timed(probe: Callable[[], str]) -> ServiceStatus:
    started = time.perf_counter()
    try:
        detail, state = probe(), "UP"
    except Exception as exc:  # any failure means the service is not usable
        detail, state = f"{type(exc).__name__}: {' '.join(str(exc).split())[:120]}", "DOWN"
    return ServiceStatus(status=state, latency_ms=round((time.perf_counter() - started) * 1000, 1), detail=detail)


def _run_probes() -> Dict[str, ServiceStatus]:
    probes = {"postgres_oltp": probe_postgres, "redis_cache": probe_redis, "exasol_olap": probe_exasol}
    with ThreadPoolExecutor(max_workers=len(probes)) as pool:
        futures = {name: pool.submit(_timed, probe) for name, probe in probes.items()}
        results = {}
        for name, future in futures.items():
            try:
                results[name] = future.result(timeout=PROBE_TIMEOUT_SECONDS)
            except FutureTimeout:
                results[name] = ServiceStatus(
                    status="DOWN", latency_ms=PROBE_TIMEOUT_SECONDS * 1000.0, detail="no answer within the timeout"
                )
    return results


def _count(db: Session, table: str) -> Optional[int]:
    try:
        return int(db.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0)
    except SQLAlchemyError:
        db.rollback()
        return None  # table missing: bootstrap has not run


def _synced_at(db: Session, table: str) -> Optional[datetime]:
    try:
        value = db.execute(text(f"SELECT MAX(synced_at) FROM {table}")).scalar()
    except SQLAlchemyError:
        db.rollback()
        return None
    if isinstance(value, str):  # SQLite (tests) returns text
        value = datetime.fromisoformat(value)
    return value


def _freshness(db: Session, now: datetime) -> DataFreshness:
    risk, golden = _synced_at(db, "user_risk_cache"), _synced_at(db, "golden_customers")
    known = [t for t in (risk, golden) if t is not None]
    if not known:
        return DataFreshness(stale=True)
    oldest = min(known)
    if oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=timezone.utc)
    age = (now - oldest).total_seconds()
    return DataFreshness(
        user_risk_cache_synced_at=risk,
        golden_customers_synced_at=golden,
        age_seconds=round(age, 1),
        stale=age > STALE_AFTER_SECONDS or len(known) < 2,
    )


@router.get(
    "/status",
    response_model=SystemStatusResponse,
    summary="Live service connectivity, data freshness and row counts",
)
def system_status(db: Session = Depends(get_db)):
    now = datetime.now(timezone.utc)
    services = _run_probes()
    freshness = _freshness(db, now)
    row_counts = {
        table: _count(db, table)
        for table in ("inventory", "sales_ledger", "golden_customers", "user_risk_cache", "retention_campaign_audit")
    }
    healthy = all(s.status == "UP" for s in services.values()) and not freshness.stale
    return SystemStatusResponse(
        status="HEALTHY" if healthy else "DEGRADED",
        version=settings.VERSION,
        environment=settings.ENVIRONMENT,
        checked_at=now,
        services=services,
        data_freshness=freshness,
        row_counts=row_counts,
    )
