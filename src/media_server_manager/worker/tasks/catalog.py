from .base import ApplicationTask


class CatalogTask(ApplicationTask):
    """TMDB catalog and Plex inventory synchronization handler."""

    def __init__(self, task_manager) -> None:
        super().__init__(
            task_manager,
            ("tmdb_catalog_sync", "plex_inventory_sync", "media_reconcile", "media_sync"),
        )

__all__ = ["CatalogTask"]
