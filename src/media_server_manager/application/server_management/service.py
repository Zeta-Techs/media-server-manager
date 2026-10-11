from __future__ import annotations

from datetime import datetime, timezone

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

    def all_servers(self) -> list[Server]:
        return self.repository.list_all()

    def create_server(
        self,
        *,
        name: str,
        address: str,
        token: str,
        skip_libraries: str = "",
        pinyin_mode: str = "first_letter",
        auth_source: str = "manual",
        webhook_secret: str,
        enabled: bool = True,
    ) -> Server:
        now = datetime.now(timezone.utc)
        return self.repository.add(
            Server(
                name=name,
                address=address,
                token=token,
                skip_libraries=skip_libraries,
                pinyin_mode=pinyin_mode,
                auth_source=auth_source,
                webhook_secret=webhook_secret,
                enabled=enabled,
                created_at=now,
                updated_at=now,
            )
        )

    def update_server(self, server: Server, **changes: object) -> Server:
        for key, value in changes.items():
            if value is not None and hasattr(server, key):
                setattr(server, key, value)
        server.updated_at = datetime.now(timezone.utc)
        self.repository.session.flush()
        return server

    def delete_server(self, server_id: int) -> bool:
        return self.repository.delete(server_id)
