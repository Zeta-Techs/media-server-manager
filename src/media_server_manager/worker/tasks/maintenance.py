from .base import ApplicationTask


class MaintenanceTask(ApplicationTask):
    def __init__(self, task_manager) -> None:
        super().__init__(task_manager, "maintenance")

__all__ = ["MaintenanceTask"]
