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
from typing import Any, Dict, List, Optional

from backend.services.exasol_client import connect_exasol, load_sql_statements

logger = logging.getLogger("omnisync.return_risk")

SQL_FILE = (
    Path(__file__).resolve().parent.parent.parent
    / "sql"
    / "exasol"
    / "return_risk"
    / "10_serial_returner.sql"
)
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

# Context-Aware Penalty Math weights for Explainable AI (XAI)
CONTEXT_PENALTY_WEIGHTS: Dict[str, float] = {
    "DEFECTIVE_BATCH": 0.0,
    "STORE_ERROR": 0.0,
    "WRONG_ITEM_SHIPPED": 0.0,
    "CHANGED_MIND": 1.0,
    "WRONG_SIZE": 1.0,
    "WARDROBING_SUSPECTED": 3.0,
    "SUSPECTED_ABUSE": 3.0,
}


def evaluate_user_return_risk_xai(user_id: str, db) -> Dict[str, Any]:
    """
    Evaluates context-aware return risk with Explainable AI (XAI) rationale based on SalesLedger history.
    Applies Context-Aware Penalty Math:
    - DEFECTIVE_BATCH, STORE_ERROR, WRONG_ITEM_SHIPPED: Weight 0.0 (supplier/merchant fault)
    - CHANGED_MIND, WRONG_SIZE: Weight 1.0 (standard customer discretion)
    - WARDROBING_SUSPECTED, SUSPECTED_ABUSE: Weight 3.0 (policy abuse penalty)

    Returns:
        Dictionary containing risk_level (1-5), weighted_score, total_returns,
        defective_count, abuse_count, xai_explanation, human_review_required, staleness_warning.
    """
    from datetime import datetime, timezone
    from sqlalchemy import text
    from backend.models.sales import SalesLedger

    try:
        from sqlalchemy import func

        normalized_ids = {
            user_id,
            user_id.lower(),
            user_id.replace(" ", "_"),
            user_id.replace("_", " "),
            user_id.lower().replace(" ", "_"),
            user_id.lower().replace("_", " "),
        }
        records = (
            db.query(SalesLedger)
            .filter(
                (SalesLedger.user_id.in_(normalized_ids))
                | (
                    func.lower(SalesLedger.user_id).in_(
                        [i.lower() for i in normalized_ids]
                    )
                )
            )
            .all()
        )
        return_records = [r for r in records if r.return_reason_code is not None]
    except Exception:
        return_records = []

    total_returns = len(return_records)

    defective_count = sum(
        1
        for r in return_records
        if r.return_reason_code
        in ("DEFECTIVE_BATCH", "STORE_ERROR", "WRONG_ITEM_SHIPPED")
    )
    discretionary_count = sum(
        1
        for r in return_records
        if r.return_reason_code in ("CHANGED_MIND", "WRONG_SIZE")
    )
    abuse_count = sum(
        1
        for r in return_records
        if r.return_reason_code in ("WARDROBING_SUSPECTED", "SUSPECTED_ABUSE")
    )

    weighted_returns = sum(
        CONTEXT_PENALTY_WEIGHTS.get(r.return_reason_code, 1.0) for r in return_records
    )

    cache_row = None
    try:
        cache_row = db.execute(
            text(
                "SELECT risk_level, return_rate, computed_at "
                "FROM user_risk_cache WHERE user_id = :uid OR user_id = :luid"
            ),
            {"uid": user_id, "luid": user_id.lower()},
        ).fetchone()
    except Exception:
        cache_row = None

    staleness_warning = False
    cached_risk_level = None
    cached_return_rate = 0.0
    if cache_row is not None:
        cached_risk_level = cache_row[0]
        cached_return_rate = cache_row[1]
        comp_at = cache_row[2]
        if comp_at is not None:
            now_utc = datetime.now(timezone.utc)
            if isinstance(comp_at, str):
                try:
                    comp_at = datetime.fromisoformat(comp_at.replace("Z", "+00:00"))
                except Exception:
                    comp_at = None
            if comp_at is not None:
                if comp_at.tzinfo is None:
                    comp_at = comp_at.replace(tzinfo=timezone.utc)
                if (now_utc - comp_at).total_seconds() > 86400:  # older than 24 hours
                    staleness_warning = True

    if total_returns > 0:
        if abuse_count > 0 and (
            abuse_count >= 2 or abuse_count == total_returns or weighted_returns >= 6.0
        ):
            risk_level = 5
            human_review_required = True
            abuse_pct = round((abuse_count / total_returns) * 100)
            xai_explanation = (
                f"Elevated return risk: {abuse_pct}% of returns flagged for suspected wardrobing/abuse. "
                "Human review required."
            )
        elif defective_count > 0 and (
            (defective_count / total_returns) >= 0.5 or defective_count >= 3
        ):
            risk_level = 1
            human_review_required = False
            defect_pct = round((defective_count / total_returns) * 100)
            xai_explanation = (
                f"High return volume ignored: {defect_pct}% of returns linked to "
                "verified supplier defect."
            )
        elif weighted_returns >= 6.0:
            risk_level = 5
            human_review_required = True
            xai_explanation = (
                f"High return volume: {total_returns} returns with context-weighted penalty score of {round(weighted_returns, 1)}. "
                "Human review required."
            )
        elif weighted_returns >= 4.0:
            risk_level = 4
            human_review_required = False
            xai_explanation = f"Elevated return pattern: {total_returns} returns with context-weighted penalty score of {round(weighted_returns, 1)}."
        elif weighted_returns >= 2.5:
            risk_level = 3
            human_review_required = False
            xai_explanation = f"Moderate return friction applied: {total_returns} returns with context-weighted penalty score of {round(weighted_returns, 1)}."
        elif weighted_returns >= 1.5:
            risk_level = 2
            human_review_required = False
            xai_explanation = f"Low-to-moderate returns monitored: {total_returns} returns with context-weighted penalty score of {round(weighted_returns, 1)}."
        else:
            risk_level = 1
            human_review_required = False
            xai_explanation = f"Clean return profile: {total_returns} returns evaluated with negligible penalty score of {round(weighted_returns, 1)}."
    elif cached_risk_level is not None:
        risk_level = int(cached_risk_level)
        human_review_required = risk_level == 5
        rate_pct = round(float(cached_return_rate or 0) * 100, 1)
        if risk_level == 5:
            xai_explanation = f"High historical return rate ({rate_pct}%) exceeds policy threshold (>60%). Human review required."
        elif risk_level >= 3:
            xai_explanation = f"Historical return rate ({rate_pct}%) within monitored return friction band (Level {risk_level})."
        else:
            xai_explanation = f"Standard return behavior: return rate ({rate_pct}%) within acceptable normal operating bounds."
    else:
        risk_level = 1
        human_review_required = False
        xai_explanation = (
            "Standard return profile: no return history or verified defects on record."
        )

    return {
        "user_id": user_id,
        "risk_level": risk_level,
        "weighted_score": float(weighted_returns),
        "total_returns": total_returns,
        "weighted_returns": float(weighted_returns),
        "defective_count": defective_count,
        "abuse_count": abuse_count,
        "xai_explanation": xai_explanation,
        "human_review_required": human_review_required,
        "staleness_warning": staleness_warning,
    }


def run_return_risk_scoring(
    host: Optional[str] = None, params: Optional[Dict[str, float]] = None
) -> Dict[int, int]:
    """
    Recompute USER_RETURN_RISK from the raw purchase/return tables and the Golden Record.

    Returns:
        Number of ids per risk level, e.g. {1: 9000, 2: 1200, 3: 800, 4: 700, 5: 600}.
    """
    params = {**RETURN_RISK_PARAMS, **(params or {})}
    bounds = [
        params["LEVEL1_MAX"],
        params["LEVEL2_MAX"],
        params["LEVEL3_MAX"],
        params["LEVEL4_MAX"],
    ]
    if bounds != sorted(bounds) or not 0 < bounds[0] or bounds[-1] > 1:
        raise ValueError(
            f"Risk level bounds must be increasing and within (0, 1]: {bounds}"
        )

    conn = connect_exasol(host=host, schema=SCHEMA, autocommit=False)
    try:
        for statement in load_sql_statements(SQL_FILE, params):
            conn.execute(statement)
        rows = conn.execute(
            "SELECT RISK_LEVEL, COUNT(*) FROM USER_RETURN_RISK GROUP BY RISK_LEVEL"
        ).fetchall()
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
    parser = argparse.ArgumentParser(
        description="Recompute Return Risk Levels inside Exasol."
    )
    parser.add_argument(
        "--localhost",
        action="store_true",
        help="connect to localhost instead of docker hostname",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    run_return_risk_scoring(host="localhost" if args.localhost else None)


if __name__ == "__main__":
    main()
