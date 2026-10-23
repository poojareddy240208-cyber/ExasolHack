"""
Dynamic Return Friction read endpoints.

The Return Risk Level (1-5) is pre-computed in Exasol and cached in PostgreSQL `user_risk_cache`. These endpoints
only read it and attach the matching friction policy, so the UI alters the return flow with no added latency.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.db.session import get_db
from backend.schemas.dashboard import (
    ReturnPolicyListResponse,
    ReturnPolicyModel,
    ReturnRiskResponse,
)
from backend.services.dashboard_db import fetch_one
from backend.services.return_policy import (
    DEFAULT_LEVEL,
    ReturnPolicy,
    all_policies,
    get_policy,
)

router = APIRouter()


from backend.services.return_risk import evaluate_user_return_risk_xai


def _policy_model(policy: ReturnPolicy) -> ReturnPolicyModel:
    return ReturnPolicyModel(**policy.__dict__)


def build_return_risk(db: Session, user_id: str) -> ReturnRiskResponse:
    """
    Context-Aware Return Risk Evaluation for a user.
    Reads pre-computed score from user_risk_cache and evaluates context-aware XAI rationale
    from user's SalesLedger return history (accounting for supplier defects and wardrobing abuse).
    """
    row = fetch_one(
        db,
        "SELECT risk_level, return_rate, purchases_6m, returns_6m, golden_customer_id, computed_at, synced_at "
        "FROM user_risk_cache WHERE user_id = :user_id OR lower(user_id) = :luser_id",
        {"user_id": user_id, "luser_id": user_id.lower()},
    )

    xai_eval = evaluate_user_return_risk_xai(user_id, db)

    if row is None:
        if xai_eval["total_returns"] > 0:
            level = xai_eval["risk_level"]
            return ReturnRiskResponse(
                user_id=user_id,
                found=True,
                risk_level=level,
                return_rate=1.0 if level == 5 else 0.1,
                purchases_6m=xai_eval["total_returns"],
                returns_6m=xai_eval["total_returns"],
                golden_customer_id=None,
                computed_at=None,
                synced_at=None,
                policy=_policy_model(get_policy(level)),
                xai_explanation=xai_eval["xai_explanation"],
                staleness_warning=xai_eval["staleness_warning"],
                human_review_required=xai_eval["human_review_required"] or (level == 5),
            )
        return ReturnRiskResponse(
            user_id=user_id,
            found=False,
            risk_level=DEFAULT_LEVEL,
            policy=_policy_model(get_policy(DEFAULT_LEVEL)),
            xai_explanation=xai_eval["xai_explanation"],
            staleness_warning=xai_eval["staleness_warning"],
            human_review_required=False,
        )

    # If the user has specific context-aware return ledger history, use context-aware level
    if xai_eval["total_returns"] > 0:
        level = xai_eval["risk_level"]
    else:
        level = get_policy(row[0]).level  # clamps to 1-5

    human_review = (level == 5) or xai_eval.get("human_review_required", False)

    return ReturnRiskResponse(
        user_id=user_id,
        found=True,
        risk_level=level,
        return_rate=float(row[1]) if row[1] is not None else None,
        purchases_6m=row[2],
        returns_6m=(
            xai_eval["total_returns"] if xai_eval["total_returns"] > 0 else row[3]
        ),
        golden_customer_id=row[4],
        computed_at=row[5],
        synced_at=row[6],
        policy=_policy_model(get_policy(level)),
        xai_explanation=xai_eval["xai_explanation"],
        staleness_warning=xai_eval["staleness_warning"],
        human_review_required=human_review,
    )


@router.get(
    "/policies",
    response_model=ReturnPolicyListResponse,
    summary="The five return-friction levels and what each one means",
)
def list_policies():
    return ReturnPolicyListResponse(policies=[_policy_model(p) for p in all_policies()])


@router.get(
    "/{user_id}",
    response_model=ReturnRiskResponse,
    summary="Pre-computed return risk level and policy for a shopper",
)
def get_return_risk(user_id: str, db: Session = Depends(get_db)):
    return build_return_risk(db, user_id)
