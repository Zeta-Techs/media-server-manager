"""Add tenant-scoped server and job lifecycle tables."""
import sqlalchemy as sa
from alembic import op

revision = "0002_server_jobs"
down_revision = "0001_multitenant_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # The initial migration created the small bootstrap tables. Add operational
    # fields and durable event/outbox tables used by the new API.
    inspector = sa.inspect(bind)
    server_columns = {column["name"] for column in inspector.get_columns("msm_servers")}
    for name, definition in {
        "webhook_secret": sa.Text(),
        "skip_libraries": sa.Text(),
        "pinyin_mode": sa.String(32),
        "auth_source": sa.String(32),
        "created_at": sa.DateTime(timezone=True),
        "updated_at": sa.DateTime(timezone=True),
    }.items():
        if name not in server_columns:
            op.add_column("msm_servers", sa.Column(name, definition, nullable=True))
    job_columns = {column["name"] for column in inspector.get_columns("msm_jobs")}
    for name, definition in {
        "result": sa.Text(),
        "request_id": sa.String(128),
        "retry_of": sa.Uuid(),
        "started_at": sa.DateTime(timezone=True),
        "finished_at": sa.DateTime(timezone=True),
        "cancel_requested_at": sa.DateTime(timezone=True),
    }.items():
        if name not in job_columns:
            op.add_column("msm_jobs", sa.Column(name, definition, nullable=True))
    if bind.dialect.name == "postgresql":
        foreign_keys = {fk.get("name") for fk in inspector.get_foreign_keys("msm_jobs")}
        if "fk_msm_jobs_retry_of" not in foreign_keys:
            op.create_foreign_key("fk_msm_jobs_retry_of", "msm_jobs", "msm_jobs", ["retry_of"], ["id"], ondelete="SET NULL")
    if "msm_job_logs" not in inspector.get_table_names():
        op.create_table("msm_job_logs", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("tenant_id", sa.Uuid(), nullable=False), sa.Column("job_id", sa.Uuid(), nullable=False), sa.Column("message", sa.Text(), nullable=False), sa.Column("level", sa.String(16), nullable=False, server_default="info"), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    if "msm_job_events" not in inspector.get_table_names():
        op.create_table("msm_job_events", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("event_id", sa.Uuid(), nullable=False, unique=True), sa.Column("tenant_id", sa.Uuid(), nullable=False), sa.Column("job_id", sa.Uuid(), nullable=False), sa.Column("event_type", sa.String(64), nullable=False), sa.Column("request_id", sa.String(128), nullable=False, server_default=""), sa.Column("payload", sa.Text(), nullable=False, server_default="{}"), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    if "msm_job_dispatch_outbox" not in inspector.get_table_names():
        op.create_table("msm_job_dispatch_outbox", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("tenant_id", sa.Uuid(), nullable=False), sa.Column("job_id", sa.Uuid(), nullable=False, unique=True), sa.Column("queue", sa.String(64), nullable=False), sa.Column("status", sa.String(32), nullable=False, server_default="pending"), sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"), sa.Column("last_error", sa.Text(), nullable=False, server_default=""), sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()), sa.Column("dispatched_at", sa.DateTime(timezone=True)))
    if bind.dialect.name == "postgresql":
        for table in ("msm_job_logs", "msm_job_events", "msm_job_dispatch_outbox"):
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"CREATE POLICY {table}_tenant_isolation ON {table} USING (tenant_id::text = current_setting('app.tenant_id', true))")


def downgrade() -> None:
    for table in ("msm_job_dispatch_outbox", "msm_job_events", "msm_job_logs"):
        op.drop_table(table)
