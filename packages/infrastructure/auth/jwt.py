from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

from apps.api.config import get_settings
from packages.integrations.oidc.provider import OIDCProvider


def create_access_token(subject: str, *, tenant_id: str | None = None, roles: list[str] | None = None) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": now,
        "exp": now + timedelta(seconds=settings.access_token_ttl_seconds),
        "roles": roles or [],
    }
    if tenant_id:
        payload["tenant_id"] = tenant_id
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        return OIDCProvider().decode(token)
    except ValueError:
        raise
