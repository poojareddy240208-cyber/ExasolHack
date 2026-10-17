"""
Apply the Module 3 SQL schemas to PostgreSQL and/or Exasol.

Usage (from the repository root):
    python -m scripts.apply_schemas --target postgres --localhost
    python -m scripts.apply_schemas --target exasol --localhost
    python -m scripts.apply_schemas --target all --localhost

`--localhost` swaps the docker-compose hostnames (postgres / exasol) for localhost, which is what
you need when running this script on your own machine instead of inside a container.
All SQL files are idempotent, so running the script twice is safe.
"""

import argparse
import logging
from pathlib import Path
from typing import List

from backend.core.config import settings
from backend.services.exasol_client import connect_exasol, split_sql_statements
from backend.services.postgres_client import connect_postgres

logger = logging.getLogger("omnisync.schemas")

SQL_ROOT = Path(__file__).resolve().parent.parent / "sql"


def _sql_files(engine: str) -> List[Path]:
    """Return the schema files for one engine in filename order (001_, 002_, ...)."""
    return sorted((SQL_ROOT / engine).glob("*.sql"))


def apply_postgres(host: str) -> None:
    """Run every PostgreSQL schema file in a single transaction per file."""
    conn = connect_postgres(host)
    try:
        for path in _sql_files("postgres"):
            logger.info("PostgreSQL: applying %s", path.name)
            with conn, conn.cursor() as cur:  # `with conn` commits, or rolls back on error
                cur.execute(path.read_text(encoding="utf-8"))
    finally:
        conn.close()


def apply_exasol(host: str) -> None:
    """Run every Exasol schema file statement by statement."""
    conn = connect_exasol(host=host)
    try:
        for path in _sql_files("exasol"):
            logger.info("Exasol: applying %s", path.name)
            for statement in split_sql_statements(path.read_text(encoding="utf-8")):
                conn.execute(statement)
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply OmniSync Module 3 SQL schemas.")
    parser.add_argument("--target", choices=["postgres", "exasol", "all"], default="all")
    parser.add_argument("--localhost", action="store_true", help="connect to localhost instead of docker hostnames")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.target in ("postgres", "all"):
        apply_postgres("localhost" if args.localhost else settings.POSTGRES_HOST)
    if args.target in ("exasol", "all"):
        apply_exasol("localhost" if args.localhost else settings.EXASOL_HOST)
    logger.info("Done.")


if __name__ == "__main__":
    main()
