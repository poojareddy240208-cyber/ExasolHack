"""PostgreSQL (OLTP) connection helper shared by the Module 3 services and scripts."""

from typing import Optional

import psycopg2

from backend.core.config import settings


def connect_postgres(host: Optional[str] = None):
    """
    Open a synchronous psycopg2 connection using the project settings.

    Args:
        host: override the configured host (use "localhost" when running outside docker-compose).
    """
    return psycopg2.connect(
        host=host or settings.POSTGRES_HOST,
        port=settings.POSTGRES_PORT,
        user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD,
        dbname=settings.POSTGRES_DB,
    )
