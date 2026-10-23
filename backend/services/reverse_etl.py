"""
Reverse ETL: copy the newly calculated Module 3 metrics from Exasol down to PostgreSQL.

Exasol is passive. This module is called by Celery Beat, and Celery issues the `EXPORT ... INTO JDBC`
statements; Exasol never pushes or schedules anything itself.

Why a stage table: `EXPORT ... INTO JDBC` can only INSERT. So each run is

    1. take an advisory lock, so two overlapping runs cannot corrupt the stage tables
    2. empty the PostgreSQL stage tables
    3. Exasol EXPORT  ->  golden_customers_stage, user_risk_cache_stage
    4. check the stage row counts match what Exasol exported
    5. in ONE PostgreSQL transaction: upsert stage -> serving tables and delete rows that vanished

FastAPI only ever reads the serving tables (primary-key lookups), so checkout latency is unaffected, and
a failed run leaves the previous values in place.

One-time setup (creates the Exasol connection object the EXPORT uses):
    python -m backend.services.reverse_etl --setup --localhost
Requires the PostgreSQL JDBC driver to be available to Exasol (bundled in 7.1; newer versions need a
driver entry in the Exasol settings.cfg - see the Exasol "Load data from PostgreSQL" guide).
"""

import argparse
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.core.config import settings
from backend.db.session import SessionLocal
from backend.services.exasol_client import connect_exasol
from backend.services.postgres_client import connect_postgres

logger = logging.getLogger("omnisync.reverse_etl")


SCHEMA = "OMNISYNC_ANALYTICS"
EXASOL_CONNECTION = "POSTGRES_OMNISYNC"
ADVISORY_LOCK_ID = 730_001  # arbitrary constant shared by every sync run

# (Exasol source query, PostgreSQL stage table, stage columns)
EXPORTS: Dict[str, Dict[str, object]] = {
    "golden_customers": {
        "source": (
            "SELECT GOLDEN_CUSTOMER_ID, PRIMARY_EMAIL, MEMBER_COUNT, TOTAL_SPEND_USD, FIRST_SEEN_AT, LAST_SEEN_AT "
            "FROM GOLDEN_CUSTOMERS"
        ),
        "stage": "golden_customers_stage",
        "columns": [
            "golden_customer_id",
            "primary_email",
            "member_count",
            "total_spend_usd",
            "first_seen_at",
            "last_seen_at",
        ],
    },
    "user_risk_cache": {
        "source": (
            "SELECT USER_ID, GOLDEN_CUSTOMER_ID, RISK_LEVEL, RETURN_RATE, PURCHASES_6M, RETURNS_6M, COMPUTED_AT "
            "FROM USER_RETURN_RISK"
        ),
        "stage": "user_risk_cache_stage",
        "columns": [
            "user_id",
            "golden_customer_id",
            "risk_level",
            "return_rate",
            "purchases_6m",
            "returns_6m",
            "computed_at",
        ],
    },
    # Which identity fragments (Shopify account, guest device, Zendesk requester) make up each golden customer.
    # The dashboard's Golden Record profile page reads this table; it only exists in Exasol otherwise.
    "golden_customer_members": {
        "source": (
            "SELECT m.GOLDEN_CUSTOMER_ID, m.NODE_ID, n.NODE_TYPE, n.SOURCE_ID, m.CONFIDENCE "
            "FROM GOLDEN_CUSTOMER_MEMBERS m JOIN IDENTITY_NODES n ON n.NODE_ID = m.NODE_ID"
        ),
        "stage": "golden_customer_members_stage",
        "columns": ["golden_customer_id", "node_id", "node_type", "source_id", "confidence"],
    },
}

# Upsert stage -> serving inside one transaction. DISTINCT ON guards against duplicate keys in a stage table.
UPSERT_SQL = [
    """
    INSERT INTO golden_customers
        (golden_customer_id, primary_email, member_count, total_spend_usd, first_seen_at, last_seen_at, synced_at)
    SELECT DISTINCT ON (golden_customer_id)
        golden_customer_id, primary_email, member_count, total_spend_usd, first_seen_at, last_seen_at, now()
    FROM golden_customers_stage
    WHERE golden_customer_id IS NOT NULL
    ORDER BY golden_customer_id
    ON CONFLICT (golden_customer_id) DO UPDATE SET
        primary_email = EXCLUDED.primary_email,
        member_count = EXCLUDED.member_count,
        total_spend_usd = EXCLUDED.total_spend_usd,
        first_seen_at = EXCLUDED.first_seen_at,
        last_seen_at = EXCLUDED.last_seen_at,
        synced_at = EXCLUDED.synced_at
    """,
    """
    DELETE FROM golden_customers
    WHERE golden_customer_id NOT IN
        (SELECT golden_customer_id FROM golden_customers_stage WHERE golden_customer_id IS NOT NULL)
    """,
    """
    INSERT INTO user_risk_cache
        (user_id, golden_customer_id, risk_level, return_rate, purchases_6m, returns_6m, computed_at, synced_at)
    SELECT DISTINCT ON (user_id)
        user_id, golden_customer_id, risk_level, return_rate, purchases_6m, returns_6m, computed_at, now()
    FROM user_risk_cache_stage
    WHERE user_id IS NOT NULL
    ORDER BY user_id, computed_at DESC
    ON CONFLICT (user_id) DO UPDATE SET
        golden_customer_id = EXCLUDED.golden_customer_id,
        risk_level = EXCLUDED.risk_level,
        return_rate = EXCLUDED.return_rate,
        purchases_6m = EXCLUDED.purchases_6m,
        returns_6m = EXCLUDED.returns_6m,
        computed_at = EXCLUDED.computed_at,
        synced_at = EXCLUDED.synced_at
    """,
    """
    DELETE FROM user_risk_cache
    WHERE user_id NOT IN (SELECT user_id FROM user_risk_cache_stage WHERE user_id IS NOT NULL)
    """,
    # Membership is replaced as a whole: a node can move to another golden customer between runs. Both
    # statements run in the same transaction as the upserts above, so readers never see a half-empty table.
    "DELETE FROM golden_customer_members",
    """
    INSERT INTO golden_customer_members (golden_customer_id, node_id, node_type, source_id, confidence, synced_at)
    SELECT DISTINCT ON (golden_customer_id, node_id)
        golden_customer_id, node_id, node_type, source_id, confidence, now()
    FROM golden_customer_members_stage
    WHERE golden_customer_id IS NOT NULL AND node_id IS NOT NULL
    ORDER BY golden_customer_id, node_id
    """,
]


def build_export_statement(name: str) -> str:
    """
    Build the Exasol `EXPORT ... INTO JDBC` statement for one table.

    The explicit STATEMENT form is used so PostgreSQL identifiers stay lower-case and unquoted.
    """
    spec = EXPORTS[name]
    columns = ", ".join(spec["columns"])
    placeholders = ", ".join("?" for _ in spec["columns"])
    return (
        f"EXPORT ({spec['source']}) INTO JDBC AT {EXASOL_CONNECTION} "
        f"STATEMENT 'INSERT INTO {spec['stage']} ({columns}) VALUES ({placeholders})'"
    )


def create_exasol_connection(host: Optional[str] = None, postgres_host_for_exasol: Optional[str] = None) -> None:
    """
    One-time setup: create the Exasol CONNECTION object that points at PostgreSQL.

    Args:
        host: where this script reaches Exasol ("localhost" outside docker-compose).
        postgres_host_for_exasol: how the Exasol container reaches PostgreSQL (docker service name by default).
    """
    pg_host = postgres_host_for_exasol or settings.POSTGRES_HOST
    url = f"jdbc:postgresql://{pg_host}:{settings.POSTGRES_PORT}/{settings.POSTGRES_DB}"
    user = settings.POSTGRES_USER.replace("'", "''")
    password = settings.POSTGRES_PASSWORD.replace("'", "''")
    conn = connect_exasol(host=host, schema=SCHEMA)
    try:
        conn.execute(
            f"CREATE OR REPLACE CONNECTION {EXASOL_CONNECTION} TO '{url}' USER '{user}' IDENTIFIED BY '{password}'"
        )
    finally:
        conn.close()
    logger.info("[reverse-etl] Exasol connection %s created for %s", EXASOL_CONNECTION, url)


def sync_module3_to_postgres(
    exasol_host: Optional[str] = None, postgres_host: Optional[str] = None
) -> Dict[str, object]:
    """
    Copy golden customers and return risk levels from Exasol to PostgreSQL.

    Returns:
        {"status": "SKIPPED"} when another sync holds the lock, otherwise {"status": "SYNC_COMPLETE", counts...}.
    """
    pg = connect_postgres(postgres_host)
    try:
        with pg.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK_ID,))
            if not cur.fetchone()[0]:
                logger.warning("[reverse-etl] another sync is running, skipping this run")
                return {"status": "SKIPPED"}
        try:
            return _run_sync(pg, exasol_host)
        finally:
            with pg.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_ID,))
            pg.commit()
    finally:
        pg.close()


def _run_sync(pg, exasol_host: Optional[str]) -> Dict[str, object]:
    """Stage, verify and upsert. Raises (leaving the old serving data untouched) on any problem."""
    with pg.cursor() as cur:
        for spec in EXPORTS.values():
            cur.execute(f"TRUNCATE {spec['stage']}")
    pg.commit()

    exasol = connect_exasol(host=exasol_host, schema=SCHEMA)
    try:
        expected = {}
        for name, spec in EXPORTS.items():
            expected[name] = int(exasol.execute(f"SELECT COUNT(*) FROM ({spec['source']})").fetchval())
            exasol.execute(build_export_statement(name))
            logger.info("[reverse-etl] exported %d rows for %s", expected[name], name)
    finally:
        exasol.close()

    try:
        with pg.cursor() as cur:
            for name, spec in EXPORTS.items():
                cur.execute(f"SELECT COUNT(*) FROM {spec['stage']}")
                staged = cur.fetchone()[0]
                if staged == 0 or staged != expected[name]:
                    raise RuntimeError(
                        f"Stage check failed for {name}: Exasol has {expected[name]} rows, PostgreSQL staged {staged}. "
                        "Serving tables left unchanged."
                    )
            for statement in UPSERT_SQL:
                cur.execute(statement)
        pg.commit()
    except Exception:
        pg.rollback()
        raise

    result: Dict[str, object] = {"status": "SYNC_COMPLETE", **expected}
    logger.info("[reverse-etl] done: %s", result)
    return result


TELEMETRY_TABLE = (SCHEMA, "LIVE_SKU_TELEMETRY")
TELEMETRY_COLUMNS = ["ITEM_ID", "CURRENT_PHYSICAL_STOCK", "CART_VELOCITY_5M", "TELEMETRY_TIMESTAMP"]

# PRD formula, evaluated inside Exasol by the LightGBM UDF created in backend/db/exasol/01_inventory_predict_udf.sql:
#   Buffer = MAX(0, Predicted_Demand_15m - Current_Physical_Stock + Active_Carts_5m)
# The buffer is a computed value, not a stored column: LIVE_SKU_TELEMETRY only holds the model inputs.
INVENTORY_BUFFER_QUERY = """
SELECT ITEM_ID,
       GREATEST(0,
                OMNISYNC_ANALYTICS.PREDICT_DEMAND_15M(
                    CAST(CART_VELOCITY_5M AS DOUBLE),
                    CAST(TO_CHAR(TELEMETRY_TIMESTAMP, 'HH24') AS DOUBLE),
                    CAST(TO_CHAR(TELEMETRY_TIMESTAMP, 'D') AS DOUBLE))
                - CURRENT_PHYSICAL_STOCK + CART_VELOCITY_5M) AS VIRTUAL_SAFETY_BUFFER
FROM OMNISYNC_ANALYTICS.LIVE_SKU_TELEMETRY
"""


def refresh_inventory_telemetry(
    exasol_host: Optional[str] = None,
    session: Optional[Session] = None,
    cart_tracker: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Load the model inputs for every SKU into Exasol: current physical stock (PostgreSQL) and the number of
    active carts in the last 5 minutes (Redis). Celery triggers this just before the buffer sync.
    """
    from backend.services.cart_tracker import get_cart_tracker

    db: Session = session if session is not None else SessionLocal()
    try:
        stock_rows = db.execute(text("SELECT item_id, stock FROM inventory")).fetchall()
    finally:
        if session is None:
            db.close()

    tracker = cart_tracker if cart_tracker is not None else get_cart_tracker()
    now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    telemetry = []
    for item_id, stock in stock_rows:
        try:
            velocity = int(tracker.get_cart_velocity(item_id))
        except Exception as exc:  # Redis unavailable: treat as no carts rather than failing the whole sync
            logger.warning("[reverse-etl] cart velocity unavailable for %s, using 0: %s", item_id, exc)
            velocity = 0
        telemetry.append((str(item_id), int(stock), velocity, now))

    exasol = connect_exasol(host=exasol_host, schema=SCHEMA)
    try:
        exasol.execute("DELETE FROM OMNISYNC_ANALYTICS.LIVE_SKU_TELEMETRY")
        if telemetry:
            exasol.import_from_iterable(telemetry, TELEMETRY_TABLE, import_params={"columns": TELEMETRY_COLUMNS})
    finally:
        exasol.close()
    logger.info("[reverse-etl] refreshed %d SKU telemetry rows in Exasol", len(telemetry))
    return {"telemetry_rows": len(telemetry)}


def sync_inventory_buffer_to_postgres(
    exasol_host: Optional[str] = None,
    session: Optional[Session] = None,
) -> Dict[str, Any]:
    """
    Compute the virtual safety stock buffer in Exasol and write it to PostgreSQL.

    Runs INVENTORY_BUFFER_QUERY in Exasol, then for each SKU executes the parameterized update:
        UPDATE inventory SET virtual_safety_buffer = :val WHERE item_id = :id
    Checkout reads that column and rejects an order when quantity > stock - virtual_safety_buffer.
    """
    logger.info("[reverse-etl] Computing virtual safety buffers in Exasol...")

    exasol = connect_exasol(host=exasol_host, schema=SCHEMA)
    try:
        rows = exasol.execute(INVENTORY_BUFFER_QUERY).fetchall()
    finally:
        exasol.close()

    db: Session = session if session is not None else SessionLocal()
    update_stmt = text("UPDATE inventory SET virtual_safety_buffer = :val WHERE item_id = :id")

    updated_count = 0
    unknown_items = []
    try:
        for row in rows:
            item_id, val = str(row[0]), int(row[1] or 0)
            result = db.execute(update_stmt, {"val": val, "id": item_id})
            if result.rowcount:
                updated_count += result.rowcount
            else:
                unknown_items.append(item_id)
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error(f"[reverse-etl] Failed to update PostgreSQL inventory buffer: {exc}")
        raise
    finally:
        if session is None:
            db.close()

    if unknown_items:
        logger.warning(
            "[reverse-etl] %d telemetry SKUs are not in PostgreSQL inventory: %s", len(unknown_items), unknown_items
        )
    logger.info("[reverse-etl] synced virtual_safety_buffer for %d inventory rows", updated_count)
    return {
        "status": "SYNC_COMPLETE",
        "target": "POSTGRES_INVENTORY_BUFFER",
        "records_updated": updated_count,
        "unknown_items": len(unknown_items),
    }


# Backwards compatibility alias
sync_inventory_buffers_to_postgres = sync_inventory_buffer_to_postgres


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Module 3 metrics from Exasol to PostgreSQL.")
    parser.add_argument("--setup", action="store_true", help="create the Exasol connection object, then exit")
    parser.add_argument("--localhost", action="store_true", help="reach both databases on localhost")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    host = "localhost" if args.localhost else None
    if args.setup:
        create_exasol_connection(host=host)
    else:
        sync_module3_to_postgres(exasol_host=host, postgres_host=host)


if __name__ == "__main__":
    main()
