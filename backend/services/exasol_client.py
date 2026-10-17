"""
Shared Exasol connection and SQL-file helpers.

Exasol is a passive analytics database: this module only opens connections and runs SQL that a
caller (a Celery Beat task or a CLI script) explicitly asks for.
"""

import re
import ssl
from pathlib import Path
from typing import Dict, List, Optional

import pyexasol

from backend.core.config import settings


def connect_exasol(
    host: Optional[str] = None, schema: Optional[str] = None, autocommit: bool = True
) -> pyexasol.ExaConnection:
    """
    Open a connection to Exasol using the project settings.

    Args:
        host: override the configured host (use "localhost" when running outside docker-compose).
        schema: schema to open after connecting.
        autocommit: pass False to run several statements as one transaction.
    """
    # The Exasol docker image ships a self-signed certificate, so certificate checks are
    # disabled for this local development connection only.
    return pyexasol.connect(
        dsn=f"{host or settings.EXASOL_HOST}:{settings.EXASOL_PORT}",
        user=settings.EXASOL_USER,
        password=settings.EXASOL_PASSWORD,
        schema=schema or "",
        autocommit=autocommit,
        encryption=True,
        websocket_sslopt={"cert_reqs": ssl.CERT_NONE},
    )


def split_sql_statements(sql_text: str) -> List[str]:
    """Drop `--` comments and split on `;` (Exasol executes one statement per call)."""
    without_comments = re.sub(r"--[^\n]*", "", sql_text)
    return [stmt.strip() for stmt in without_comments.split(";") if stmt.strip()]


def load_sql_statements(sql_path: Path, params: Dict[str, float]) -> List[str]:
    """Read a SQL file, fill in ${NAME} placeholders and split it into single statements."""
    # Only the ${NAME} form is replaced, so JSON paths such as '$.email_hint' pass through untouched.
    text = sql_path.read_text(encoding="utf-8")
    filled = re.sub(r"\$\{(\w+)\}", lambda match: str(params[match.group(1)]), text)
    return split_sql_statements(filled)
