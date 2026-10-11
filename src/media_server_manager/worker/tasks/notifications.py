from .base import ApplicationTask


class NotificationTask(ApplicationTask):
    def __init__(self, task_manager) -> None:
        super().__init__(task_manager, "notification_event")

__all__ = ["NotificationTask"]
