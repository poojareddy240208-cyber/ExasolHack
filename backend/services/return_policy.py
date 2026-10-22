"""
Return-friction policy per Return Risk Level (1-5).

The level itself is pre-computed in Exasol by the serial-returner batch job (return rate = returns / purchases over
the last 6 months) and cached in PostgreSQL `user_risk_cache`. This table says what each level means for the shopper,
so the UI can alter the return flow by reading an integer, with no model or analytics query at request time.

Levels 1, 3 and 5 are defined by the PRD. Levels 2 and 4 are PROPOSED bands that fill the gaps; confirm them with the
team (they are flagged `defined_in_prd: false` in the API).
"""

from dataclasses import dataclass
from typing import Dict, List

MIN_LEVEL = 1
MAX_LEVEL = 5
DEFAULT_LEVEL = 1  # shoppers with no cached score yet get standard free returns


@dataclass(frozen=True)
class ReturnPolicy:
    level: int
    name: str
    return_rate_band: str
    defined_in_prd: bool
    customer_message: str
    restocking_fee_usd: float
    cash_on_delivery_allowed: bool
    keep_it_discount_pct: int  # "Keep it for X% off" offer shown instead of a return; 0 = no offer


RETURN_POLICIES: Dict[int, ReturnPolicy] = {
    1: ReturnPolicy(
        1,
        "Standard Free Returns",
        "< 10%",
        True,
        "Free returns with an instant prepaid label.",
        0.0,
        True,
        0,
    ),
    2: ReturnPolicy(
        2,
        "Free Returns (monitored)",
        "10% to < 25%",
        False,
        "Free returns with an instant prepaid label.",
        0.0,
        True,
        0,
    ),
    3: ReturnPolicy(
        3,
        "Return Friction Offer",
        "25% to < 40%",
        True,
        "Keep this item and get an instant 10% credit instead of returning it.",
        0.0,
        True,
        10,
    ),
    4: ReturnPolicy(
        4,
        "Elevated Friction",
        "40% to 60%",
        False,
        "Keep this item and get an instant 10% credit instead of returning it.",
        0.0,
        True,
        10,
    ),
    5: ReturnPolicy(
        5,
        "Penalty Guard",
        "> 60%",
        True,
        "A $5 restocking fee applies to returns, and Cash on Delivery is not available.",
        5.0,
        False,
        0,
    ),
}


def get_policy(level: int) -> ReturnPolicy:
    """Policy for a risk level; out-of-range values are clamped to 1-5."""
    return RETURN_POLICIES[min(MAX_LEVEL, max(MIN_LEVEL, int(level)))]


def all_policies() -> List[ReturnPolicy]:
    return [RETURN_POLICIES[level] for level in range(MIN_LEVEL, MAX_LEVEL + 1)]
