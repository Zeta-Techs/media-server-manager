from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path


def backup_sqlite(source: Path, destination_dir: Path) -> Path:
    """Create a consistent SQLite backup without copying a live WAL manually."""

    destination_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = destination_dir / f"{source.stem}-{stamp}.sqlite3"
    source_db = sqlite3.connect(source)
    target_db = sqlite3.connect(target)
    try:
        source_db.backup(target_db)
        target_db.execute("PRAGMA integrity_check")
        target_db.commit()
    finally:
        target_db.close()
        source_db.close()
    target.chmod(0o600)
    return target
