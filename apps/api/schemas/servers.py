from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field


class ServerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    address: str = Field(min_length=1, max_length=1000)
    token: str = Field(min_length=1, max_length=1000)
    enabled: bool = True


class ServerRead(BaseModel):
    id: int
    tenant_id: UUID
    name: str
    address: str
    enabled: bool
    token_configured: bool
