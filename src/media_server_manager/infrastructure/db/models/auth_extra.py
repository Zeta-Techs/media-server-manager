from __future__ import annotations

from sqlalchemy import Column, ForeignKey, Integer, Table, Text

from ...security.secrets import EncryptedText
from ..session import Base


class OauthFlows(Base):
    __table__ = Table(
        "oauth_flows", Base.metadata,
        Column("id", Text(), primary_key=True),
        Column("user_id", Integer(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        Column("pin_id", Integer(), nullable=False),
        Column("token", EncryptedText(), nullable=False),
        Column("resources", Text(), nullable=False),
        Column("expires_at", Text(), nullable=False),
        Column("created_at", Text(), nullable=False),
    )
