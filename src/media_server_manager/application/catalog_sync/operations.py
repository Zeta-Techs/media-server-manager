"""Application entry points for catalog and media-library synchronization."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from media_server_manager.application.media_operations import sync as typed_sync
from media_server_manager.infrastructure.db.session import Database
from media_server_manager.infrastructure.integrations.plex.client import PlexServer


def resolve_plex_rating_key(db_file: Path, server_id: int, library_id: int, rating_key: str) -> str:
    # Preserve the established extension seam: deployments and tests may
    # replace the client on the application operation module.
    typed_sync.PlexServer = PlexServer
    database = Database.for_path(db_file)
    try:
        return typed_sync.resolve_plex_rating_key(database, server_id, library_id, rating_key)
    finally:
        database.engine.dispose()


def sync_media_library_full(db_file: Path, server_id: int, library_id: int, **kwargs: Any) -> dict[str, Any]:
    database = Database.for_path(db_file)
    try:
        typed_sync.PlexServer = PlexServer
        plex_count = typed_sync.sync_media_library(database, server_id, library_id, **kwargs)
        typed_sync.sync_media_library_tmdb(database, server_id, library_id, **kwargs)
        return {"plex": plex_count}
    finally:
        database.engine.dispose()


def sync_media_library_tmdb(db_file: Path, server_id: int, library_id: int, **kwargs: Any) -> dict[str, Any]:
    database = Database.for_path(db_file)
    try:
        return typed_sync.sync_media_library_tmdb(database, server_id, library_id, **kwargs)
    finally:
        database.engine.dispose()


__all__ = ["resolve_plex_rating_key", "sync_media_library_full", "sync_media_library_tmdb"]
