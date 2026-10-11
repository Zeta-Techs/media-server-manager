"""Application service for durable job execution.

This is the stable dependency used by the Worker and Web layers.  The
remaining historical workflow code is kept behind the infrastructure adapter
until each handler is replaced by a typed domain operation.
"""

from __future__ import annotations

from media_server_manager.application.job_orchestration.executor import (
    TaskManager as _LegacyTaskManager,
)


class JobExecutionService(_LegacyTaskManager):
    """Execute a claimed job using the shared process database."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


TaskManager = JobExecutionService

__all__ = ["JobExecutionService", "TaskManager"]
