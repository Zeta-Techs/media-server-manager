from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class JobCreate(BaseModel):
    type: str = Field(min_length=1, max_length=100)
    server_id: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=255)


class JobRead(BaseModel):
    id: UUID
    tenant_id: UUID
    type: str
    server_id: int | None
    status: str
    payload: dict[str, Any]
