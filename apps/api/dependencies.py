from __future__ import annotations

from collections.abc import Generator
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Path, status
from sqlalchemy import text

from packages.domain.tenancy.context import TenantContext
from packages.infrastructure.auth.jwt import decode_access_token
from packages.infrastructure.db.session import SessionFactory
from packages.infrastructure.observability.context import RequestContext


def get_db() -> Generator:
    with SessionFactory() as session:
        yield session


def get_request_context(x_request_id: str | None = Header(default=None)) -> RequestContext:
    return RequestContext(request_id=x_request_id)


def get_current_claims(authorization: str | None = Header(default=None)) -> dict:
    if not authorization:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing authorization")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid authorization")
    try:
        return decode_access_token(token)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc


def get_tenant_context(
    tenant_id: UUID = Path(...),
    claims: dict = Depends(get_current_claims),
    db=Depends(get_db),
) -> TenantContext:
    context = TenantContext.from_claims(tenant_id=tenant_id, claims=claims)
    if db.bind and db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_id)})
    if context.is_platform_admin:
        return context
    membership = db.execute(
        text("""
            SELECT m.role, m.status
            FROM tenant_memberships m
            JOIN users_v2 u ON u.id = m.user_id
            WHERE m.tenant_id = :tenant_id AND u.oidc_subject = :subject
        """),
        {"tenant_id": str(tenant_id), "subject": context.user_id},
    ).mappings().first()
    if not membership or membership["status"] != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="tenant access denied")
    return context.with_role(str(membership["role"]))


def require_roles(*roles: str):
    def dependency(context: TenantContext = Depends(get_tenant_context)) -> TenantContext:
        if not context.has_any_role(*roles):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient permissions")
        return context

    return dependency


def require_platform_admin(claims: dict = Depends(get_current_claims)) -> dict:
    if "platform_admin" not in set(claims.get("roles") or []):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="platform admin required")
    return claims
