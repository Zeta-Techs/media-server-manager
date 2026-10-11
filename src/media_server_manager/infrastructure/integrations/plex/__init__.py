"""Plex integration exports loaded lazily to keep database startup acyclic."""

from __future__ import annotations

_AUTH_EXPORTS = {
    "PLEX_APP_AUTH",
    "PLEX_TV",
    "build_auth_url",
    "check_pin",
    "choose_best_connection",
    "create_pin",
    "discover_servers",
    "get_client_identifier",
    "normalize_connections",
}


def __getattr__(name: str):
    if name == "PlexServer":
        from .client import PlexServer

        return PlexServer
    if name in _AUTH_EXPORTS:
        from . import auth

        return getattr(auth, name)
    raise AttributeError(name)


__all__ = sorted(_AUTH_EXPORTS | {"PlexServer"})
