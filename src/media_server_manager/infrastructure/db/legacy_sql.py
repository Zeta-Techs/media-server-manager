"""Deprecated compatibility import for the SQLAlchemy repository boundary."""

from .sql import (
    LegacyConnection,
    LegacyCursor,
    SessionSqlConnection,
    connect_legacy,
    connect_session,
    execute_legacy,
)

__all__ = [
    "LegacyConnection",
    "LegacyCursor",
    "SessionSqlConnection",
    "connect_legacy",
    "connect_session",
    "execute_legacy",
]