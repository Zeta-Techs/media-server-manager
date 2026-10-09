from __future__ import annotations

from celery import Celery

from apps.api.config import get_settings

settings = get_settings()
celery_app = Celery("media_server_manager", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_default_queue="media_sync",
    task_routes={
        "packages.infrastructure.queue.tasks.run_job": {"queue": "media_sync"},
    },
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_track_started=True,
    broker_connection_retry_on_startup=True,
    result_expires=86400,
    beat_schedule={
        "dispatch-job-outbox": {
            "task": "packages.infrastructure.queue.tasks.dispatch_outbox",
            "schedule": 5.0,
        }
    },
)
