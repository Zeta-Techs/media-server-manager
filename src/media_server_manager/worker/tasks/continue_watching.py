from .base import ApplicationTask


class ContinueWatchingTask(ApplicationTask):
    def __init__(self, task_manager) -> None:
        super().__init__(task_manager, ("continue_watching_preview", "continue_watching_apply"))

__all__ = ["ContinueWatchingTask"]
