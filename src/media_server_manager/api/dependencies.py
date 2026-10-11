"""Request-time dependencies shared by the Flask route modules."""

from __future__ import annotations

from pathlib import Path

from flask import current_app

from ..infrastructure.db.session import Database


def get_database() -> Database:
    """Return the application-scoped database handle.

    The factory creates one engine/session factory per process.  Routes only
    request short-lived sessions or unit-of-work transactions from it.
    """

    return current_app.extensions["database"]


def get_database_path() -> Path:
    return current_app.extensions["db_file"]


__all__ = ["get_database", "get_database_path"]
