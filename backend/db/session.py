"""
PostgreSQL Database Session and Engine Configuration for OmniSync.
Provides database connection pooling and FastAPI session dependencies.
"""

from typing import Generator
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from backend.core.config import settings

# Synchronous engine for FastAPI endpoints & transactional OCC operations
engine = create_engine(
    settings.POSTGRES_SYNC_URL,
    pool_pre_ping=True,
    pool_size=20,
    max_overflow=40,
    echo=False,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Generator[Session, None, None]:
    """
    FastAPI dependency that yields a SQLAlchemy database session per request.
    Closes the session when the request completes.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
