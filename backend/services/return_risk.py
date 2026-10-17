"""
Serial Returner batch job runner (Return Risk Levels 1-5).

Runs `sql/exasol/return_risk/10_serial_returner.sql` inside Exasol, in one transaction, against the
Golden Record produced by `identity_stitching`. The risk levels are pre-computed here so FastAPI only
has to read an integer from PostgreSQL (0ms added latency at checkout).

Exasol never starts this on its own. A Celery Beat task (or the CLI below) calls `run_return_risk_scoring`.

Manual run:  python -m backend.services.return_risk --localhost
"""

import argparse
import logging
from pathlib import Path
from typing import Dict, Optional

from backend.services.exasol_client import connect_exasol, load_sql_statements

logger = logging.getLogger("omnisync.return_risk")

SQL_FILE = Path(__file__).resolve().parent.parent.parent / "sql" / "exasol" / "return_risk" / "10_serial_returner.sql"
SCHEMA = "OMNISYNC_ANALYTICS"

# Level 1, 3 and 5 bands come from the PRD (<10%, 25-40%, >60%). Levels 2 and 4 fill the gaps and
# are PROPOSED values: confirm them with the team before the demo.
RETURN_RISK_PARAMS: Dict[str, float] = {
    "WINDOW_MONTHS": 6,
    "MIN_PURCHASES": 3,  # fewer purchases than this in the window -> not enough history, level 1
    "LEVEL1_MAX": 0.10,  # rate below this -> level 1
    "LEVEL2_MAX": 0.25,  # below this -> level 2
    "LEVEL3_MAX": 0.40,  # below this -> level 3
    "LEVEL4_MAX": 0.60,  # up to and including this -> level 4, above -> level 5
}


def run_return_risk_scoring(host: Optional[str] = None, params: Optional[Dict[str, float]] = None) -> Dict[int, int]:
    """
    Recompute USER_RETURN_RISK from the raw purchase/return tables and the Golden Record.

    Returns:
        Number of ids per risk level, e.g. {1: 9000, 2: 1200, 3: 800, 4: 700, 5: 600}.
    """
    params = {**RETURN_RISK_PARAMS, **(params or {})}
    bounds = [params["LEVEL1_MAX"], params["LEVEL2_MAX"], params["LEVEL3_MAX"], params["LEVEL4_MAX"]]
    if bounds != sorted(bounds) or not 0 < bounds[0] or bounds[-1] > 1:
        raise ValueError(f"Risk level bounds must be increasing and within (0, 1]: {bounds}")

    conn = connect_exasol(host=host, schema=SCHEMA, autocommit=False)
    try:
        for statement in load_sql_statements(SQL_FILE, params):
            conn.execute(statement)
        rows = conn.execute("SELECT RISK_LEVEL, COUNT(*) FROM USER_RETURN_RISK GROUP BY RISK_LEVEL").fetchall()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    result = {int(level): int(count) for level, count in rows}
    logger.info("[return-risk] done: %s", dict(sorted(result.items())))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Recompute Return Risk Levels inside Exasol.")
    parser.add_argument("--localhost", action="store_true", help="connect to localhost instead of docker hostname")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    run_return_risk_scoring(host="localhost" if args.localhost else None)


if __name__ == "__main__":
    main()
