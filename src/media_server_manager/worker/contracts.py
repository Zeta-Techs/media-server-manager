from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from media_server_manager.infrastructure.db.session import Database, UnitOfWork


@dataclass(frozen=True)
class JobContext:
    job_id: int
    server_id: int
    database: Database
    payload: dict[str, Any]

    def session(self):
        """Open a short-lived SQLAlchemy session for the task."""

        return self.database.session()

    def transaction(self, *, immediate: bool = False) -> UnitOfWork:
        """Create the task's transaction boundary."""

        return self.database.transaction(immediate=immediate)


@dataclass(frozen=True)
class JobResult:
    status: str
    message: str = ""


class JobHandler(Protocol):
    job_type: str

    def run(self, context: JobContext) -> JobResult:
        ...
