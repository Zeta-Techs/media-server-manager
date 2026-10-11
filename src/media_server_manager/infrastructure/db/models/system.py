from __future__ import annotations

from sqlalchemy import Column, Integer, String, Table, Text, UniqueConstraint

from ..session import Base


class SchemaMeta(Base):
    __table__ = Table(
        "schema_meta",
        Base.metadata,
        Column("id", Integer(), primary_key=True),
        Column("version", Integer(), nullable=False),
        Column("created_at", Text(), nullable=False),
    )


class MigrationRun(Base):
    __table__ = Table(
        "migration_runs",
        Base.metadata,
        Column("id", Integer(), primary_key=True),
        Column("migration_id", String(100), nullable=False),
        Column("source_path", Text(), nullable=False),
        Column("destination_path", Text(), nullable=False),
        Column("status", String(32), nullable=False),
        Column("current_table", String(255), nullable=False),
        Column("copied_rows", Integer(), nullable=False),
        Column("error", Text(), nullable=False),
        Column("started_at", Text(), nullable=False),
        Column("finished_at", Text()),
        UniqueConstraint("migration_id", name="uq_migration_runs_migration_id"),
    )
