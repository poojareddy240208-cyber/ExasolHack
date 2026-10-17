"""
OmniSync API v1 Modular Router Aggregator.
Combines sub-routers for checkout, webhooks, and diagnostics.
"""

from fastapi import APIRouter
from backend.api.v1.endpoints import checkout, webhooks, health

api_v1_router = APIRouter()

# Register sub-routers
api_v1_router.include_router(health.router, tags=["Diagnostics & System Health"])

api_v1_router.include_router(checkout.router, prefix="/checkout", tags=["Transactional OLTP - OCC Checkout"])

api_v1_router.include_router(webhooks.router, prefix="/webhooks", tags=["Asynchronous Webhooks - NLP Churn Queue"])
