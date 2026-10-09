from __future__ import annotations

from typing import Any

import jwt

from apps.api.config import get_settings


class OIDCProvider:
    """Decode local development JWTs or validate production OIDC JWKS tokens."""

    def decode(self, token: str) -> dict[str, Any]:
        settings = get_settings()
        try:
            if settings.oidc_jwks_url:
                client = jwt.PyJWKClient(settings.oidc_jwks_url)
                key = client.get_signing_key_from_jwt(token).key
                return dict(
                    jwt.decode(
                        token,
                        key,
                        algorithms=["RS256", "RS384", "RS512"],
                        audience=settings.oidc_audience or None,
                        issuer=settings.oidc_issuer or None,
                    )
                )
            return dict(jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm], audience=None))
        except jwt.PyJWTError as exc:
            raise ValueError("invalid or expired access token") from exc
