"""
Golden Record identity stitching runner.

Executes the SQL pipeline in `sql/exasol/stitching/` inside Exasol, in one transaction:

    10  build identity nodes (Shopify users, guest devices, Zendesk requesters; JSON web logs parsed)
    20  collect evidence (account links, rare promo codes)
    30  score candidate pairs with a weighted log-odds model
    40  start connected-component labelling
    45  repeat label propagation until nothing changes (looped here: Exasol has no recursive queries)
    50  publish GOLDEN_CUSTOMERS and GOLDEN_CUSTOMER_MEMBERS

Exasol never starts this on its own. A Celery Beat task (or the CLI below) calls `run_identity_stitching`.

Manual run:  python -m backend.services.identity_stitching --localhost
"""

import argparse
import logging
from pathlib import Path
from typing import Dict, List, Optional

from backend.services.exasol_client import connect_exasol, load_sql_statements

logger = logging.getLogger("omnisync.identity")

SQL_DIR = Path(__file__).resolve().parent.parent.parent / "sql" / "exasol" / "stitching"
SCHEMA = "OMNISYNC_ANALYTICS"

BUILD_STAGES = ["10_nodes.sql", "20_links_and_promos.sql", "30_pair_scores.sql", "40_labels_init.sql"]
PROPAGATE_STAGE = "45_propagate_labels.sql"
PUBLISH_STAGES = ["50_golden_records.sql"]
MAX_PROPAGATION_ROUNDS = 25

# Scoring model parameters (log-odds). With BIAS = -4:
#   email match alone        -> logit  4.0 -> probability 0.982  (merged)
#   account link alone       -> logit  3.0 -> probability 0.953  (merged)
#   shared rare promo alone  -> logit <= -1.5 -> probability <= 0.18 (NEVER merged)
STITCHING_PARAMS: Dict[str, float] = {
    "BIAS": -4.0,
    "EMAIL_WEIGHT": 8.0,
    "LINK_WEIGHT": 7.0,
    "PROMO_WEIGHT_CAP": 2.5,  # must stay below abs(BIAS) so promo evidence can never merge on its own
    "MAX_PROMO_HOLDERS": 50,  # codes shared by more nodes than this are coupon-site codes and are ignored
    "MERGE_THRESHOLD": 0.90,
}


def load_sql(file_name: str, params: Dict[str, float]) -> List[str]:
    """Read one stitching SQL file, fill in ${PLACEHOLDERS} and split it into single statements."""
    return load_sql_statements(SQL_DIR / file_name, params)


def run_identity_stitching(host: Optional[str] = None, params: Optional[Dict[str, float]] = None) -> Dict[str, int]:
    """
    Rebuild the Golden Record from the raw tables.

    Everything runs in a single transaction, so readers see either the previous Golden Record
    or the new one, never a half-built table.

    Returns:
        Counts of golden customers, merged pairs and label propagation rounds.
    """
    params = {**STITCHING_PARAMS, **(params or {})}
    if params["PROMO_WEIGHT_CAP"] >= abs(params["BIAS"]):
        raise ValueError("PROMO_WEIGHT_CAP must be smaller than abs(BIAS): promo codes may never merge on their own.")

    conn = connect_exasol(host=host, schema=SCHEMA, autocommit=False)
    try:
        for file_name in BUILD_STAGES:
            logger.info("[identity] running %s", file_name)
            for statement in load_sql(file_name, params):
                conn.execute(statement)

        rounds = _propagate_labels(conn, params)
        _assert_clusters_complete(conn)

        for file_name in PUBLISH_STAGES:
            logger.info("[identity] running %s", file_name)
            for statement in load_sql(file_name, params):
                conn.execute(statement)

        merged_pairs = conn.execute("SELECT COUNT(*) FROM IDENTITY_PAIR_SCORES WHERE IS_MERGED = 1").fetchval()
        customers = conn.execute("SELECT COUNT(*) FROM GOLDEN_CUSTOMERS").fetchval()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    result = {"golden_customers": int(customers), "merged_pairs": int(merged_pairs), "propagation_rounds": rounds}
    logger.info("[identity] done: %s", result)
    return result


def _propagate_labels(conn, params: Dict[str, float]) -> int:
    """Repeat one label-propagation round until a round changes nothing."""
    (statement,) = load_sql(PROPAGATE_STAGE, params)
    for round_number in range(1, MAX_PROPAGATION_ROUNDS + 1):
        changed = conn.execute(statement).rowcount()
        logger.info("[identity] label propagation round %d updated %d nodes", round_number, changed)
        if changed == 0:
            return round_number
    logger.warning("[identity] label propagation hit the %d-round limit", MAX_PROPAGATION_ROUNDS)
    return MAX_PROPAGATION_ROUNDS


def _assert_clusters_complete(conn) -> None:
    """Safety net: every merged pair must end up with one shared label, otherwise abort and roll back."""
    split_pairs = conn.execute(
        "SELECT COUNT(*) FROM IDENTITY_PAIR_SCORES s "
        "JOIN IDENTITY_LABELS la ON la.NODE_ID = s.NODE_A "
        "JOIN IDENTITY_LABELS lb ON lb.NODE_ID = s.NODE_B "
        "WHERE s.IS_MERGED = 1 AND la.CLUSTER_LABEL <> lb.CLUSTER_LABEL"
    ).fetchval()
    if split_pairs:
        raise RuntimeError(f"{split_pairs} merged pairs ended in different clusters: label propagation incomplete.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild the Golden Record inside Exasol.")
    parser.add_argument("--localhost", action="store_true", help="connect to localhost instead of docker hostname")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    run_identity_stitching(host="localhost" if args.localhost else None)


if __name__ == "__main__":
    main()
