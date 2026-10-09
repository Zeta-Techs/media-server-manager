from fastapi import APIRouter

router = APIRouter(prefix="/tenants/{tenant_id}/automation", tags=["automation"])
