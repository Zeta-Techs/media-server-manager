"""Compatibility exports for the job orchestration application service."""

from media_server_manager.application.job_orchestration import executor as _execution_module
from media_server_manager.infrastructure.integrations.plex.client import PlexServer

from .service import JobExecutionService as _JobExecutionService


class JobExecutionService(_JobExecutionService):
    def __init__(self, *args, **kwargs):
        # Keep the historical monkeypatch/import seam available to extensions.
        _execution_module.PlexServer = PlexServer
        super().__init__(*args, **kwargs)


TaskManager = JobExecutionService

__all__ = ["JobExecutionService", "TaskManager"]

