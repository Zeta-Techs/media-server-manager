from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.dependencies import get_current_claims, get_db, require_platform_admin
from apps.api.schemas.tenants import TenantCreate, TenantRead
from packages.infrastructure.db.models import Tenant

router = APIRouter(prefix="/tenants", tags=["tenants"])


@router.get("", response_model=list[TenantRead])
def list_tenants(db: Session = Depends(get_db), claims: dict = Depends(get_current_claims)):
    # Listing all tenants is a platform operation; the endpoint is intentionally
    # wired through a dedicated claim check in deployments using OIDC.
    if "platform_admin" not in set(claims.get("roles") or []):
        raise HTTPException(status_code=403, detail="platform admin required")
    return list(db.scalars(select(Tenant).order_by(Tenant.name)).all())


@router.post("", response_model=TenantRead, status_code=status.HTTP_201_CREATED)
def create_tenant(
    payload: TenantCreate,
    db: Session = Depends(get_db),
    _claims: dict = Depends(require_platform_admin),
):
    tenant = Tenant(slug=payload.slug, name=payload.name, plan=payload.plan)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant
