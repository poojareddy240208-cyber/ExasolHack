"""
OmniSync Core Configuration Module.

Loads and validates environment variables using Pydantic v2 settings management.
Enforces type safety and strict isolation of database connection credentials.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application runtime and infrastructure settings.
    Default values match local docker-compose network bindings.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # General Environment
    PROJECT_NAME: str = "OmniSync Retail Analytics Platform"
    VERSION: str = "0.1.0"
    ENVIRONMENT: str = "development"
    DEBUG: bool = True
    LOG_LEVEL: str = "INFO"

    # API Server Config
    FASTAPI_HOST: str = "0.0.0.0"
    FASTAPI_PORT: int = 8000
    API_V1_STR: str = "/api/v1"
    API_SECRET_KEY: str = "omnisync_super_secret_jwt_and_session_signing_key_2026"

    # =========================================================================
    # OLTP: PostgreSQL Database Configuration
    # Dedicated to ACID transactions, OCC inventory checkout ledgers, and catalog.
    # =========================================================================
    POSTGRES_USER: str = "omnisync"
    POSTGRES_PASSWORD: str = "omnisync_secret"
    POSTGRES_DB: str = "omnisync_db"
    POSTGRES_HOST: str = "postgres"
    POSTGRES_PORT: int = 5432
    POSTGRES_URL: str = "postgresql+asyncpg://omnisync:omnisync_secret@postgres:5432/omnisync_db"
    POSTGRES_SYNC_URL: str = "postgresql://omnisync:omnisync_secret@postgres:5432/omnisync_db"

    # =========================================================================
    # OLAP: Exasol Personal Edition Configuration (The Brain)
    # Dedicated in-memory analytics, PyTorch/LightGBM UDFs, BucketFS weight store.
    # Architectural rule: Passive database; never triggers sync on its own.
    # =========================================================================
    EXASOL_HOST: str = "exasol"
    EXASOL_PORT: int = 8563
    EXASOL_USER: str = "sys"
    EXASOL_PASSWORD: str = "exasol"
    EXASOL_SCHEMA: str = "OMNISYNC_ANALYTICS"
    EXASOL_URL: str = "exasol://sys:exasol@exasol:8563"
    EXASOL_BUCKETFS_PORT: int = 2580
    EXASOL_BUCKETFS_SERVICE: str = "bfsdefault"
    EXASOL_BUCKETFS_BUCKET: str = "models"
    EXASOL_BUCKETFS_PASSWORD: str = "write_password"

    # =========================================================================
    # Cache & Event Store: Redis
    # Real-time cart velocity tracking (5-minute rolling window) & Celery queue.
    # =========================================================================
    REDIS_HOST: str = "redis"
    REDIS_PORT: int = 6379
    REDIS_URL: str = "redis://redis:6379/0"

    # =========================================================================
    # Message Broker: Celery
    # Asynchronous ticket processing & periodic Exasol-to-Postgres data syncing.
    # =========================================================================
    CELERY_BROKER_URL: str = "redis://redis:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://redis:6379/2"

    # =========================================================================
    # Integrations & Security
    # =========================================================================
    ZENDESK_WEBHOOK_SECRET: str = "sk_live_zendesk_webhook_secret_placeholder_9482736"

    # =========================================================================
    # System Standardized Thresholds (from PRD Section 2)
    # =========================================================================
    CHURN_SCORE_THRESHOLD: float = 0.85
    VIP_LTV_PERCENTILE: float = 0.80  # Top 20%
    CART_VELOCITY_WINDOW_MINUTES: int = 5


# Singleton instance for configuration access across the application
settings = Settings()
