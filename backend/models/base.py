"""
SQLAlchemy Declarative Base Configuration for OmniSync.
Provides the declarative base for all transactional OLTP models.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy ORM models in OmniSync."""

    pass
