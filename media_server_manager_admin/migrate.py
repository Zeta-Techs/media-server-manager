from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from media_server_manager_web.db import init_db

TABLES = (
    "users",
    "servers",
    "jobs",
    "media_libraries",
    "media_library_items",
    "tmdb_items",
    "webhook_events",
)


def _count_rows(connection: sqlite3.Connection, table: str) -> int:
    try:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except sqlite3.OperationalError:
        return 0


def migrate_legacy(source: Path, destination: Path, copy_cache: bool = True) -> dict[str, Any]:
    """Copy a legacy SQLite installation into the canonical data directory.

    The old schema is retained as-is for compatibility; Alembic owns future
    changes after the import. The source is never modified.
    """

    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"找不到旧数据库：{source}")
    if destination.exists():
        raise FileExistsError(f"目标数据库已存在：{destination}")

    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as old:
        old.row_factory = sqlite3.Row
        integrity = str(old.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity.lower() != "ok":
            raise RuntimeError(f"旧数据库完整性检查失败：{integrity}")
        counts = {table: _count_rows(old, table) for table in TABLES}
        destination.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(destination) as new:
            old.backup(new)

    init_db(destination)

    copied_cache = False
    if copy_cache:
        legacy_cache = source.parent / "media_cache"
        new_cache = destination.parent / "cache" / "tmdb"
        if legacy_cache.exists() and not new_cache.exists():
            new_cache.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(legacy_cache, new_cache)
            copied_cache = True

    try:
        destination.chmod(0o600)
    except OSError:
        pass
    return {"source": str(source), "destination": str(destination), "counts": counts, "cache_copied": copied_cache}


def write_report(report: dict[str, Any], path: Path | None = None) -> None:
    if path is None:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
