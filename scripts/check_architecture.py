"""Reject new direct SQLite access outside the compatibility boundary."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = (
    "import sqlite3",
    "sqlite3.connect(",
    "db.execute(",
    "db.executemany(",
    "session.execute(",
    "session.exec_driver_sql(",
    "connection().exec_driver_sql(",
    "connect_session(",
    "execute_legacy(",
    "CREATE TABLE",
    "ALTER TABLE",
    "from media_server_manager_web.db import connect",
    "from media_server_manager.api.routes.compat.db import connect",
    "routes.compat",
    "infrastructure.db.compat",
    "infrastructure.db.legacy_sql",
    "os.environ",
    "_run_job(",
)
ALLOWED = {
    ROOT / "media_server_manager_web" / "db.py",
    ROOT / "src" / "media_server_manager" / "infrastructure" / "db" / "sql.py",
    ROOT / "src" / "media_server_manager" / "infrastructure" / "db" / "legacy_sql.py",
}
SCAN_ROOTS = (
    ROOT / "src" / "media_server_manager" / "api",
    ROOT / "src" / "media_server_manager" / "application",
    ROOT / "src" / "media_server_manager" / "worker",
)


def main() -> int:
    violations: list[str] = []
    for scan_root in SCAN_ROOTS:
        if not scan_root.exists():
            continue
        for path in scan_root.rglob("*.py"):
            if path in ALLOWED:
                continue
            # Every API, application and worker module must use a Repository
            # or UnitOfWork.  The compatibility SQL adapter is intentionally
            # outside these roots and is audited separately.
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if any(token in line for token in FORBIDDEN):
                    violations.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    if violations:
        print("禁止在新分层中直接访问 sqlite3：", file=sys.stderr)
        print("\n".join(violations), file=sys.stderr)
        return 1
    print("架构静态检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
