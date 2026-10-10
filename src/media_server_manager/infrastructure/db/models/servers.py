from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ...security.secrets import EncryptedText
from ..session import Base


class Server(Base):
    __tablename__ = "servers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    address: Mapped[str] = mapped_column(Text, nullable=False)
    token: Mapped[str] = mapped_column(EncryptedText, nullable=False)
    skip_libraries: Mapped[str] = mapped_column(Text, nullable=False, default="")
    pinyin_mode: Mapped[str] = mapped_column(String(64), nullable=False, default="first_letter")
    auth_source: Mapped[str] = mapped_column(String(64), nullable=False, default="manual")
    webhook_secret: Mapped[str] = mapped_column(EncryptedText, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
