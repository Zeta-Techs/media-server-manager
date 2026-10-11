"""Create legacy tables that were present in production but absent from init_db."""

from alembic import op

revision = "0003_complete_legacy_tables"
down_revision = "0002_migration_runs"
branch_labels = None
depends_on = None

_TABLES = (
    "anime_manual_overrides",
    "anime_match_results",
    "anime_season_items",
    "anime_seasons",
    "anime_show_schedule",
    "anime_sync_events",
)


def upgrade() -> None:
    from media_server_manager.infrastructure.db.session import Base

    bind = op.get_bind()
    for name in _TABLES:
        Base.metadata.tables[name].create(bind=bind, checkfirst=True)


def downgrade() -> None:
    from media_server_manager.infrastructure.db.session import Base

    bind = op.get_bind()
    for name in reversed(_TABLES):
        Base.metadata.tables[name].drop(bind=bind, checkfirst=True)
