"""
One-shot database bootstrap for a fresh OmniSync stack.

Without this, `docker-compose up` starts empty databases: the first checkout fails with "relation does not exist".

    python -m scripts.bootstrap               # PostgreSQL only (what the docker-compose `bootstrap` service runs)
    python -m scripts.bootstrap --exasol      # also set up Exasol (run once Exasol has finished booting)
    python -m scripts.bootstrap --localhost   # when running on your own machine instead of inside a container

PostgreSQL:
  1. SQLAlchemy models -> `inventory`, `sales_ledger`, `retention_campaign_audit`
  2. sql/postgres/*.sql -> `users`, `purchases`, `returns`, `support_tickets`, `golden_customers`,
     `user_risk_cache` and their stage tables
  3. seed the three demo inventory items (only when the table is empty)
Exasol (--exasol):
  4. sql/exasol/*.sql -> schemas and tables
  5. the LightGBM demand UDF and the PyTorch churn UDF
  6. the CONNECTION object used by `EXPORT ... INTO JDBC`

Every step is idempotent, so running the script again is safe.
"""

import argparse
import logging
import re
from pathlib import Path
from typing import List, Optional

from backend.core.config import settings
from backend.db.session import SessionLocal, engine
from backend.models.base import Base

# Importing the models registers their tables with SQLAlchemy's metadata.
from backend.models.inventory import Inventory, InventoryReservation  # noqa: F401
from backend.models.sales import (
    RetentionCampaignAudit,
    SalesLedger,
    UserRiskCache,
)  # noqa: F401
from scripts.apply_schemas import apply_exasol, apply_postgres

logger = logging.getLogger("omnisync.bootstrap")

UDF_FILES = [
    Path(__file__).resolve().parent.parent
    / "backend"
    / "db"
    / "exasol"
    / "01_inventory_predict_udf.sql",
    Path(__file__).resolve().parent.parent
    / "backend"
    / "db"
    / "exasol"
    / "02_nlp_churn_udf.sql",
]

SEED_ITEMS = [
    {"item_id": "SKU-4090-GPU", "name": "RTX 4090", "stock": 50},
    {"item_id": "SKU-HERMES-BAG", "name": "Hermes Birkin", "stock": 5},
    {"item_id": "SKU-AIR-JORDAN", "name": "Air Jordan 1", "stock": 150},
]


def seed_inventory() -> int:
    """Insert the demo inventory items when the table is empty. Returns how many rows were added."""
    db = SessionLocal()
    try:
        if db.query(Inventory).count() > 0:
            logger.info("Inventory already has rows, skipping seed.")
            return 0
        db.add_all(
            [
                Inventory(version=1, virtual_safety_buffer=0, **item)
                for item in SEED_ITEMS
            ]
        )
        db.commit()
        logger.info("Seeded %d inventory items.", len(SEED_ITEMS))
        return len(SEED_ITEMS)
    finally:
        db.close()


def udf_statements(sql_path: Path) -> List[str]:
    """Extract the `--/ ... /` script blocks (CREATE ... SCRIPT) from an Exasol SQL file, without the markers."""
    text = sql_path.read_text(encoding="utf-8")
    return [
        block.strip()
        for block in re.findall(r"^--/\s*\n(.*?)^/\s*$", text, flags=re.S | re.M)
    ]


def setup_exasol(host: Optional[str]) -> None:
    """Create Exasol schemas/tables, register the UDFs and create the JDBC connection to PostgreSQL."""
    from backend.services.exasol_client import connect_exasol
    from backend.services.reverse_etl import SCHEMA, create_exasol_connection

    apply_exasol(host or settings.EXASOL_HOST)
    conn = connect_exasol(host=host, schema=SCHEMA)
    try:
        for path in UDF_FILES:
            for statement in udf_statements(path):
                conn.execute(statement)
            logger.info("Registered UDFs from %s", path.name)
    finally:
        conn.close()
    create_exasol_connection(host=host)


def init_db(host: Optional[str] = None, with_exasol: bool = False) -> None:
    """Run the full bootstrap. `host` overrides the database hostnames (e.g. "localhost")."""
    logger.info("Creating PostgreSQL tables from the SQLAlchemy models...")
    Base.metadata.create_all(bind=engine)
    logger.info("Applying PostgreSQL Module 3 schema (sql/postgres)...")
    apply_postgres(host or settings.POSTGRES_HOST)
    seed_inventory()
    if with_exasol:
        setup_exasol(host)
    logger.info("Bootstrap complete.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap the OmniSync databases.")
    parser.add_argument(
        "--exasol",
        action="store_true",
        help="also set up Exasol (schemas, UDFs, JDBC connection)",
    )
    parser.add_argument(
        "--localhost",
        action="store_true",
        help="connect to localhost instead of docker hostnames",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    init_db(host="localhost" if args.localhost else None, with_exasol=args.exasol)


if __name__ == "__main__":
    main()
