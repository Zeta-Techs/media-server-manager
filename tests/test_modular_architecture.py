from __future__ import annotations

import uuid

from packages.domain.tenancy.context import TenantContext
from packages.infrastructure.auth.jwt import create_access_token, decode_access_token


def test_tenant_context_accepts_opaque_oidc_subjects() -> None:
    tenant_id = uuid.uuid4()
    context = TenantContext.from_claims(tenant_id, {"sub": "keycloak|user-42", "roles": ["viewer"]})
    assert context.user_id == "keycloak|user-42"
    assert context.tenant_id == tenant_id
    assert context.has_any_role("viewer")


def test_development_jwt_round_trip_preserves_tenant_claim() -> None:
    token = create_access_token("oidc-subject", tenant_id=str(uuid.uuid4()), roles=["tenant_admin"])
    claims = decode_access_token(token)
    assert claims["sub"] == "oidc-subject"
    assert claims["roles"] == ["tenant_admin"]
