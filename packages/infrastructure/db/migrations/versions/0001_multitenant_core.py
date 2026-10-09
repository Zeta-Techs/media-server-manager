"""Create the multitenant core tables."""
import sqlalchemy as sa
from alembic import op

revision = "0001_multitenant_core"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        uuid_default = sa.text("gen_random_uuid()")
    else:
        uuid_default = None
    op.create_table("tenants", sa.Column("id", sa.Uuid(), primary_key=True, server_default=uuid_default), sa.Column("slug", sa.String(100), nullable=False, unique=True), sa.Column("name", sa.String(200), nullable=False), sa.Column("status", sa.String(32), nullable=False, server_default="active"), sa.Column("plan", sa.String(64), nullable=False, server_default="standard"), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_table("users_v2", sa.Column("id", sa.Uuid(), primary_key=True, server_default=uuid_default), sa.Column("oidc_subject", sa.String(255), nullable=False, unique=True), sa.Column("email", sa.String(320), nullable=False, server_default=""), sa.Column("display_name", sa.String(200), nullable=False, server_default=""), sa.Column("status", sa.String(32), nullable=False, server_default="active"), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_table("tenant_memberships", sa.Column("id", sa.Uuid(), primary_key=True, server_default=uuid_default), sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False), sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users_v2.id", ondelete="CASCADE"), nullable=False), sa.Column("role", sa.String(64), nullable=False, server_default="viewer"), sa.Column("status", sa.String(32), nullable=False, server_default="active"), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()), sa.UniqueConstraint("tenant_id", "user_id", name="uq_tenant_membership"))
    op.create_index("ix_membership_tenant", "tenant_memberships", ["tenant_id"])
    op.create_index("ix_membership_user", "tenant_memberships", ["user_id"])
    op.create_table("roles", sa.Column("id", sa.Uuid(), primary_key=True, server_default=uuid_default), sa.Column("name", sa.String(64), nullable=False, unique=True), sa.Column("scope", sa.String(32), nullable=False, server_default="tenant"))
    op.create_table("permissions", sa.Column("id", sa.Uuid(), primary_key=True, server_default=uuid_default), sa.Column("name", sa.String(128), nullable=False, unique=True))
    op.create_table("role_permissions", sa.Column("role_id", sa.Uuid(), sa.ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True), sa.Column("permission_id", sa.Uuid(), sa.ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True))
    op.create_table("refresh_tokens", sa.Column("id", sa.Uuid(), primary_key=True, server_default=uuid_default), sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users_v2.id", ondelete="CASCADE"), nullable=False), sa.Column("token_hash", sa.String(128), nullable=False, unique=True), sa.Column("device_name", sa.String(200), nullable=False, server_default=""), sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False), sa.Column("revoked_at", sa.DateTime(timezone=True)), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_table("msm_servers", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False), sa.Column("name", sa.String(200), nullable=False), sa.Column("address", sa.Text(), nullable=False), sa.Column("token", sa.Text(), nullable=False), sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()), sa.UniqueConstraint("tenant_id", "name", name="uq_server_tenant_name"))
    op.create_index("ix_servers_tenant", "msm_servers", ["tenant_id"])
    op.create_table("msm_jobs", sa.Column("id", sa.Uuid(), primary_key=True, server_default=uuid_default), sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False), sa.Column("type", sa.String(100), nullable=False), sa.Column("server_id", sa.Integer(), sa.ForeignKey("msm_servers.id", ondelete="SET NULL")), sa.Column("status", sa.String(32), nullable=False, server_default="queued"), sa.Column("payload", sa.Text(), nullable=False, server_default="{}"), sa.Column("error", sa.Text(), nullable=False, server_default=""), sa.Column("idempotency_key", sa.String(255)), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()), sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_job_idempotency"))
    op.create_index("ix_jobs_tenant_status", "msm_jobs", ["tenant_id", "status", "created_at"])
    if bind.dialect.name == "postgresql":
        for table in ("msm_servers", "msm_jobs"):
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"CREATE POLICY {table}_tenant_isolation ON {table} USING (tenant_id::text = current_setting('app.tenant_id', true))")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS msm_jobs_tenant_isolation ON msm_jobs")
        op.execute("DROP POLICY IF EXISTS msm_servers_tenant_isolation ON msm_servers")
    op.drop_table("msm_jobs")
    op.drop_table("msm_servers")
    op.drop_table("tenant_memberships")
    op.drop_table("refresh_tokens")
    op.drop_table("role_permissions")
    op.drop_table("permissions")
    op.drop_table("roles")
    op.drop_table("users_v2")
    op.drop_table("tenants")
