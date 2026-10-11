from .base import ApplicationTask


class MediaLibraryTask(ApplicationTask):
    def __init__(self, task_manager) -> None:
        super().__init__(
            task_manager,
            (
                "media_library_refresh",
                "media_library_tmdb_refresh",
                "media_library_full_refresh",
                "media_library_show_recheck",
                "media_library_bulk_resolve",
                "media_library_bulk_confirm",
                "media_library_search_add",
            ),
        )

__all__ = ["MediaLibraryTask"]
