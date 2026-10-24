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
from typing import Any, Dict, List, Optional

from backend.services.exasol_client import connect_exasol, load_sql_statements

logger = logging.getLogger("omnisync.identity")

SQL_DIR = Path(__file__).resolve().parent.parent.parent / "sql" / "exasol" / "stitching"
SCHEMA = "OMNISYNC_ANALYTICS"

BUILD_STAGES = [
    "10_nodes.sql",
    "20_links_and_promos.sql",
    "30_pair_scores.sql",
    "40_labels_init.sql",
]
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

    result = {
        "golden_customers": int(customers),
        "merged_pairs": int(merged_pairs),
        "propagation_rounds": rounds,
    }
    logger.info("[identity] done: %s", result)
    return result


def _propagate_labels(conn, params: Dict[str, float]) -> int:
    """Repeat one label-propagation round until a round changes nothing."""
    (statement,) = load_sql(PROPAGATE_STAGE, params)
    for round_number in range(1, MAX_PROPAGATION_ROUNDS + 1):
        changed = conn.execute(statement).rowcount()
        logger.info(
            "[identity] label propagation round %d updated %d nodes",
            round_number,
            changed,
        )
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


def generate_identity_investigation(candidate_node_id: str, db) -> Optional[Dict[str, Any]]:
    """
    Queries candidate_links and identity_evidence tables to assemble an Explainable AI (XAI)
    identity detective investigation card.

    Returns structured analysis containing:
    - confidence_score (Integer 0-100)
    - supporting_evidence (List of strings)
    - counter_evidence (List of strings)
    - decision_status (String: 'STRONG_CANDIDATE_PENDING_REVIEW', 'MERGED', 'REJECTED')
    """
    from backend.services.dashboard_db import fetch_all, fetch_one

    # 1. Query candidate_links for direct or member matches
    try:
        candidate_rows = fetch_all(
            db,
            "SELECT node_a, node_b, model_score, human_review_status "
            "FROM candidate_links "
            "WHERE node_a = :id OR node_b = :id "
            "ORDER BY model_score DESC",
            {"id": candidate_node_id},
        )
    except Exception:
        candidate_rows = []

    if not candidate_rows:
        try:
            candidate_rows = fetch_all(
                db,
                "SELECT cl.node_a, cl.node_b, cl.model_score, cl.human_review_status "
                "FROM candidate_links cl "
                "JOIN golden_customer_members m ON (cl.node_a = m.node_id OR cl.node_b = m.node_id "
                "                                   OR cl.node_a = m.source_id OR cl.node_b = m.source_id) "
                "WHERE m.golden_customer_id = :id "
                "ORDER BY cl.model_score DESC",
                {"id": candidate_node_id},
            )
        except Exception:
            candidate_rows = []

    # 2. Query identity_evidence
    try:
        evidence_rows = fetch_all(
            db,
            "SELECT source_id, event_type, confidence_weight, linked_to_golden_id "
            "FROM identity_evidence "
            "WHERE linked_to_golden_id = :id OR source_id = :id "
            "ORDER BY confidence_weight DESC",
            {"id": candidate_node_id},
        )
    except Exception:
        evidence_rows = []

    if not evidence_rows:
        try:
            evidence_rows = fetch_all(
                db,
                "SELECT ie.source_id, ie.event_type, ie.confidence_weight, ie.linked_to_golden_id "
                "FROM identity_evidence ie "
                "JOIN golden_customer_members m ON (ie.source_id = m.source_id OR ie.source_id = m.node_id) "
                "WHERE m.golden_customer_id = :id "
                "ORDER BY ie.confidence_weight DESC",
                {"id": candidate_node_id},
            )
        except Exception:
            evidence_rows = []

    # 3. Check golden_customers summary
    gc_row = None
    try:
        gc_row = fetch_one(
            db,
            "SELECT golden_customer_id, primary_email, member_count FROM golden_customers WHERE golden_customer_id = :id",
            {"id": candidate_node_id},
        )
    except Exception:
        gc_row = None

    if not candidate_rows and not evidence_rows and not gc_row:
        return None

    confidence_score = 50
    decision_status = "STRONG_CANDIDATE_PENDING_REVIEW"

    if candidate_rows:
        best_link = candidate_rows[0]
        confidence_score = int(round(float(best_link[2]) * 100))
        decision_status = best_link[3] or "STRONG_CANDIDATE_PENDING_REVIEW"
    elif evidence_rows:
        positive_weights = [float(r[2]) for r in evidence_rows if float(r[2]) > 0]
        max_weight = max(positive_weights) if positive_weights else 0.5
        confidence_score = int(round(max_weight * 100))
        decision_status = "STRONG_CANDIDATE_PENDING_REVIEW"
    elif gc_row:
        confidence_score = 100
        decision_status = "MERGED"

    supporting_evidence: List[str] = []
    counter_evidence: List[str] = []

    for r in evidence_rows:
        event = str(r[1]).strip()
        weight = float(r[2])
        is_counter = (
            weight < 0
            or event.upper().startswith("COUNTER")
            or any(
                kw in event.lower()
                for kw in [
                    "no authenticated",
                    "public forum",
                    "forum",
                    "shared via",
                    "conflict",
                    "mismatch",
                ]
            )
        )
        if is_counter:
            clean_event = event[len("COUNTER:") :].strip() if event.upper().startswith("COUNTER:") else event
            if clean_event not in counter_evidence:
                counter_evidence.append(clean_event)
        else:
            if event not in supporting_evidence:
                supporting_evidence.append(event)

    if not supporting_evidence and candidate_rows:
        for cl in candidate_rows:
            supporting_evidence.append(f"Pairwise graph candidate link between {cl[0]} and {cl[1]}")

    if not counter_evidence and decision_status == "STRONG_CANDIDATE_PENDING_REVIEW":
        counter_evidence.append("No authenticated login")
        if any("PROMO" in s.upper() for s in supporting_evidence):
            counter_evidence.append("Promo code could be shared via public forum")

    return {
        "confidence_score": confidence_score,
        "supporting_evidence": supporting_evidence,
        "counter_evidence": counter_evidence,
        "decision_status": decision_status,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild the Golden Record inside Exasol.")
    parser.add_argument(
        "--localhost",
        action="store_true",
        help="connect to localhost instead of docker hostname",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    run_identity_stitching(host="localhost" if args.localhost else None)


if __name__ == "__main__":
    main()
