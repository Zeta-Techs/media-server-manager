from __future__ import annotations

import argparse
import shutil
from datetime import datetime

from media_server_manager_web.db import DB_FILE, reset_database, init_db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Media Server Manager 管理命令")
    subparsers = parser.add_subparsers(dest="command", required=True)
    reset = subparsers.add_parser("reset-db", help="重建不兼容的数据库")
    reset.add_argument("--backup", action="store_true", help="重建前备份旧数据库")
    migrate = subparsers.add_parser("migrate-legacy", help="迁移旧 CLP 数据库")
    args = parser.parse_args(argv)
    if args.command == "reset-db":
        backup = reset_database(DB_FILE, backup=args.backup)
        if backup:
            print(f"旧数据库已备份到：{backup}")
        print(f"数据库已初始化：{DB_FILE}")
        return 0
    if args.command == "migrate-legacy":
        legacy = DB_FILE.with_name("clp.db")
        if not legacy.exists():
            print(f"未找到旧数据库：{legacy}")
            return 1
        backup = legacy.with_name(f"clp.db.backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(legacy, backup)
        shutil.copy2(legacy, DB_FILE)
        init_db(DB_FILE)
        print(f"旧数据库已迁移到：{DB_FILE}；备份：{backup}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

