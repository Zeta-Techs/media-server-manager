from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from apps.api.dependencies import get_db, get_tenant_context, require_roles
from apps.api.schemas.jobs import JobRead
from apps.api.schemas.servers import ServerCreate, ServerRead, ServerUpdate
from packages.application.job_orchestration import JobOrchestrationService
from packages.application.server_management import ServerManagementService
from packages.domain.servers.models import ServerDraft
from packages.domain.tenancy.context import TenantContext

router = APIRouter(prefix="/tenants/{tenant_id}/servers", tags=["servers"])


def serialize(server) -> dict:
    return {"id": server.id, "tenant_id": server.tenant_id, "name": server.name, "address": server.address, "enabled": server.enabled, "token_configured": bool(server.token), "pinyin_mode": server.pinyin_mode, "auth_source": server.auth_source}


def require_tenant(tenant_id: UUID, context: TenantContext) -> None:
    if context.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="tenant access denied")


@router.get("", response_model=list[ServerRead])
def list_servers(tenant_id: UUID, db: Session = Depends(get_db), context: TenantContext = Depends(get_tenant_context)):
    require_tenant(tenant_id, context)
    return [serialize(server) for server in ServerManagementService(db).list(context)]


@router.post("", response_model=ServerRead, status_code=status.HTTP_201_CREATED)
def create_server(tenant_id: UUID, payload: ServerCreate, db: Session = Depends(get_db), context: TenantContext = Depends(require_roles("tenant_admin"))):
    require_tenant(tenant_id, context)
    try:
        server = ServerManagementService(db).create(context, ServerDraft(**payload.model_dump()))
        db.commit()
        return serialize(server)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{server_id}", response_model=ServerRead)
def get_server(tenant_id: UUID, server_id: int, db: Session = Depends(get_db), context: TenantContext = Depends(get_tenant_context)):
    require_tenant(tenant_id, context)
    server = ServerManagementService(db).repository.get_by_id(context.tenant_id, server_id)
    if not server:
        raise HTTPException(status_code=404, detail="server not found")
    return serialize(server)


@router.patch("/{server_id}", response_model=ServerRead)
def update_server(tenant_id: UUID, server_id: int, payload: ServerUpdate, db: Session = Depends(get_db), context: TenantContext = Depends(require_roles("tenant_admin"))):
    require_tenant(tenant_id, context)
    service = ServerManagementService(db)
    existing = service.repository.get_by_id(context.tenant_id, server_id)
    if not existing:
        raise HTTPException(status_code=404, detail="server not found")
    values = {"name": existing.name, "address": existing.address, "token": existing.token, "enabled": existing.enabled, "pinyin_mode": existing.pinyin_mode, "skip_libraries": existing.skip_libraries}
    values.update({key: value for key, value in payload.model_dump().items() if value is not None})
    try:
        server = service.update(context, server_id, ServerDraft(**values))
        db.commit()
        return serialize(server)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_server(tenant_id: UUID, server_id: int, db: Session = Depends(get_db), context: TenantContext = Depends(require_roles("tenant_admin"))):
    require_tenant(tenant_id, context)
    try:
        ServerManagementService(db).delete(context, server_id)
        db.commit()
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{server_id}/test", response_model=JobRead, status_code=status.HTTP_202_ACCEPTED)
def test_server(tenant_id: UUID, server_id: int, request: Request, db: Session = Depends(get_db), context: TenantContext = Depends(require_roles("operator", "tenant_admin"))):
    require_tenant(tenant_id, context)
    try:
        job = JobOrchestrationService(db).create(context, "server_test", server_id, {}, getattr(request.state, "request_id", ""), None)
        db.commit()
        return {"id": job.id, "tenant_id": job.tenant_id, "type": job.type, "server_id": job.server_id, "status": job.status, "payload": {}, "error": "", "request_id": job.request_id, "created_at": job.created_at.isoformat() if job.created_at else None}
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{server_id}/webhook-secret/rotate")
def rotate_secret(tenant_id: UUID, server_id: int, request: Request, db: Session = Depends(get_db), context: TenantContext = Depends(require_roles("tenant_admin"))):
    require_tenant(tenant_id, context)
    try:
        secret = ServerManagementService(db).rotate_webhook_secret(context, server_id)
        db.commit()
        return {"ok": True, "webhook_secret": secret, "request_id": getattr(request.state, "request_id", "")}
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
