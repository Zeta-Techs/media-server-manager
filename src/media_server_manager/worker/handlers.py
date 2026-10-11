from __future__ import annotations

from pathlib import Path

from .contracts import JobHandler
from .tasks.base import ApplicationTask
from .tasks.catalog import CatalogTask
from .tasks.continue_watching import ContinueWatchingTask
from .tasks.episode_audit import EpisodeAuditTask
from .tasks.maintenance import MaintenanceTask
from .tasks.media_library import MediaLibraryTask
from .tasks.notifications import NotificationTask


class ApplicationTaskHandler(ApplicationTask):
    """Adapter for the existing task implementation during extraction."""

    pass


def handler_registry(task_manager, database: Path) -> dict[str, JobHandler]:
    del database
    handlers: dict[str, JobHandler] = {}
    for handler in (
        CatalogTask(task_manager),
        MediaLibraryTask(task_manager),
        EpisodeAuditTask(task_manager),
        ContinueWatchingTask(task_manager),
        NotificationTask(task_manager),
        MaintenanceTask(task_manager),
    ):
        for task_type in handler.job_types:
            handlers[task_type] = handler
    for job_type in ("all", "localize", "continue_watching"):
        handlers[job_type] = ApplicationTaskHandler(task_manager, job_type)
    return handlers
