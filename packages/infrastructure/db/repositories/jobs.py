from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from packages.infrastructure.db.models import Job, JobDispatchOutbox, JobEvent, JobLog


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class JobRepository:
    def __init__(self, session: Session):
        self.session = session

    def get_by_id(self, tenant_id: UUID, job_id: UUID, *, for_update: bool = False) -> Job | None:
        statement = select(Job).where(Job.tenant_id == tenant_id, Job.id == job_id)
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def list(self, tenant_id: UUID, limit: int = 50, cursor: datetime | None = None) -> list[Job]:
        statement = select(Job).where(Job.tenant_id == tenant_id).order_by(Job.created_at.desc(), Job.id.desc()).limit(max(1, min(limit, 200)))
        if cursor:
            statement = statement.where(Job.created_at < cursor)
        return list(self.session.scalars(statement).all())

    def create_queued(self, tenant_id: UUID, job_type: str, server_id: int | None, payload: dict, queue: str, request_id: str, idempotency_key: str | None) -> Job:
        if idempotency_key:
            existing = self.session.scalar(select(Job).where(Job.tenant_id == tenant_id, Job.idempotency_key == idempotency_key))
            if existing:
                return existing
        job = Job(tenant_id=tenant_id, type=job_type, server_id=server_id, payload=json.dumps(payload, ensure_ascii=False), request_id=request_id, idempotency_key=idempotency_key)
        self.session.add(job)
        try:
            with self.session.begin_nested():
                self.session.flush()
        except IntegrityError:
            if idempotency_key:
                existing = self.session.scalar(select(Job).where(Job.tenant_id == tenant_id, Job.idempotency_key == idempotency_key))
                if existing:
                    return existing
            raise
        self.append_log(tenant_id, job.id, "任务已加入队列。")
        self.session.add(JobDispatchOutbox(tenant_id=tenant_id, job_id=job.id, queue=queue))
        self.append_event(tenant_id, job.id, "job.created", request_id, {"type": job_type})
        return job

    def append_log(self, tenant_id: UUID, job_id: UUID, message: str, level: str = "info") -> None:
        self.session.add(JobLog(tenant_id=tenant_id, job_id=job_id, message=message, level=level))

    def append_event(self, tenant_id: UUID, job_id: UUID, event_type: str, request_id: str, payload: dict) -> None:
        self.session.add(JobEvent(tenant_id=tenant_id, job_id=job_id, event_type=event_type, request_id=request_id, payload=json.dumps(payload, ensure_ascii=False)))

    def request_cancel(self, job: Job) -> str:
        if job.status == "queued":
            job.status = "cancelled"
            job.finished_at = now_utc()
            return job.status
        if job.status == "running":
            job.status = "cancelling"
            job.cancel_requested_at = now_utc()
        return job.status

    def claim_running(self, job: Job) -> bool:
        if job.status not in {"queued", "dispatching"}:
            return False
        job.status = "running"
        job.started_at = now_utc()
        return True

    def mark_succeeded(self, job: Job, result: dict) -> None:
        job.status = "succeeded"
        job.result = json.dumps(result, ensure_ascii=False)
        job.finished_at = now_utc()

    def mark_failed(self, job: Job, error: str) -> None:
        job.status = "failed"
        job.error = error
        job.finished_at = now_utc()

    def mark_cancelled(self, job: Job) -> None:
        job.status = "cancelled"
        job.finished_at = now_utc()

    def cancel_queued_for_server(self, tenant_id: UUID, server_id: int) -> int:
        jobs = list(self.session.scalars(select(Job).where(Job.tenant_id == tenant_id, Job.server_id == server_id, Job.status == "queued").with_for_update()).all())
        for job in jobs:
            job.status = "cancelled"
            job.finished_at = now_utc()
            self.append_log(tenant_id, job.id, "服务器删除，未开始任务已取消。", "warning")
            self.append_event(tenant_id, job.id, "job.cancelled", job.request_id, {"reason": "server_deleted"})
        return len(jobs)

    def retry(self, tenant_id: UUID, source_job: Job, queue: str, request_id: str) -> Job:
        if source_job.status not in {"failed", "cancelled", "interrupted"}:
            raise ValueError("only failed, cancelled or interrupted jobs can be retried")
        job = self.create_queued(tenant_id, source_job.type, source_job.server_id, json.loads(source_job.payload or "{}"), queue, request_id, None)
        job.retry_of = source_job.id
        return job

    def list_logs(self, tenant_id: UUID, job_id: UUID, limit: int = 300) -> list[JobLog]:
        return list(self.session.scalars(select(JobLog).where(JobLog.tenant_id == tenant_id, JobLog.job_id == job_id).order_by(JobLog.id).limit(min(max(limit, 1), 2000))).all())

    def list_events(self, tenant_id: UUID, job_id: UUID, limit: int = 300) -> list[JobEvent]:
        return list(self.session.scalars(select(JobEvent).where(JobEvent.tenant_id == tenant_id, JobEvent.job_id == job_id).order_by(JobEvent.id).limit(min(max(limit, 1), 2000))).all())
