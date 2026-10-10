from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config

from .db import DB_FILE


def upgrade_head(db_file: Path | None = None) -> bool:
    """Apply Alembic revisions for a canonical data-directory deployment."""

    if os.environ.get("MSM_CONFIG_DIR", "").strip() or os.environ.get("MSM_CONFIG_PATH", "").strip():
        return False
    root = Path(__file__).resolve().parent.parent
    ini = root / "alembic.ini"
    if not ini.exists():
        return False
    database = Path(db_file or DB_FILE).resolve()
    config = Config(str(ini))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")
    command.upgrade(config, "head")
    return True
