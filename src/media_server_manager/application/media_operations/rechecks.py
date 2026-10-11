"""Application service for automatic media-library rechecks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from media_server_manager.infrastructure.db.repositories.rechecks import RecheckRepository
from media_server_manager.infrastructure.db.session import Database


def _session_from_compat(value: Any) -> Session:
    """Accept the old session-backed connection during the API transition."""

    if isinstance(value, Session):
        return value
    session = getattr(value, "sqlalchemy_session", None)
    if isinstance(session, Session):
        return session
    raise TypeError("request_recheck 需要 SQLAlchemy Session 或其兼容连接")


def request_recheck(session: Any, server_id: int, event: str, metadata: dict[str, Any]) -> str:
    return RecheckRepository(_session_from_compat(session)).request(server_id, event, metadata)


def enqueue_due_rechecks(db_file: Path) -> None:
    with Database.for_path(db_file).transaction() as uow:
        RecheckRepository(uow.session).enqueue_due()


def retry_recheck(db_file: Path, source_job: dict[str, Any]) -> int:
    with Database.for_path(db_file).transaction() as uow:
        return RecheckRepository(uow.session).retry(source_job)
