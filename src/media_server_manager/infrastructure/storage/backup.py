from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from sqlalchemy import text

from ..db.session import Database


def backup_sqlite(source: Path, destination_dir: Path) -> Path:
    """Create a consistent SQLite backup without copying a live WAL manually."""

    destination_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = destination_dir / f"{source.stem}-{stamp}.sqlite3"
    if target.exists():
        target = destination_dir / f"{source.stem}-{stamp}-{datetime.now().microsecond:06d}.sqlite3"
    # VACUUM INTO is SQLite's transactional online-backup primitive.  It
    # includes the current WAL state and executes through the canonical
    # SQLAlchemy engine, so backup commands do not open an unmanaged DB-API
    # connection.
    source_db = Database.for_path(source)
    try:
        with source_db.session() as session:
            escaped_target = str(target).replace("'", "''")
            session.connection().exec_driver_sql(f"VACUUM INTO '{escaped_target}'")
    finally:
        source_db.engine.dispose()
    target_db = Database.for_path(target)
    try:
        with target_db.session() as session:
            integrity = str(session.execute(text("PRAGMA integrity_check")).scalar_one())
            foreign_keys = session.execute(text("PRAGMA foreign_key_check")).all()
    finally:
        target_db.engine.dispose()
    if integrity.lower() != "ok" or foreign_keys:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"备份完整性检查失败：integrity={integrity}, foreign_keys={len(foreign_keys)}")
    target.chmod(0o600)
    return target


def restore_sqlite(source: Path, destination: Path, *, backup_current: bool = True) -> Path | None:
    """Restore a verified SQLite backup and return the safety backup path.

    The destination is replaced atomically after the source passes an
    integrity check.  Any existing database is copied aside first so an
    operator can immediately undo an accidental restore.
    """

    source = Path(source).expanduser().resolve()
    destination = Path(destination).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"找不到备份数据库：{source}")
    source_db = Database.for_path(source)
    try:
        with source_db.session() as session:
            integrity = str(session.execute(text("PRAGMA integrity_check")).scalar_one())
            foreign_keys = session.execute(text("PRAGMA foreign_key_check")).all()
            if integrity.lower() != "ok" or foreign_keys:
                raise RuntimeError(
                    f"备份数据库完整性检查失败：integrity={integrity}, foreign_keys={len(foreign_keys)}"
                )
    finally:
        source_db.engine.dispose()
    destination.parent.mkdir(parents=True, exist_ok=True)
    current_backup: Path | None = None
    if destination.exists() and backup_current:
        current_backup = backup_sqlite(destination, destination.parent / "backups")
    temp = destination.with_name(destination.name + ".restore-tmp")
    shutil.copy2(source, temp)
    temp.chmod(0o600)
    temp.replace(destination)
    destination.chmod(0o600)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{destination}{suffix}")
        if sidecar.exists():
            sidecar.unlink()
    return current_backup
