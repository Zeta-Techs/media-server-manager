from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from media_server_manager.config import Settings
from media_server_manager.infrastructure.db.legacy_sql import connect_session
from media_server_manager.infrastructure.db.runtime import reset_database
from media_server_manager.infrastructure.storage.backup import backup_sqlite, restore_sqlite

from .migrate import migrate_legacy, write_report
from .secrets import rotate_secrets


def _read_key(path: Path | None) -> str:
    if path is None:
        return Settings.from_env().encryption_key
    return path.read_text(encoding="utf-8").strip()


def _database_file() -> Path:
    return Settings.from_env().database_path


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
    migrate.add_argument("--encryption-key-file", type=Path, help="目标数据库加密密钥文件")
    migrate.add_argument("--old-encryption-key-file", type=Path, help="旧数据库加密密钥文件")
    migrate.add_argument("--dry-run", action="store_true", help="只执行完整性和结构校验")
    migrate.add_argument("--resume", action="store_true", help="继续导入一个已存在的目标数据库")
    migrate.add_argument("--migration-id", help="指定迁移批次 ID；恢复时可省略并自动选择最近失败批次")
    subparsers.add_parser("upgrade-db", help="执行 Alembic 数据库迁移")
    backup = subparsers.add_parser("backup-db", help="创建一致性 SQLite 备份")
    backup.add_argument("--database", type=Path, help="数据库路径")
    backup.add_argument("--output-dir", type=Path, help="备份目录")
    restore = subparsers.add_parser("restore-db", help="从一致性 SQLite 备份恢复数据库")
    restore.add_argument("--source", type=Path, required=True, help="备份数据库路径")
    restore.add_argument("--destination", type=Path, help="目标数据库路径")
    check = subparsers.add_parser("check-db", help="检查数据库完整性和外键")
    check.add_argument("--database", type=Path, help="数据库路径")
    rotate = subparsers.add_parser("rotate-secrets", help="原子轮换数据库敏感字段密钥")
    rotate.add_argument("--database", type=Path, help="数据库路径")
    rotate.add_argument("--old-key-file", type=Path, required=True)
    rotate.add_argument("--new-key-file", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "reset-db":
        # Reset is destructive; always create a consistent backup first.
        database = _database_file()
        backup_path = reset_database(database, backup=True)
        if backup_path:
            print(f"旧数据库已备份到：{backup_path}")
        print(f"数据库已初始化：{database}")
        return 0
    if args.command == "migrate-legacy":
        data_dir = Settings.from_env().data_dir
        destination = args.destination or data_dir / "media_server_manager.sqlite3"
        try:
            report = migrate_legacy(
                args.source,
                destination,
                copy_cache=not args.no_cache,
                encryption_key=_read_key(args.encryption_key_file),
                old_encryption_key=_read_key(args.old_encryption_key_file) if args.old_encryption_key_file else None,
                dry_run=args.dry_run,
                resume=args.resume,
                migration_id=args.migration_id,
            )
        except Exception as exc:
            failure = {
                "status": "failed",
                "source": str(args.source),
                "destination": str(destination),
                "error": str(exc),
                "finished_at": datetime.now().isoformat(timespec="seconds"),
            }
            if destination.exists() and destination.stat().st_size:
                try:
                    with connect_session(destination) as connection:
                        checkpoint = connection.execute(
                            "SELECT migration_id, status, current_table, copied_rows FROM migration_runs "
                            "WHERE source_path = ? ORDER BY id DESC LIMIT 1",
                            (str(args.source.expanduser().resolve()),),
                        ).fetchone()
                    if checkpoint:
                        failure.update(
                            {
                                "migration_id": checkpoint["migration_id"],
                                "migration_status": checkpoint["status"],
                                "current_table": checkpoint["current_table"],
                                "copied_rows": checkpoint["copied_rows"],
                            }
                        )
                except Exception:
                    pass
            write_report(failure, args.report)
            print(f"旧数据库导入失败：{exc}", file=sys.stderr)
            return 1
        write_report(report, args.report)
        return 0
    if args.command == "upgrade-db":
        from alembic import command
        from alembic.config import Config

        root = Path(__file__).resolve().parents[3]
        config = Config(str(root / "alembic.ini"))
        database = _database_file()
        config.set_main_option("sqlalchemy.url", f"sqlite:///{database.resolve().as_posix()}")
        command.upgrade(config, "head")
        print(f"数据库迁移完成：{database}")
        return 0
    if args.command == "backup-db":
        database = (args.database or _database_file()).expanduser().resolve()
        target = backup_sqlite(database, (args.output_dir or database.parent / "backups").resolve())
        print(f"数据库备份完成：{target}")
        return 0
    if args.command == "restore-db":
        destination = (args.destination or _database_file()).expanduser().resolve()
        backup_path = restore_sqlite(args.source, destination)
        if backup_path:
            print(f"恢复前数据库已备份到：{backup_path}")
        print(f"数据库恢复完成：{destination}")
        return 0
    if args.command == "check-db":
        from sqlalchemy import text

        database = (args.database or _database_file()).expanduser().resolve()
        from media_server_manager.infrastructure.db.session import Database

        with Database.for_path(database).session() as session:
            integrity = str(session.execute(text("PRAGMA integrity_check")).scalar_one())
            foreign_keys = session.execute(text("PRAGMA foreign_key_check")).all()
        if integrity.lower() != "ok" or foreign_keys:
            print(f"数据库检查失败：integrity={integrity}, foreign_keys={len(foreign_keys)}")
            return 1
        print(f"数据库检查通过：{database}")
        return 0
    if args.command == "rotate-secrets":
        database = (args.database or _database_file()).expanduser().resolve()
        result = rotate_secrets(database, _read_key(args.old_key_file), _read_key(args.new_key_file))
        print(f"已轮换敏感字段：{result['rotated_values']} 个")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
