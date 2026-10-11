"""Verify that the live SQLite database contains the ORM/Alembic schema."""

from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import UniqueConstraint, inspect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from media_server_manager.config import settings  # noqa: E402
from media_server_manager.infrastructure.db import models  # noqa: E402,F401
from media_server_manager.infrastructure.db.session import Base, Database  # noqa: E402


def check_schema(database: Path | None = None) -> list[str]:
    handle = Database.for_path(database, settings) if database else Database(settings)
    try:
        inspector = inspect(handle.engine)
        live_tables = set(inspector.get_table_names())
        problems: list[str] = []
        for table_name, table in Base.metadata.tables.items():
            if table_name not in live_tables:
                problems.append(f"缺少表：{table_name}")
                continue
            live_columns = {column["name"] for column in inspector.get_columns(table_name)}
            missing = set(table.c.keys()) - live_columns
            problems.extend(f"{table_name} 缺少列：{column}" for column in sorted(missing))
            live_indexes = {index["name"] for index in inspector.get_indexes(table_name)}
            expected_indexes = {index.name for index in table.indexes if index.name}
            problems.extend(
                f"{table_name} 缺少索引：{index_name}"
                for index_name in sorted(expected_indexes - live_indexes)
            )
            live_foreign_keys = {
            (
                tuple(item.get("constrained_columns") or ()),
                item.get("referred_table"),
                tuple(item.get("referred_columns") or ()),
                str((item.get("options") or {}).get("ondelete") or "").upper(),
            )
                for item in inspector.get_foreign_keys(table_name)
            }
            expected_foreign_keys = {
            (
                (foreign_key.parent.name,),
                foreign_key.column.table.name,
                (foreign_key.column.name,),
                str(foreign_key.ondelete or "").upper(),
            )
                for foreign_key in table.foreign_keys
            }
            problems.extend(
            f"{table_name} 缺少外键：{columns}->{referred_table}.{referred_columns} ondelete={ondelete or 'NO ACTION'}"
                for columns, referred_table, referred_columns, ondelete in sorted(expected_foreign_keys - live_foreign_keys)
            )
            expected_unique = {
            tuple(column.name for column in constraint.columns)
                for constraint in table.constraints
                if isinstance(constraint, UniqueConstraint)
            }
            live_unique = {
            tuple(item.get("column_names") or ())
                for item in inspector.get_unique_constraints(table_name)
            }
            live_unique.update(
            tuple(item.get("column_names") or ())
            for item in inspector.get_indexes(table_name)
                if item.get("unique")
            )
            problems.extend(
            f"{table_name} 缺少唯一约束：{columns}"
                for columns in sorted(expected_unique - live_unique)
            )
        return problems
    finally:
        handle.engine.dispose()


def main() -> int:
    problems = check_schema()
    if problems:
        print("数据库 Schema 检查失败：", file=sys.stderr)
        print("\n".join(problems), file=sys.stderr)
        return 1
    print("数据库 Schema 检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
