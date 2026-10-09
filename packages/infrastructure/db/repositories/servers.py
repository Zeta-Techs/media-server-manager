from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.domain.servers.models import ServerDraft
from packages.infrastructure.db.models import Server


class ServerRepository:
    def __init__(self, session: Session):
        self.session = session

    def list_by_tenant(self, tenant_id: UUID) -> list[Server]:
        return list(self.session.scalars(select(Server).where(Server.tenant_id == tenant_id).order_by(Server.id)).all())

    def get_by_id(self, tenant_id: UUID, server_id: int, *, for_update: bool = False) -> Server | None:
        statement = select(Server).where(Server.tenant_id == tenant_id, Server.id == server_id)
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def create(self, tenant_id: UUID, draft: ServerDraft, *, webhook_secret: str, auth_source: str = "manual") -> Server:
        value = draft.validate()
        server = Server(tenant_id=tenant_id, name=value.name, address=value.address, token=value.token, enabled=value.enabled, pinyin_mode=value.pinyin_mode, skip_libraries=value.skip_libraries, webhook_secret=webhook_secret, auth_source=auth_source)
        self.session.add(server)
        self.session.flush()
        return server

    def update(self, server: Server, draft: ServerDraft) -> Server:
        value = draft.validate()
        server.name, server.address, server.token = value.name, value.address, value.token
        server.enabled, server.pinyin_mode, server.skip_libraries = value.enabled, value.pinyin_mode, value.skip_libraries
        self.session.flush()
        return server

    def delete(self, server: Server) -> None:
        self.session.delete(server)
