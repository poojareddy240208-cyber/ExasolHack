"""
Pydantic v2 response/request models for the dashboard (read-only) API endpoints.

All values come from PostgreSQL (pre-computed by Exasol and synced by Celery Beat) or Redis (live cart velocity).
Nothing here triggers an analytics query.
"""

from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------------------------------------------
# Return risk
# --------------------------------------------------------------------------------------------------------------
class ReturnPolicyModel(BaseModel):
    level: int
    name: str
    return_rate_band: str = Field(
        ..., description="Return-rate range this level covers (6-month window)"
    )
    defined_in_prd: bool = Field(
        ..., description="False for the proposed levels 2 and 4"
    )
    customer_message: str
    restocking_fee_usd: float
    cash_on_delivery_allowed: bool
    keep_it_discount_pct: int = Field(
        ..., description='"Keep it for X% off" offer; 0 means no offer'
    )
    human_review_required: bool = Field(
        False,
        description="Flag for Level 5 users requiring human review rather than silent ban",
    )


class ReturnPolicyListResponse(BaseModel):
    policies: List[ReturnPolicyModel]


class ReturnRiskResponse(BaseModel):
    user_id: str
    found: bool = Field(
        ...,
        description="False when no score is cached yet; the default level 1 is returned",
    )
    risk_level: int = Field(..., ge=1, le=5)
    return_rate: Optional[float] = None
    purchases_6m: Optional[int] = None
    returns_6m: Optional[int] = None
    golden_customer_id: Optional[str] = None
    computed_at: Optional[datetime] = Field(
        None, description="When Exasol calculated the score"
    )
    synced_at: Optional[datetime] = Field(
        None, description="When it was copied to PostgreSQL"
    )
    policy: ReturnPolicyModel
    xai_explanation: Optional[str] = Field(
        None, description="Explainable AI rationale for context-aware return evaluation"
    )
    staleness_warning: bool = Field(
        False, description="True if computed_at timestamp is older than 24 hours"
    )
    human_review_required: bool = Field(
        False,
        description="Flag for Level 5 users requiring governance and human review",
    )


# --------------------------------------------------------------------------------------------------------------
# Golden Record
# --------------------------------------------------------------------------------------------------------------
class GoldenCustomerSummary(BaseModel):
    golden_customer_id: str
    primary_email: Optional[str] = None
    member_count: int = Field(
        ..., description="Identity fragments stitched into this customer"
    )
    total_spend_usd: float = Field(
        ..., description="Net of refunds, across every linked account and device"
    )
    first_seen_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None
    ltv_tier: str = Field(..., description="Top 1% / 5% / 10% / 20% VIP, or Standard")


class GoldenMember(BaseModel):
    node_id: str
    node_type: str = Field(..., description="SHOPIFY, DEVICE or ZENDESK")
    source_id: str
    confidence: float = Field(
        ...,
        ge=0,
        le=1,
        description="Strongest merge probability linking it to the customer",
    )


class GoldenCustomerProfile(GoldenCustomerSummary):
    ltv_percent_rank: float = Field(..., ge=0, le=1)
    members: List[GoldenMember]
    return_risk: Optional[ReturnRiskResponse] = None


class GoldenCustomerSearchResponse(BaseModel):
    query: str
    count: int
    results: List[GoldenCustomerSummary]


class GoldenStatsResponse(BaseModel):
    golden_customers: int
    stitched_nodes: int
    multi_identity_customers: int = Field(
        ..., description="Customers made of more than one identity fragment"
    )
    avg_members_per_customer: float
    last_synced_at: Optional[datetime] = None


class IdentityInvestigationResponse(BaseModel):
    confidence_score: int = Field(
        ..., ge=0, le=100, description="Confidence score percentage (0-100)"
    )
    supporting_evidence: List[str] = Field(
        ..., description="Key signals supporting the identity link"
    )
    counter_evidence: List[str] = Field(
        ..., description="Key counter-signals or ambiguity factors"
    )
    decision_status: str = Field(
        ...,
        description="Decision status: 'STRONG_CANDIDATE_PENDING_REVIEW', 'MERGED', or 'REJECTED'",
    )


# --------------------------------------------------------------------------------------------------------------
# Inventory & cart velocity
# --------------------------------------------------------------------------------------------------------------
class InventoryItemView(BaseModel):
    item_id: str
    name: str
    stock: int = Field(..., description="Current physical stock")
    version: int = Field(..., description="OCC version stamp")
    virtual_safety_buffer: int = Field(
        ..., description="Synced from the Exasol LightGBM surge prediction"
    )
    available_stock: int = Field(
        ...,
        description="stock - virtual_safety_buffer (never below 0); what checkout allows",
    )
    active_carts_5m: Optional[int] = Field(
        None, description="Live carts in the last 5 minutes; null if Redis is down"
    )
    predicted_demand_15m_estimate: Optional[int] = Field(
        None,
        description="buffer + stock - carts when the buffer is above 0; null when the buffer is 0 (clamped)",
    )


class InventoryListResponse(BaseModel):
    generated_at: datetime
    total_items: int
    total_active_carts_5m: Optional[int] = None
    redis_available: bool
    items: List[InventoryItemView]


class CartAddRequest(BaseModel):
    item_id: str
    user_session_id: str


class CartVelocityResponse(BaseModel):
    item_id: str
    window_seconds: int
    active_carts_5m: int


class CartAddResponse(CartVelocityResponse):
    user_session_id: str


# --------------------------------------------------------------------------------------------------------------
# Churn radar
# --------------------------------------------------------------------------------------------------------------
class ChurnAlert(BaseModel):
    ticket_id: str
    golden_customer_id: str
    customer_email: Optional[str] = Field(
        None, description="From the Golden Record, when it has been synced"
    )
    churn_score: float
    ltv_tier: str
    action_triggered: str
    status: str
    created_at: datetime


class ChurnAlertsResponse(BaseModel):
    since_hours: int
    count_in_window: int
    total_alerts: int
    alerts: List[ChurnAlert]


# --------------------------------------------------------------------------------------------------------------
# System status
# --------------------------------------------------------------------------------------------------------------
class ServiceStatus(BaseModel):
    status: str = Field(..., description="UP or DOWN")
    latency_ms: float
    detail: Optional[str] = None


class DataFreshness(BaseModel):
    user_risk_cache_synced_at: Optional[datetime] = None
    golden_customers_synced_at: Optional[datetime] = None
    age_seconds: Optional[float] = Field(
        None, description="Age of the oldest sync; null when nothing was synced"
    )
    stale: bool = Field(
        ...,
        description="True when nothing was synced, or the last sync is older than 15 minutes",
    )


class SystemStatusResponse(BaseModel):
    status: str = Field(
        ...,
        description="HEALTHY, or DEGRADED when a service is down or the sync is stale",
    )
    version: str
    environment: str
    checked_at: datetime
    services: Dict[str, ServiceStatus]
    data_freshness: DataFreshness
    row_counts: Dict[str, Optional[int]]
