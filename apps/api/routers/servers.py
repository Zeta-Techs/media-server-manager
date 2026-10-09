from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from apps.api.dependencies import get_db, get_tenant_context, require_roles
from apps.api.schemas.servers import ServerCreate, ServerRead
from packages.domain.tenancy.context import TenantContext

router = APIRouter(prefix="/tenants/{tenant_id}/servers", tags=["servers"])


@router.get("", response_model=list[ServerRead])
def list_servers(
    tenant_id: UUID,
    db: Session = Depends(get_db),
    context: TenantContext = Depends(get_tenant_context),
):
    if context.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="tenant access denied")
    rows = db.execute(text("SELECT id, tenant_id, name, address, enabled, token FROM msm_servers WHERE tenant_id = :tenant_id ORDER BY id"), {"tenant_id": str(tenant_id)}).mappings()
    return [{**dict(row), "token_configured": bool(row["token"])} for row in rows]


@router.post("", response_model=ServerRead, status_code=status.HTTP_201_CREATED)
def create_server(
    tenant_id: UUID,
    payload: ServerCreate,
    db: Session = Depends(get_db),
    context: TenantContext = Depends(require_roles("tenant_admin", "operator")),
):
    if context.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="tenant access denied")
    row = db.execute(text("""
        INSERT INTO msm_servers (tenant_id, name, address, token, enabled)
        VALUES (:tenant_id, :name, :address, :token, :enabled)
        RETURNING id, tenant_id, name, address, enabled, token
    """), {"tenant_id": str(tenant_id), **payload.model_dump()}).mappings().one()
    db.commit()
    return {**dict(row), "token_configured": bool(row["token"])}
