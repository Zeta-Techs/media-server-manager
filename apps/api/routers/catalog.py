from fastapi import APIRouter

router = APIRouter(prefix="/tenants/{tenant_id}/catalog", tags=["catalog"])
