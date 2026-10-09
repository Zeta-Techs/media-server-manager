from fastapi import APIRouter

router = APIRouter(prefix="/tenants/{tenant_id}/media-libraries", tags=["media-libraries"])
