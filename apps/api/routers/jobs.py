from __future__ import annotations

import json
import time
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from apps.api.dependencies import get_db, get_tenant_context, require_roles
from apps.api.schemas.jobs import JobCreate, JobEventRead, JobLogRead, JobRead
from packages.application.job_orchestration import JobOrchestrationService
from packages.domain.tenancy.context import TenantContext
from packages.infrastructure.db.repositories.jobs import JobRepository

router = APIRouter(prefix="/tenants/{tenant_id}/jobs", tags=["jobs"])


def require_tenant(tenant_id: UUID, context: TenantContext) -> None:
    if context.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="tenant access denied")


def serialize(job) -> dict:
    return {"id": job.id, "tenant_id": job.tenant_id, "type": job.type, "server_id": job.server_id, "status": job.status, "payload": json.loads(job.payload or "{}"), "error": job.error, "request_id": job.request_id, "created_at": job.created_at.isoformat() if job.created_at else None, "started_at": job.started_at.isoformat() if job.started_at else None, "finished_at": job.finished_at.isoformat() if job.finished_at else None}


@router.get("", response_model=list[JobRead])
def list_jobs(tenant_id: UUID, limit: int = 50, cursor: datetime | None = None, db: Session = Depends(get_db), context: TenantContext = Depends(get_tenant_context)):
    require_tenant(tenant_id, context)
    return [serialize(job) for job in JobRepository(db).list(context.tenant_id, limit, cursor)]


@router.post("", response_model=JobRead, status_code=status.HTTP_202_ACCEPTED)
def create_job(tenant_id: UUID, payload: JobCreate, request: Request, db: Session = Depends(get_db), context: TenantContext = Depends(require_roles("operator", "tenant_admin")), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    require_tenant(tenant_id, context)
    try:
        job = JobOrchestrationService(db).create(context, payload.type, payload.server_id, payload.payload, getattr(request.state, "request_id", ""), idempotency_key or payload.idempotency_key)
        db.commit()
        return serialize(job)
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception:
        db.rollback()
        raise


@router.get("/{job_id}", response_model=JobRead)
def get_job(tenant_id: UUID, job_id: UUID, db: Session = Depends(get_db), context: TenantContext = Depends(get_tenant_context)):
    require_tenant(tenant_id, context)
    try:
        return serialize(JobOrchestrationService(db).get(context, job_id))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{job_id}/cancel", response_model=JobRead)
def cancel_job(tenant_id: UUID, job_id: UUID, db: Session = Depends(get_db), context: TenantContext = Depends(require_roles("operator", "tenant_admin"))):
    require_tenant(tenant_id, context)
    try:
        job = JobOrchestrationService(db).cancel(context, job_id)
        db.commit()
        return serialize(job)
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{job_id}/retry", response_model=JobRead, status_code=status.HTTP_202_ACCEPTED)
def retry_job(tenant_id: UUID, job_id: UUID, request: Request, db: Session = Depends(get_db), context: TenantContext = Depends(require_roles("operator", "tenant_admin"))):
    require_tenant(tenant_id, context)
    try:
        job = JobOrchestrationService(db).retry(context, job_id, getattr(request.state, "request_id", ""))
        db.commit()
        return serialize(job)
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{job_id}/logs", response_model=list[JobLogRead])
def list_logs(tenant_id: UUID, job_id: UUID, limit: int = 300, db: Session = Depends(get_db), context: TenantContext = Depends(get_tenant_context)):
    require_tenant(tenant_id, context)
    if not JobRepository(db).get_by_id(context.tenant_id, job_id):
        raise HTTPException(status_code=404, detail="job not found")
    return [{"id": item.id, "message": item.message, "level": item.level, "created_at": item.created_at.isoformat()} for item in JobRepository(db).list_logs(context.tenant_id, job_id, limit)]


@router.get("/{job_id}/events", response_model=list[JobEventRead])
def list_events(tenant_id: UUID, job_id: UUID, limit: int = 300, db: Session = Depends(get_db), context: TenantContext = Depends(get_tenant_context)):
    require_tenant(tenant_id, context)
    if not JobRepository(db).get_by_id(context.tenant_id, job_id):
        raise HTTPException(status_code=404, detail="job not found")
    return [{"id": item.id, "event_id": item.event_id, "event_type": item.event_type, "request_id": item.request_id, "payload": json.loads(item.payload or "{}"), "created_at": item.created_at.isoformat()} for item in JobRepository(db).list_events(context.tenant_id, job_id, limit)]


@router.get("/{job_id}/events/stream")
def stream_events(tenant_id: UUID, job_id: UUID, db: Session = Depends(get_db), context: TenantContext = Depends(get_tenant_context)):
    require_tenant(tenant_id, context)
    if not JobRepository(db).get_by_id(context.tenant_id, job_id):
        raise HTTPException(status_code=404, detail="job not found")

    def generate():
        last_id = 0
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            with SessionFactory() as stream_db:
                events = list(JobRepository(stream_db).list_events(context.tenant_id, job_id, 200))
            for event in events:
                if event.id <= last_id:
                    continue
                last_id = event.id
                payload = {"id": event.id, "event_id": str(event.event_id), "event_type": event.event_type, "request_id": event.request_id, "payload": json.loads(event.payload or "{}"), "created_at": event.created_at.isoformat()}
                yield f"id: {event.id}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            yield ": keep-alive\n\n"
            time.sleep(1)

    from packages.infrastructure.db.session import SessionFactory

    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
