"""Canonical Alembic entry points used by web and worker processes."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from .runtime import DB_FILE


def upgrade_head(db_file: Path | None = None) -> bool:
    database = Path(db_file or DB_FILE).expanduser().resolve()
    root = Path(__file__).resolve().parents[4]
    ini = root / "alembic.ini"
    if not ini.exists():
        return False
    config = Config(str(ini))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")
    command.upgrade(config, "head")
    return True


__all__ = ["upgrade_head"]

