"""Track legacy imports and make migration reports auditable."""

import sqlalchemy as sa
from alembic import op

revision = "0002_migration_runs"
down_revision = "0001_legacy_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "migration_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("migration_id", sa.String(length=100), nullable=False, unique=True),
        sa.Column("source_path", sa.Text(), nullable=False),
        sa.Column("destination_path", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_table", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("copied_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("started_at", sa.Text(), nullable=False),
        sa.Column("finished_at", sa.Text(), nullable=True),
    )
    op.create_index("idx_migration_runs_status", "migration_runs", ["status", "started_at"])


def downgrade() -> None:
    op.drop_index("idx_migration_runs_status", table_name="migration_runs")
    op.drop_table("migration_runs")
