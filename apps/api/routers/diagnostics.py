from fastapi import APIRouter

router = APIRouter(prefix="/tenants/{tenant_id}/diagnostics", tags=["diagnostics"])
