"""
OmniSync API v1 Modular Router Aggregator.
Combines sub-routers for checkout, webhooks, diagnostics and the read-only dashboard endpoints.
"""

from fastapi import APIRouter
from backend.api.v1.endpoints import cart, checkout, churn, customers, health, inventory, return_risk, system, webhooks

api_v1_router = APIRouter()

# Register sub-routers
api_v1_router.include_router(health.router, tags=["Diagnostics & System Health"])

api_v1_router.include_router(checkout.router, prefix="/checkout", tags=["Transactional OLTP - OCC Checkout"])

api_v1_router.include_router(webhooks.router, prefix="/webhooks", tags=["Asynchronous Webhooks - NLP Churn Queue"])

# Read-only dashboard endpoints (data is pre-computed in Exasol and synced to PostgreSQL by Celery Beat)
api_v1_router.include_router(system.router, prefix="/system", tags=["Diagnostics & System Health"])
api_v1_router.include_router(inventory.router, prefix="/inventory", tags=["Predictive Inventory"])
api_v1_router.include_router(cart.router, prefix="/cart", tags=["Predictive Inventory"])
api_v1_router.include_router(return_risk.router, prefix="/return-risk", tags=["Dynamic Return Friction"])
api_v1_router.include_router(customers.router, prefix="/golden-customers", tags=["Identity Stitching - Golden Record"])
api_v1_router.include_router(churn.router, prefix="/churn", tags=["NLP Churn Early Warning"])
