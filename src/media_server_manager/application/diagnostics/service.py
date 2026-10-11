from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ...infrastructure.db.repositories.overview import OverviewRepository


class DiagnosticsService:
    """Coordinate dashboard aggregates without exposing persistence details to routes."""

    def __init__(self, session: Session) -> None:
        self.repository = OverviewRepository(session)

    def overview(self, limit: int = 5) -> dict[str, Any]:
        return self.repository.snapshot(limit)
