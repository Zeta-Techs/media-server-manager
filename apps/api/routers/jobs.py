from __future__ import annotations

import json
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from apps.api.dependencies import get_db, get_tenant_context, require_roles
from apps.api.schemas.jobs import JobCreate, JobRead
from packages.domain.tenancy.context import TenantContext
from packages.infrastructure.queue.routing import queue_for_job_type
from packages.infrastructure.queue.tasks import run_job

router = APIRouter(prefix="/tenants/{tenant_id}/jobs", tags=["jobs"])


@router.get("", response_model=list[JobRead])
def list_jobs(tenant_id: UUID, db: Session = Depends(get_db), context: TenantContext = Depends(get_tenant_context)):
    if context.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="tenant access denied")
    rows = db.execute(text("SELECT id, tenant_id, type, server_id, status, payload FROM msm_jobs WHERE tenant_id=:tenant_id ORDER BY created_at DESC LIMIT 100"), {"tenant_id": str(tenant_id)}).mappings()
    return [{**dict(row), "payload": json.loads(row["payload"] or "{}")} for row in rows]


@router.post("", response_model=JobRead, status_code=status.HTTP_202_ACCEPTED)
def create_job(
    tenant_id: UUID,
    payload: JobCreate,
    db: Session = Depends(get_db),
    context: TenantContext = Depends(require_roles("tenant_admin", "operator")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    if context.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="tenant access denied")
    key = idempotency_key or payload.idempotency_key
    if key:
        existing = db.execute(text("SELECT id, tenant_id, type, server_id, status, payload FROM msm_jobs WHERE tenant_id=:tenant_id AND idempotency_key=:key"), {"tenant_id": str(tenant_id), "key": key}).mappings().first()
        if existing:
            return {**dict(existing), "payload": json.loads(existing["payload"] or "{}")} 
    job_id = uuid4()
    db.execute(text("""
      INSERT INTO msm_jobs (id, tenant_id, type, server_id, status, payload, idempotency_key)
      VALUES (:id, :tenant_id, :type, :server_id, 'queued', :payload, :key)
    """), {"id": str(job_id), "tenant_id": str(tenant_id), "type": payload.type, "server_id": payload.server_id, "payload": json.dumps(payload.payload), "key": key})
    db.commit()
    try:
        run_job.apply_async(args=[str(job_id), str(tenant_id)], queue=queue_for_job_type(payload.type))
    except Exception:
        # The durable row remains queued and can be dispatched by an outbox
        # reconciler when Redis becomes available.
        pass
    return {"id": job_id, "tenant_id": tenant_id, "type": payload.type, "server_id": payload.server_id, "status": "queued", "payload": payload.payload}
