"""
Dynamic Return Friction read endpoints.

The Return Risk Level (1-5) is pre-computed in Exasol and cached in PostgreSQL `user_risk_cache`. These endpoints
only read it and attach the matching friction policy, so the UI alters the return flow with no added latency.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.db.session import get_db
from backend.schemas.dashboard import ReturnPolicyListResponse, ReturnPolicyModel, ReturnRiskResponse
from backend.services.dashboard_db import fetch_one
from backend.services.return_policy import DEFAULT_LEVEL, ReturnPolicy, all_policies, get_policy

router = APIRouter()


def _policy_model(policy: ReturnPolicy) -> ReturnPolicyModel:
    return ReturnPolicyModel(**policy.__dict__)


def build_return_risk(db: Session, user_id: str) -> ReturnRiskResponse:
    """Cached risk for a Shopify user id or guest device id; level 1 (not found) when nothing is cached yet."""
    row = fetch_one(
        db,
        "SELECT risk_level, return_rate, purchases_6m, returns_6m, golden_customer_id, computed_at, synced_at "
        "FROM user_risk_cache WHERE user_id = :user_id",
        {"user_id": user_id},
    )
    if row is None:
        return ReturnRiskResponse(
            user_id=user_id, found=False, risk_level=DEFAULT_LEVEL, policy=_policy_model(get_policy(DEFAULT_LEVEL))
        )
    level = get_policy(row[0]).level  # clamps to 1-5
    return ReturnRiskResponse(
        user_id=user_id,
        found=True,
        risk_level=level,
        return_rate=float(row[1]) if row[1] is not None else None,
        purchases_6m=row[2],
        returns_6m=row[3],
        golden_customer_id=row[4],
        computed_at=row[5],
        synced_at=row[6],
        policy=_policy_model(get_policy(level)),
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
