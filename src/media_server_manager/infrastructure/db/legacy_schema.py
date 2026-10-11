"""Compatibility hook for database initialization.

Schema creation is owned exclusively by Alembic.
"""

from __future__ import annotations

from pathlib import Path


def init_db(db_file: Path | None = None) -> None:
    # Validate legacy installations before Alembic opens the file.  This
    # preserves the old reset workflow and keeps foreign WAL/SHM sidecars
    # untouched when the database requires an explicit backup reset.
    from .runtime import DB_FILE, _existing_schema_version

    _existing_schema_version(Path(db_file or DB_FILE))
    from .migrations import upgrade_head

    if not upgrade_head(db_file):
        raise RuntimeError("找不到 Alembic 配置，无法初始化数据库")
    database = Path(db_file or DB_FILE)
    if database.exists():
        database.chmod(0o600)
