from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field


class TenantCreate(BaseModel):
    slug: str = Field(min_length=2, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=200)
    plan: str = "standard"


class TenantRead(BaseModel):
    id: UUID
    slug: str
    name: str
    status: str
    plan: str

    model_config = {"from_attributes": True}
