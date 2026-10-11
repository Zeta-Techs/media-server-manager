from .base import ApplicationTask


class EpisodeAuditTask(ApplicationTask):
    def __init__(self, task_manager) -> None:
        super().__init__(task_manager, "episode_audit")

__all__ = ["EpisodeAuditTask"]
