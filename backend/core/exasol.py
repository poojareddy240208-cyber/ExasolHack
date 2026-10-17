"""
OmniSync Exasol Connection & Staging Management.

Provides PyExasol connection helpers and schema/table initialization for
the Exasol Personal Edition analytics engine (OLAP).
Strict Architectural Invariants:
- Exasol is a passive analytics engine.
- Zero Postgres code inside Exasol components.
- Staging and UDF model execution occur directly within Exasol.
"""

import logging
from typing import Any, Dict, Optional
import pyexasol

from backend.core.config import settings

logger = logging.getLogger("omnisync.exasol")

# Schema & Table DDL definitions
CREATE_SCHEMA_QUERY = "CREATE SCHEMA IF NOT EXISTS OMNISYNC_ANALYTICS;"

CREATE_STAGING_TABLE_QUERY = """
CREATE TABLE IF NOT EXISTS OMNISYNC_ANALYTICS.STAGING_ZENDESK_TICKETS (
    TICKET_ID VARCHAR(64) PRIMARY KEY,
    USER_ID VARCHAR(64),
    RAW_TEXT VARCHAR(4000),
    CLEANED_TEXT VARCHAR(4000),
    CREATED_AT TIMESTAMP,
    STATUS VARCHAR(20) DEFAULT 'PENDING'
);
"""

# In-memory flag to avoid redundant table creation checks in steady-state operations
_STAGING_TABLE_INITIALIZED = False


def get_exasol_connection(
    autocommit: bool = True,
    encryption: bool = False,
    connection_timeout: int = 10,
    **kwargs: Any,
) -> pyexasol.ExaConnection:
    """
    Establishes and returns a pyexasol connection to Exasol Personal Edition.
    Reads host, port, user, and password from backend.core.config.settings.

    Args:
        autocommit: Whether to automatically commit statements (default True).
        encryption: Whether to use TLS encryption (default False for local container setup).
        connection_timeout: Maximum seconds to wait during connection handshake.
        **kwargs: Additional parameters passed to pyexasol.connect.

    Returns:
        pyexasol.ExaConnection instance.
    """
    conn_params: Dict[str, Any] = {
        "dsn": f"{settings.EXASOL_HOST}:{settings.EXASOL_PORT}",
        "user": settings.EXASOL_USER,
        "password": settings.EXASOL_PASSWORD,
        "autocommit": autocommit,
        "encryption": encryption,
        "connection_timeout": connection_timeout,
    }
    conn_params.update(kwargs)
    logger.debug(
        f"Connecting to Exasol at {settings.EXASOL_HOST}:{settings.EXASOL_PORT} as user '{settings.EXASOL_USER}'"
    )
    return pyexasol.connect(**conn_params)


def init_staging_table(conn: Optional[pyexasol.ExaConnection] = None) -> None:
    """
    Ensures that the OMNISYNC_ANALYTICS schema and STAGING_ZENDESK_TICKETS table exist in Exasol.
    If a connection is not provided, a temporary connection is opened and closed.

    Args:
        conn: Optional active pyexasol connection.
    """
    global _STAGING_TABLE_INITIALIZED
    should_close = False
    if conn is None:
        conn = get_exasol_connection()
        should_close = True

    try:
        logger.info("Verifying/creating Exasol schema and staging table OMNISYNC_ANALYTICS.STAGING_ZENDESK_TICKETS...")
        conn.execute(CREATE_SCHEMA_QUERY)
        conn.execute(CREATE_STAGING_TABLE_QUERY)
        _STAGING_TABLE_INITIALIZED = True
        logger.info("Exasol staging table OMNISYNC_ANALYTICS.STAGING_ZENDESK_TICKETS verified successfully.")
    except Exception as exc:
        logger.error(f"Failed to initialize Exasol staging table: {exc}")
        raise
    finally:
        if should_close:
            conn.close()


def insert_staging_ticket(
    ticket_id: str,
    user_id: Optional[str],
    raw_text: str,
    cleaned_text: str,
    created_at: str,
    status: str = "PENDING",
    conn: Optional[pyexasol.ExaConnection] = None,
) -> None:
    """
    Inserts a cleaned Zendesk ticket record into OMNISYNC_ANALYTICS.STAGING_ZENDESK_TICKETS using pyexasol.
    Initializes the staging table on first invocation if not already completed.

    Args:
        ticket_id: Zendesk unique ticket identifier.
        user_id: Associated customer identifier or contact email.
        raw_text: Original raw ticket text or comment body.
        cleaned_text: Sanitized comment text processed by nlp_cleaner.
        created_at: Formatted timestamp string ('YYYY-MM-DD HH:MM:SS').
        status: Initial processing status (default 'PENDING').
        conn: Optional active pyexasol connection.
    """
    should_close = False
    if conn is None:
        conn = get_exasol_connection()
        should_close = True

    try:
        if not _STAGING_TABLE_INITIALIZED:
            init_staging_table(conn)

        insert_sql = """
        INSERT INTO OMNISYNC_ANALYTICS.STAGING_ZENDESK_TICKETS
        (TICKET_ID, USER_ID, RAW_TEXT, CLEANED_TEXT, CREATED_AT, STATUS)
        VALUES ({ticket_id}, {user_id}, {raw_text}, {cleaned_text}, {created_at}, {status})
        """
        params = {
            "ticket_id": str(ticket_id)[:64],
            "user_id": str(user_id)[:64] if user_id else None,
            "raw_text": str(raw_text)[:4000],
            "cleaned_text": str(cleaned_text)[:4000],
            "created_at": str(created_at),
            "status": str(status)[:20],
        }
        conn.execute(insert_sql, query_params=params)
        logger.info(f"Inserted ticket {ticket_id} into OMNISYNC_ANALYTICS.STAGING_ZENDESK_TICKETS.")
    except pyexasol.ExaQueryError as eqe:
        # If table was dropped or missing, attempt self-healing initialization and retry once
        logger.warning(f"Exasol query error during staging insert ({eqe}). Retrying after table initialization...")
        init_staging_table(conn)
        conn.execute(insert_sql, query_params=params)
    finally:
        if should_close:
            conn.close()


def register_churn_udf(
    conn: Optional[pyexasol.ExaConnection] = None,
    bucketfs_path: Optional[str] = None,
) -> None:
    """
    Registers the OMNISYNC_ANALYTICS.PREDICT_CHURN_SCORE Python3 scalar UDF in Exasol.
    If conn is None, creates a temporary connection.

    Args:
        conn: Optional active pyexasol connection.
        bucketfs_path: Optional custom mounted container path to model weights in BucketFS.
    """
    from backend.core.exasol_udf import get_churn_udf_sql

    should_close = False
    if conn is None:
        conn = get_exasol_connection()
        should_close = True

    try:
        logger.info("Registering OMNISYNC_ANALYTICS.PREDICT_CHURN_SCORE UDF in Exasol...")
        conn.execute(CREATE_SCHEMA_QUERY)
        udf_sql = get_churn_udf_sql(bucketfs_model_path=bucketfs_path)
        conn.execute(udf_sql)
        logger.info("OMNISYNC_ANALYTICS.PREDICT_CHURN_SCORE UDF registered successfully.")
    except Exception as exc:
        logger.error(f"Failed to register churn UDF in Exasol: {exc}")
        raise
    finally:
        if should_close:
            conn.close()
