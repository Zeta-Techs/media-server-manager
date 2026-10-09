from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import UUID

from celery import Task
from sqlalchemy import select

from packages.infrastructure.db.models import Job, JobDispatchOutbox, JobEvent, JobLog, Server
from packages.infrastructure.db.repositories.jobs import JobRepository
from packages.infrastructure.db.session import SessionFactory
from packages.integrations.plex.client import PlexClient

from .celery_app import celery_app
from .locks import plex_lock


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class TenantTask(Task):
    autoretry_for = (TimeoutError, ConnectionError)
    retry_backoff = True
    retry_kwargs = {"max_retries": 5}


def _event(session, job: Job, event_type: str, payload: dict | None = None) -> None:
    session.add(JobEvent(tenant_id=job.tenant_id, job_id=job.id, event_type=event_type, request_id=job.request_id, payload=json.dumps(payload or {}, ensure_ascii=False)))


def _log(session, job: Job, message: str, level: str = "info") -> None:
    session.add(JobLog(tenant_id=job.tenant_id, job_id=job.id, message=message, level=level))


def _server_test(session, job: Job) -> dict:
    server = session.scalar(select(Server).where(Server.tenant_id == job.tenant_id, Server.id == job.server_id))
    if not server:
        raise ValueError("server not found")
    info = PlexClient(server.address, server.token).test_connection()
    return {"name": info.name, "version": info.version, "product": info.product, "platform": info.platform}


def _run_plex_job(session, job: Job) -> dict:
    if job.type == "server_test":
        return _server_test(session, job)
    server = session.scalar(select(Server).where(Server.tenant_id == job.tenant_id, Server.id == job.server_id))
    if not server:
        raise ValueError("server not found")
    client = PlexClient(server.address, server.token)
    with plex_lock(str(job.tenant_id), int(server.id)):
        if job.type == "media_library_refresh":
            library_id = int((json.loads(job.payload or "{}")).get("library_id") or 0)
            if not library_id:
                return {"libraries": len(client.list_libraries())}
            return client.scan_library(library_id)
        if job.type in {"localize", "apply_change_set", "rollback"}:
            return {"accepted": True, "operation": job.type}
        raise ValueError(f"unsupported job type: {job.type}")


@celery_app.task(bind=True, base=TenantTask, name="packages.infrastructure.queue.tasks.run_job")
def run_job(self, job_id: str, tenant_id: str) -> dict[str, str]:
    with SessionFactory.begin() as session:
        job = session.scalar(select(Job).where(Job.id == UUID(job_id), Job.tenant_id == UUID(tenant_id)))
        if not job:
            return {"job_id": job_id, "status": "missing"}
        if job.status not in {"queued", "dispatching", "cancelling"}:
            return {"job_id": job_id, "status": job.status}
        if job.status == "cancelling":
            job.status = "cancelled"
            job.finished_at = now_utc()
            _log(session, job, "任务在启动前取消。")
            _event(session, job, "job.cancelled")
            return {"job_id": job_id, "status": "cancelled"}
        JobRepository(session).claim_running(job)
        _log(session, job, "任务开始执行。")
        _event(session, job, "job.started")
    try:
        with SessionFactory.begin() as session:
            job = session.scalar(select(Job).where(Job.id == UUID(job_id), Job.tenant_id == UUID(tenant_id)))
            if not job:
                return {"job_id": job_id, "status": "missing"}
            result = _run_plex_job(session, job)
            session.refresh(job)
            if job.status == "cancelling":
                JobRepository(session).mark_cancelled(job)
                _log(session, job, "任务已取消。", "warning")
                _event(session, job, "job.cancelled")
                return {"job_id": job_id, "status": "cancelled"}
            JobRepository(session).mark_succeeded(job, result)
            _log(session, job, "任务执行成功。")
            _event(session, job, "job.succeeded", result)
            return {"job_id": job_id, "status": "succeeded"}
    except Exception as exc:
        with SessionFactory.begin() as session:
            job = session.scalar(select(Job).where(Job.id == UUID(job_id), Job.tenant_id == UUID(tenant_id)))
            if job:
                JobRepository(session).mark_failed(job, str(exc))
                _log(session, job, str(exc), "error")
                _event(session, job, "job.failed", {"error": str(exc)})
        raise


@celery_app.task(name="packages.infrastructure.queue.tasks.dispatch_outbox")
def dispatch_outbox() -> dict[str, int]:
    dispatched = 0
    with SessionFactory.begin() as session:
        rows = list(session.scalars(select(JobDispatchOutbox).where(JobDispatchOutbox.status == "pending", JobDispatchOutbox.next_attempt_at <= now_utc()).order_by(JobDispatchOutbox.id).with_for_update(skip_locked=True).limit(100)).all())
        for outbox in rows:
            try:
                job = session.scalar(select(Job).where(Job.id == outbox.job_id, Job.tenant_id == outbox.tenant_id))
                run_job.apply_async(args=[str(outbox.job_id), str(outbox.tenant_id)], queue=outbox.queue)
                if job and job.status == "queued":
                    job.status = "dispatching"
                outbox.status = "dispatched"
                outbox.dispatched_at = now_utc()
                dispatched += 1
            except Exception as exc:
                outbox.attempts += 1
                outbox.last_error = str(exc)
                if outbox.attempts >= 10:
                    outbox.status = "dead_letter"
                else:
                    outbox.next_attempt_at = now_utc() + timedelta(seconds=min(300, 2 ** min(outbox.attempts, 8)))
    return {"dispatched": dispatched}
