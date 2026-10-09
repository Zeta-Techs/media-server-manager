from __future__ import annotations

import secrets

from packages.domain.servers.models import ServerDraft
from packages.domain.tenancy.context import TenantContext
from packages.infrastructure.db.repositories.jobs import JobRepository
from packages.infrastructure.db.repositories.servers import ServerRepository
from packages.integrations.plex.client import PlexClient, PlexServerInfo


class ServerManagementService:
    def __init__(self, session):
        self.repository = ServerRepository(session)
        self.session = session

    def list(self, context: TenantContext):
        return self.repository.list_by_tenant(context.tenant_id)

    def create(self, context: TenantContext, draft: ServerDraft):
        return self.repository.create(context.tenant_id, draft, webhook_secret=secrets.token_urlsafe(32))

    def update(self, context: TenantContext, server_id: int, draft: ServerDraft):
        server = self.repository.get_by_id(context.tenant_id, server_id, for_update=True)
        if not server:
            raise LookupError("server not found")
        return self.repository.update(server, draft)

    def delete(self, context: TenantContext, server_id: int) -> None:
        server = self.repository.get_by_id(context.tenant_id, server_id, for_update=True)
        if not server:
            raise LookupError("server not found")
        JobRepository(self.session).cancel_queued_for_server(context.tenant_id, server_id)
        self.repository.delete(server)

    def test_connection(self, context: TenantContext, server_id: int) -> PlexServerInfo:
        server = self.repository.get_by_id(context.tenant_id, server_id)
        if not server:
            raise LookupError("server not found")
        return PlexClient(server.address, server.token).test_connection()

    def rotate_webhook_secret(self, context: TenantContext, server_id: int) -> str:
        server = self.repository.get_by_id(context.tenant_id, server_id, for_update=True)
        if not server:
            raise LookupError("server not found")
        server.webhook_secret = secrets.token_urlsafe(32)
        self.session.flush()
        return server.webhook_secret
