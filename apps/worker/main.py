import packages.infrastructure.queue.tasks  # noqa: F401
from packages.infrastructure.queue.celery_app import celery_app

__all__ = ["celery_app"]
