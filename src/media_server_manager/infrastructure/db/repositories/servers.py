from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Server


class ServerRepository:
    """Typed access to Plex server configuration."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, server_id: int) -> Server | None:
        return self.session.get(Server, server_id)

    def list_enabled(self) -> list[Server]:
        return list(self.session.scalars(select(Server).where(Server.enabled.is_(True)).order_by(Server.id)))

    def add(self, server: Server) -> Server:
        self.session.add(server)
        self.session.flush()
        return server
