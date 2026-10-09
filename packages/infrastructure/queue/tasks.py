from __future__ import annotations

import json

from celery import Task
from sqlalchemy import text

from packages.infrastructure.db.session import SessionFactory

from .celery_app import celery_app


class TenantTask(Task):
    autoretry_for = (TimeoutError, ConnectionError)
    retry_backoff = True
    retry_kwargs = {"max_retries": 5}


@celery_app.task(bind=True, base=TenantTask, name="packages.infrastructure.queue.tasks.run_job")
def run_job(self, job_id: str, tenant_id: str) -> dict[str, str]:
    """Dispatch a persisted job to a domain handler.

    The first implementation deliberately keeps the durable state transition in
    PostgreSQL and leaves Plex-specific handlers behind an explicit seam. This
    makes queue delivery and tenant propagation testable before moving every
    legacy task implementation.
    """
    with SessionFactory.begin() as session:
        row = session.execute(
            text("SELECT id, tenant_id, type, payload FROM msm_jobs WHERE id = :id AND tenant_id = :tenant_id"),
            {"id": job_id, "tenant_id": tenant_id},
        ).mappings().first()
        if not row:
            return {"job_id": job_id, "status": "missing"}
        session.execute(text("UPDATE msm_jobs SET status='running' WHERE id=:id AND status='queued'"), {"id": job_id})
    try:
        payload = json.loads(row["payload"] or "{}")
        # Legacy TaskManager remains the compatibility implementation during migration.
        from apps.api.config import get_settings
        from media_server_manager_web.tasks import TaskManager
        TaskManager(get_settings().database_url.replace("sqlite:///", ""))._run_job(int(payload["legacy_job_id"])) if payload.get("legacy_job_id") else None
        result = {"job_id": job_id, "status": "succeeded"}
    except Exception as exc:
        with SessionFactory.begin() as session:
            session.execute(text("UPDATE msm_jobs SET status='failed', error=:error WHERE id=:id"), {"id": job_id, "error": str(exc)})
        raise
    with SessionFactory.begin() as session:
        session.execute(text("UPDATE msm_jobs SET status=:status WHERE id=:id"), {"status": result["status"], "id": job_id})
    return result

