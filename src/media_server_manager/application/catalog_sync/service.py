from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from media_server_manager.infrastructure.db.repositories.catalog import CatalogRepository
from media_server_manager.infrastructure.db.session import Database

from ..media_operations.sync import reconcile as typed_reconcile
from ..media_operations.sync import resolve_plex_rating_key as typed_resolve_plex_rating_key
from ..media_operations.sync import sync_media_library as typed_sync_media_library
from ..media_operations.sync import sync_media_library_tmdb as typed_sync_media_library_tmdb
from .workflow import scan_plex_inventory as typed_scan_plex_inventory
from .workflow import sync_catalog as typed_sync_catalog


class CatalogService:
    """Transactional catalog queries used by HTTP and worker adapters."""

    def __init__(self, session: Session) -> None:
        self.repository = CatalogRepository(session)

    def list_items(self, **filters: Any) -> tuple[list[dict[str, Any]], int]:
        return self.repository.list_items(**filters)

    def detail(self, media_type: str, tmdb_id: int) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        return self.repository.detail(media_type, tmdb_id)

    def stats(self) -> tuple[dict[str, int], dict[str, int]]:
        return self.repository.stats()


class CatalogSyncService:
    """Coordinate external catalog/media synchronization workflows.

    The repository functions retain their public compatibility signatures for
    old plugins, while Worker and Application code depend on this service and
    its process-scoped ``Database`` handle.
    """

    def __init__(self, database: Database, database_path: Path | None = None) -> None:
        self.database = database
        self.database_path = Path(database_path or database.settings.database_path)

    def sync_catalog(self, payload: dict[str, Any], *, progress=None, cancelled=None) -> dict[str, Any]:
        return typed_sync_catalog(self.database, payload, progress=progress, cancelled=cancelled)

    def scan_plex_inventory(
        self, server_id: int, library_ids: list[int] | None = None, *, progress=None
    ) -> int:
        return typed_scan_plex_inventory(self.database, server_id, library_ids, progress=progress)

    def reconcile(self, server_id: int) -> int:
        return typed_reconcile(self.database, server_id)

    def sync_media_library(
        self, server_id: int, library_id: int, *, progress=None, cancelled=None, **options: Any
    ) -> int:
        return typed_sync_media_library(
            self.database,
            server_id,
            library_id,
            progress=progress,
            cancelled=cancelled,
            **options,
        )

    def sync_media_library_tmdb(
        self, server_id: int, library_id: int, *, progress=None, cancelled=None, **options: Any
    ) -> int:
        return typed_sync_media_library_tmdb(
            self.database,
            server_id,
            library_id,
            progress=progress,
            cancelled=cancelled,
            **options,
        )

    def resolve_plex_rating_key(self, server_id: int, library_id: int, rating_key: str) -> str:
        return typed_resolve_plex_rating_key(self.database, server_id, library_id, rating_key)
