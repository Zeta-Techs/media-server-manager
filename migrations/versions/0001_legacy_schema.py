"""Bootstrap the existing schema into Alembic ownership.

The application already has a production schema initializer.  This first
revision deliberately delegates the bootstrap to it so existing installations
can adopt Alembic without losing any of the 47 current tables. Future changes
must use regular Alembic operations.
"""

from __future__ import annotations

from pathlib import Path

from alembic import op
from sqlalchemy import inspect

revision = "0001_legacy_schema"
down_revision = None
branch_labels = None
depends_on = None


def _database_path() -> Path:
    bind = op.get_bind()
    database = bind.engine.url.database
    if not database or database == ":memory:":
        raise RuntimeError("初始迁移需要一个文件型 SQLite 数据库")
    return Path(database).expanduser().resolve()


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        raise RuntimeError("当前版本只支持 SQLite 初始迁移")
    if inspect(bind).has_table("schema_meta"):
        return
    from media_server_manager_web.db import init_db

    init_db(_database_path())


def downgrade() -> None:
    raise RuntimeError("初始兼容迁移不可自动降级；请使用数据库备份恢复")
