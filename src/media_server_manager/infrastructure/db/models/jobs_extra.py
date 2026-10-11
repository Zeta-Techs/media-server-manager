from __future__ import annotations

from sqlalchemy import Column, ForeignKey, Integer, Table, Text

from ..session import Base


class ChangeSets(Base):
    __table__ = Table(
        "change_sets", Base.metadata,
        Column("id", Integer(), primary_key=True),
        Column("job_id", Integer(), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        Column("server_id", Integer(), ForeignKey("servers.id", ondelete="CASCADE"), nullable=False),
        Column("mode", Text(), nullable=False),
        Column("scope", Text(), nullable=False),
        Column("source_change_set_id", Integer(), ForeignKey("change_sets.id", ondelete="SET NULL")),
        Column("created_at", Text(), nullable=False),
    )

class Changes(Base):
    __table__ = Table(
        "changes", Base.metadata,
        Column("id", Integer(), primary_key=True),
        Column("change_set_id", Integer(), ForeignKey("change_sets.id", ondelete="CASCADE"), nullable=False),
        Column("job_id", Integer(), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        Column("server_id", Integer(), ForeignKey("servers.id", ondelete="CASCADE"), nullable=False),
        Column("library_id", Integer(), nullable=False),
        Column("media_type", Text(), nullable=False),
        Column("rating_key", Text(), nullable=False),
        Column("title", Text(), nullable=False),
        Column("field", Text(), nullable=False),
        Column("old_value", Text(), nullable=False),
        Column("new_value", Text(), nullable=False),
        Column("old_locked", Integer()),
        Column("apply_status", Text(), nullable=False),
        Column("apply_error", Text(), nullable=False),
        Column("applied", Integer(), nullable=False),
        Column("rollback_status", Text(), nullable=False),
        Column("rollback_error", Text(), nullable=False),
        Column("created_at", Text(), nullable=False),
        Column("applied_at", Text()),
        Column("rolled_back_at", Text()),
    )
