from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field


class ServerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    address: str = Field(min_length=1, max_length=1000)
    token: str = Field(min_length=1, max_length=1000)
    enabled: bool = True
    pinyin_mode: str = "first_letter"
    skip_libraries: str = ""


class ServerUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    address: str | None = Field(default=None, max_length=1000)
    token: str | None = Field(default=None, max_length=1000)
    enabled: bool | None = None
    pinyin_mode: str | None = None
    skip_libraries: str | None = None


class ServerRead(BaseModel):
    id: int
    tenant_id: UUID
    name: str
    address: str
    enabled: bool
    token_configured: bool
    pinyin_mode: str
    auth_source: str
