from __future__ import annotations

import argparse
import os
from pathlib import Path

from media_server_manager_web.db import DB_FILE, reset_database

from .migrate import migrate_legacy, write_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Media Server Manager 管理命令")
    subparsers = parser.add_subparsers(dest="command", required=True)
    reset = subparsers.add_parser("reset-db", help="重建不兼容的数据库")
    reset.add_argument("--backup", action="store_true", help="重建前备份旧数据库")
    migrate = subparsers.add_parser("migrate-legacy", help="导入旧版 SQLite 数据库")
    migrate.add_argument("--source", type=Path, required=True, help="旧版数据库路径")
    migrate.add_argument("--destination", type=Path, help="新数据库路径，默认使用 MSM_DATA_DIR")
    migrate.add_argument("--report", type=Path, help="将导入报告写入 JSON 文件")
    migrate.add_argument("--no-cache", action="store_true", help="不复制旧媒体缓存")
    subparsers.add_parser("upgrade-db", help="执行 Alembic 数据库迁移")
    args = parser.parse_args(argv)
    if args.command == "reset-db":
        backup = reset_database(DB_FILE, backup=args.backup)
        if backup:
            print(f"旧数据库已备份到：{backup}")
        print(f"数据库已初始化：{DB_FILE}")
        return 0
    if args.command == "migrate-legacy":
        data_dir = Path(
            os.environ.get("MSM_DATA_DIR")
            or os.environ.get("MSM_CONFIG_DIR")
            or os.environ.get("MSM_CONFIG_PATH")
            or "data"
        ).expanduser()
        destination = args.destination or data_dir / "media_server_manager.sqlite3"
        report = migrate_legacy(args.source, destination, copy_cache=not args.no_cache)
        write_report(report, args.report)
        return 0
    if args.command == "upgrade-db":
        from media_server_manager_web.migrations import upgrade_head

        upgrade_head(DB_FILE)
        print(f"数据库迁移完成：{DB_FILE}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
