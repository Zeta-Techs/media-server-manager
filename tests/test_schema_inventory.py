from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

from media_server_manager.infrastructure.db import models  # noqa: F401
from media_server_manager.infrastructure.db.session import Base


def test_orm_metadata_covers_legacy_business_tables() -> None:
    source = Path("config/media_server_manager.db")
    if not source.exists():
        return
    with sqlite3.connect(source) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    assert tables - {"alembic_version"} <= set(Base.metadata.tables)


def test_admin_entrypoint_exposes_database_commands() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "media_server_manager_admin", "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "migrate-legacy" in result.stdout
    assert "rotate-secrets" in result.stdout
