from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID


@dataclass(frozen=True, slots=True)
class TenantContext:
    tenant_id: UUID
    user_id: str
    role: str = "viewer"
    is_platform_admin: bool = False
    permissions: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_claims(cls, tenant_id: UUID, claims: dict) -> "TenantContext":
        user_id = str(claims.get("sub") or "").strip()
        if not user_id:
            raise ValueError("token subject is required")
        roles = set(claims.get("roles") or [])
        return cls(
            tenant_id=tenant_id,
            user_id=user_id,
            role="platform_admin" if "platform_admin" in roles else str(claims.get("role") or "viewer"),
            is_platform_admin="platform_admin" in roles,
            permissions=frozenset(str(item) for item in claims.get("permissions") or []),
        )

    def with_role(self, role: str) -> "TenantContext":
        return TenantContext(self.tenant_id, self.user_id, role, self.is_platform_admin, self.permissions)

    def has_any_role(self, *roles: str) -> bool:
        return self.is_platform_admin or self.role in roles

    def has_permission(self, permission: str) -> bool:
        return self.is_platform_admin or permission in self.permissions
