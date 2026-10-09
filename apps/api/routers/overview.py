from fastapi import APIRouter

router = APIRouter(prefix="/tenants/{tenant_id}/overview", tags=["overview"])
