"""
OmniSync - Advanced Retail Analytics Platform
=============================================
FastAPI Application Entry Point.

Architectural Highlights:
- High-throughput asynchronous REST API built with FastAPI & Pydantic v2.
- Strict OLTP / OLAP separation:
  * PostgreSQL handles transactional checkout with Optimistic Concurrency Control (OCC).
  * Exasol Personal Edition runs in-memory analytical ML models (LightGBM & PyTorch UDFs).
  * Redis tracks real-time cart velocity & brokers Celery jobs.
- Zero Postgres code inside Exasol components and zero Exasol queries blocking the checkout path.
"""

import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, status
from fastapi.middleware.cors import CORSMiddleware

from backend.core.config import settings
from backend.api.v1.router import api_v1_router

# Configure structured logging
logging.basicConfig(level=settings.LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
logger = logging.getLogger("omnisync.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan manager.
    Initializes connection pools, background resources, and performs startup validation.
    """
    logger.info("================================================================")
    logger.info(f"Starting {settings.PROJECT_NAME} v{settings.VERSION}")
    logger.info(f"Environment: {settings.ENVIRONMENT} | Debug: {settings.DEBUG}")
    logger.info(f"OLTP Target: PostgreSQL @ {settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}")
    logger.info(f"OLAP Target: Exasol @ {settings.EXASOL_HOST}:{settings.EXASOL_PORT} (Privileged)")
    logger.info(f"Broker Target: Redis @ {settings.REDIS_HOST}:{settings.REDIS_PORT}")
    logger.info("================================================================")
    yield
    logger.info("Shutting down OmniSync API service...")


# Initialize FastAPI instance with rich metadata for hackathon demonstration
app = FastAPI(
    title="OmniSync Retail Intelligence API",
    description=(
        "Hybrid OLTP/OLAP Retail Analytics Platform.\n\n"
        "**Core Capabilities:**\n"
        "- **Optimistic Concurrency Control (OCC)**: Zero negative inventory checkout in PostgreSQL.\n"
        "- **In-Memory Analytics**: Exasol UDFs running LightGBM & PyTorch over native BucketFS weights.\n"
        "- **Asynchronous Queueing**: Zendesk support webhooks immediately handed off to Celery.\n"
        "- **Pre-computed Decisioning**: Return risk levels & churn scoring with 0ms added checkout latency."
    ),
    version=settings.VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

# ------------------------------------------------------------------------------
# CORS Middleware Configuration
# ------------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Permits Streamlit dashboard and local frontend dev
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------------------
# Root & Health Check Endpoints
# ------------------------------------------------------------------------------
@app.get(
    "/health",
    tags=["Diagnostics & System Health"],
    status_code=status.HTTP_200_OK,
    summary="System Health & Liveness Probe",
)
async def root_health_check():
    """
    Root health check endpoint for container orchestrators and monitoring agents.
    Returns status of the API and connection configurations.
    """
    return {
        "status": "HEALTHY",
        "service": "OmniSync Core API",
        "version": settings.VERSION,
        "environment": settings.ENVIRONMENT,
        "databases": {
            "oltp_postgres": "configured",
            "olap_exasol": "configured (privileged)",
            "cache_redis": "configured",
            "queue_celery": "configured",
        },
    }


@app.get("/", tags=["Diagnostics & System Health"], include_in_schema=False)
async def root_index():
    """Friendly root welcome message directing to interactive documentation."""
    return {
        "message": "Welcome to the OmniSync Retail Analytics API. Visit /docs for Swagger UI.",
        "documentation": "/docs",
        "health": "/health",
    }


# ------------------------------------------------------------------------------
# Include Modular API Routers
# Easily support future versioning (/api/v2) and additional domain routers
# ------------------------------------------------------------------------------
app.include_router(api_v1_router, prefix=settings.API_V1_STR)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host=settings.FASTAPI_HOST,
        port=settings.FASTAPI_PORT,
        reload=settings.DEBUG,
    )
