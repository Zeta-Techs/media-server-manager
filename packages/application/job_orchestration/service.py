from __future__ import annotations

from uuid import UUID

from packages.domain.tenancy.context import TenantContext
from packages.infrastructure.db.models import Job
from packages.infrastructure.db.repositories.jobs import JobRepository
from packages.infrastructure.db.repositories.servers import ServerRepository
from packages.infrastructure.queue.routing import queue_for_job_type


class JobOrchestrationService:
    def __init__(self, session):
        self.session = session
        self.repository = JobRepository(session)

    def create(self, context: TenantContext, job_type: str, server_id: int | None, payload: dict, request_id: str, idempotency_key: str | None) -> Job:
        if server_id is not None and not ServerRepository(self.session).get_by_id(context.tenant_id, server_id):
            raise LookupError("server not found")
        return self.repository.create_queued(context.tenant_id, job_type, server_id, payload, queue_for_job_type(job_type), request_id, idempotency_key)

    def get(self, context: TenantContext, job_id: UUID) -> Job:
        job = self.repository.get_by_id(context.tenant_id, job_id)
        if not job:
            raise LookupError("job not found")
        return job

    def cancel(self, context: TenantContext, job_id: UUID) -> Job:
        job = self.repository.get_by_id(context.tenant_id, job_id, for_update=True)
        if not job:
            raise LookupError("job not found")
        previous = job.status
        new_status = self.repository.request_cancel(job)
        if new_status != previous:
            event_type = "job.cancelled" if new_status == "cancelled" else "job.cancelling"
            self.repository.append_log(context.tenant_id, job.id, "任务已取消。" if new_status == "cancelled" else "已请求取消任务。", "warning" if new_status != "cancelled" else "info")
            self.repository.append_event(context.tenant_id, job.id, event_type, job.request_id, {})
        return job

    def retry(self, context: TenantContext, job_id: UUID, request_id: str) -> Job:
        job = self.repository.get_by_id(context.tenant_id, job_id)
        if not job:
            raise LookupError("job not found")
        return self.repository.retry(context.tenant_id, job, queue_for_job_type(job.type), request_id)
