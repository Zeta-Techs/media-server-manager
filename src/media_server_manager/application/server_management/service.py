from __future__ import annotations

from sqlalchemy.orm import Session

from ...infrastructure.db.models import Server
from ...infrastructure.db.repositories.servers import ServerRepository


class ServerManagementService:
    def __init__(self, session: Session) -> None:
        self.repository = ServerRepository(session)

    def get_server(self, server_id: int) -> Server | None:
        return self.repository.get(server_id)

    def enabled_servers(self) -> list[Server]:
        return self.repository.list_enabled()
