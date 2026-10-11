from __future__ import annotations

import gc
import json
import shutil
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection

from media_server_manager.config import Settings
from media_server_manager.infrastructure.db.legacy_sql import connect_session
from media_server_manager.infrastructure.security import SecretBox

_SKIP_TABLES = {"schema_meta", "alembic_version", "migration_runs"}
_SECRET_COLUMNS = {
    ("servers", "token"),
    ("servers", "webhook_secret"),
    ("oauth_flows", "token"),
    ("notification_channels", "url"),
}
_SECRET_SETTING_WORDS = ("key", "secret", "token", "credential", "password")


@contextmanager
def _open_sqlite(path: Path | str, *, uri: bool = False):
    """Open a legacy SQLite source through a SQLAlchemy read-only engine."""
    if uri:
        url = f"sqlite:///{path}&uri=true"
        engine = create_engine(url, connect_args={"uri": True}, pool_pre_ping=True)
    else:
        database = Path(path).expanduser().resolve()
        engine = create_engine(f"sqlite:///{database.as_posix()}", pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            yield connection
    finally:
        engine.dispose()


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _row_value(row: Any, key: int | str) -> Any:
    mapping = getattr(row, "_mapping", None)
    return mapping[key] if mapping is not None and isinstance(key, str) else row[key]


def _is_secret(table: str, column: str, row: Any) -> bool:
    if (table, column) in _SECRET_COLUMNS:
        return True
    if column.lower() in {"token", "secret", "api_key", "webhook_secret", "auth_token", "password"}:
        return True
    if table == "settings" and column == "value":
        key = str(_row_value(row, "key")).lower()
        return any(word in key for word in _SECRET_SETTING_WORDS)
    return False


def _upgrade_destination(destination: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{destination.as_posix()}")
    command.upgrade(config, "head")
    # Alembic loads revision modules dynamically; force finalizers so the
    # SQLite driver releases every handle before the import transaction opens.
    gc.collect()


def _source_tables(connection: Connection) -> list[str]:
    return [
        str(row[0])
        for row in connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        if str(row[0]) not in _SKIP_TABLES
    ]


def _copy_cache(source: Path, destination: Path) -> tuple[bool, Path | None]:
    candidates = [source.parent / "media_cache" / "tmdb", source.parent / "media_cache"]
    cache_source = next((path for path in candidates if path.exists() and path.is_dir()), None)
    if cache_source is None:
        return False, None
    target = destination.parent / "cache" / "tmdb"
    target.mkdir(parents=True, exist_ok=True)
    for item in cache_source.rglob("*"):
        if item.is_file():
            output = target / item.relative_to(cache_source)
            output.parent.mkdir(parents=True, exist_ok=True)
            if not output.exists():
                shutil.copy2(item, output)
    return True, target


def _relocated_cache_path(value: str, source: Path, destination: Path, cache_dir: Path | None) -> str:
    """Translate legacy absolute/relative cache paths without flattening them."""
    if not value or cache_dir is None:
        return value
    raw = Path(value)
    roots = [source.parent / "media_cache" / "tmdb", source.parent / "media_cache"]
    relative: Path | None = None
    for root in roots:
        try:
            relative = raw.resolve().relative_to(root.resolve()) if raw.is_absolute() else raw.relative_to(root)
            break
        except (ValueError, OSError):
            continue
    if relative is None:
        parts = raw.parts
        try:
            marker = next(index for index, part in enumerate(parts) if part.casefold() == "tmdb")
            relative = Path(*parts[marker:])
        except StopIteration:
            relative = Path(raw.name)
    return str(cache_dir / relative)


def _resume_state(destination: Path, source: Path) -> tuple[str | None, str | None]:
    """Return the last unfinished migration id and table for a destination.

    A resume invocation does not need to repeat the opaque migration id.  The
    newest unfinished record for the same source is the only safe candidate;
    completed imports are deliberately ignored so a successful destination
    cannot be accidentally reopened with ``--resume``.
    """

    if not destination.exists() or destination.stat().st_size == 0:
        return None, None
    try:
        with connect_session(destination) as connection:
            row = connection.execute(
                "SELECT migration_id, current_table FROM migration_runs "
                "WHERE source_path = ? AND status != 'succeeded' "
                "ORDER BY id DESC LIMIT 1",
                (str(source),),
            ).fetchone()
            if row is None:
                return None, None
            return str(row["migration_id"]), str(row["current_table"] or "")
    except Exception:
        # A zero byte/partially bootstrapped destination is still recoverable;
        # Alembic will create the tracking table below.
        return None, None


def migrate_legacy(
    source: Path,
    destination: Path,
    copy_cache: bool = True,
    *,
    encryption_key: str | bytes | None = None,
    old_encryption_key: str | bytes | None = None,
    dry_run: bool = False,
    resume: bool = False,
    migration_id: str | None = None,
) -> dict[str, Any]:
    """Import a legacy SQLite database table by table, leaving the source untouched."""

    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"找不到旧数据库：{source}")
    if destination.exists() and not resume:
        raise FileExistsError(f"目标数据库已存在：{destination}")
    resume_table: str | None = None
    if resume and migration_id is None:
        migration_id, resume_table = _resume_state(destination, source)
    migration_id = migration_id or uuid.uuid4().hex
    configured = Settings.from_env()
    new_key = encryption_key if encryption_key is not None else configured.encryption_key
    old_key = old_encryption_key if old_encryption_key is not None else configured.encryption_key
    new_box = SecretBox(new_key) if new_key else None
    old_box = SecretBox(old_key) if old_key else None
    report: dict[str, Any] = {
        "migration_id": migration_id,
        "source": str(source),
        "destination": str(destination),
        "status": "dry-run" if dry_run else "running",
        "tables": {},
        "warnings": [],
        "sensitive_columns": [],
        "started_at": _utcnow(),
    }

    with _open_sqlite(f"file:{source}?mode=ro", uri=True) as old:
        integrity = str(old.exec_driver_sql("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = old.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if integrity.lower() != "ok" or foreign_keys:
            raise RuntimeError(f"旧数据库完整性检查失败：integrity={integrity}, foreign_keys={len(foreign_keys)}")
        all_tables = [
            str(row[0])
            for row in old.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        tables = _source_tables(old)
        report["source_tables"] = tables
        if "schema_meta" in all_tables:
            version_row = old.exec_driver_sql("SELECT version FROM schema_meta WHERE id = 1").fetchone()
            report["source_schema_version"] = int(version_row[0]) if version_row else None
        elif "alembic_version" in all_tables:
            revision_row = old.exec_driver_sql("SELECT version_num FROM alembic_version LIMIT 1").fetchone()
            report["source_schema_version"] = str(revision_row[0]) if revision_row else None
        report["source_counts"] = {
            table: int(old.exec_driver_sql(f"SELECT COUNT(*) FROM {_quote(table)}").fetchone()[0]) for table in tables
        }
        if resume_table and resume_table not in tables:
            # A stale checkpoint can occur after a manual schema repair.  It
            # is safer to replay the complete import than to report success
            # without copying any source table.
            resume_table = None
        # Keep the original report key for existing automation while the
        # richer source_counts/tables sections are adopted.
        report["counts"] = dict(report["source_counts"])
        if dry_run:
            report["status"] = "validated"
            report["finished_at"] = _utcnow()
            return report

        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            destination.touch()
        _upgrade_destination(destination)
        copied_cache, cache_dir = (_copy_cache(source, destination) if copy_cache else (False, None))
        report["cache_copied"] = copied_cache
        if cache_dir:
            report["cache_path"] = str(cache_dir)

        # The destination is always managed by the canonical SQLAlchemy
        # session.  The source remains a read-only SQLite compatibility input
        # because its schema is unknown until inspected.
        with connect_session(destination) as new:
            new.execute("PRAGMA foreign_keys=OFF")
            # Track the import before the first table is touched.  Each table
            # is committed independently so an interrupted process can resume
            # at the failed table without losing completed work.
            existing_run = new.execute(
                "SELECT id, current_table, status FROM migration_runs WHERE migration_id = ?",
                (migration_id,),
            ).fetchone()
            if existing_run is None:
                if resume:
                    raise RuntimeError(
                        "目标数据库没有可恢复的迁移批次；请移除不完整目标库后重新导入，或指定已有 migration-id"
                    )
                new.execute(
                    "INSERT INTO migration_runs "
                    "(migration_id, source_path, destination_path, status, current_table, copied_rows, error, started_at) "
                    "VALUES (?, ?, ?, 'running', '', 0, '', ?)",
                    (migration_id, str(source), str(destination), report["started_at"]),
                )
                new.commit()
            elif str(existing_run["status"]) == "succeeded":
                raise RuntimeError(f"迁移批次已完成：{migration_id}")
            elif not resume:
                raise RuntimeError(f"目标数据库已有未完成迁移批次：{migration_id}，请使用 --resume")
            elif not resume_table:
                resume_table = str(existing_run["current_table"] or "")

            # Alembic bootstrap may seed the default tag dictionary.  A
            # legacy import must reproduce the source exactly, so clear that
            # optional seed before copying source rows on a fresh run only.
            if not resume and "tag_mappings" in tables:
                new.execute("DELETE FROM tag_mappings")
                new.commit()

            skipping = bool(resume_table)
            copied_total = 0
            try:
                # Alembic bootstrap may seed the default tag dictionary.  A
                # legacy import must reproduce the source exactly, so clear
                # that optional seed before copying source rows.
                for table in tables:
                    if skipping:
                        if table != resume_table:
                            continue
                        skipping = False
                    # Persist the checkpoint before doing work.  If the
                    # process dies during this table, --resume retries it.
                    new.execute(
                        "UPDATE migration_runs SET status = 'running', current_table = ?, error = '' WHERE migration_id = ?",
                        (table, migration_id),
                    )
                    new.commit()
                    target_columns = {
                        str(row[1]) for row in new.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
                    }
                    if not target_columns:
                        report["warnings"].append(f"目标库缺少表：{table}")
                        new.execute(
                            "UPDATE migration_runs SET current_table = '', error = ? WHERE migration_id = ?",
                            (f"目标库缺少表：{table}", migration_id),
                        )
                        new.commit()
                        continue
                    source_columns = [str(row[1]) for row in old.exec_driver_sql(f"PRAGMA table_info({_quote(table)})")]
                    columns = [column for column in source_columns if column in target_columns]
                    if not columns:
                        continue
                    placeholders = ",".join("?" for _ in columns)
                    sql = (
                        f"INSERT OR IGNORE INTO {_quote(table)} "
                        f"({','.join(_quote(column) for column in columns)}) VALUES ({placeholders})"
                    )
                    rows = old.exec_driver_sql(
                        f"SELECT {','.join(_quote(column) for column in columns)} FROM {_quote(table)}"
                    )
                    copied = 0
                    for row in rows:
                        values: list[Any] = []
                        for column in columns:
                            value = _row_value(row, column)
                            if table == "tmdb_images" and column == "cache_path" and value and cache_dir:
                                value = _relocated_cache_path(str(value), source, destination, cache_dir)
                            if value is not None and _is_secret(table, column, row):
                                if f"{table}.{column}" not in report["sensitive_columns"]:
                                    report["sensitive_columns"].append(f"{table}.{column}")
                                if new_box is None:
                                    raise RuntimeError(
                                        f"导入敏感字段前必须配置目标加密密钥：{table}.{column}"
                                    )
                                try:
                                    source_value = str(value)
                                    if SecretBox.is_encrypted(source_value):
                                        if old_box is None:
                                            raise ValueError("源字段已加密，但未提供旧密钥")
                                        source_value = old_box.decrypt(source_value)
                                    value = new_box.encrypt(source_value)
                                except Exception as exc:
                                    raise RuntimeError(f"无法重加密 {table}.{column}: {exc}") from exc
                            values.append(value)
                        new.execute(sql, values)
                        copied += 1
                    target_count = int(new.execute(f"SELECT COUNT(*) FROM {_quote(table)}").fetchone()[0])
                    report["tables"][table] = {
                        "source": report["source_counts"][table],
                        "copied": copied,
                        "target": target_count,
                    }
                    if target_count != report["source_counts"][table]:
                        raise RuntimeError(
                            f"表 {table} 导入行数不一致：源库 {report['source_counts'][table]}，目标库 {target_count}"
                        )
                    copied_total += copied
                    new.execute(
                        "UPDATE migration_runs SET current_table = '', copied_rows = ?, error = '' WHERE migration_id = ?",
                        (copied_total, migration_id),
                    )
                    new.commit()
                new.execute("PRAGMA foreign_keys=ON")
                violations = new.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise RuntimeError(f"目标数据库外键检查失败：{len(violations)} 条")
                target_integrity = str(new.execute("PRAGMA integrity_check").fetchone()[0])
                if target_integrity.lower() != "ok":
                    raise RuntimeError(f"目标数据库完整性检查失败：{target_integrity}")
                new.execute(
                    "UPDATE migration_runs SET status = 'succeeded', current_table = '', copied_rows = ?, error = '', finished_at = ? "
                    "WHERE migration_id = ?",
                    (sum(item.get("copied", 0) for item in report["tables"].values()), _utcnow(), migration_id),
                )
                new.commit()
            except Exception as exc:
                new.rollback()
                # The failed table checkpoint was committed before the table
                # transaction began.  Record the error in a separate short
                # transaction so the next invocation can reliably resume.
                try:
                    with connect_session(destination) as failure:
                        failure.execute(
                            "UPDATE migration_runs SET status = 'failed', error = ?, finished_at = ? WHERE migration_id = ?",
                            (str(exc), _utcnow(), migration_id),
                        )
                        failure.commit()
                except Exception:
                    # Preserve the original import error if the reporting
                    # connection itself is unavailable.
                    pass
                raise
    try:
        destination.chmod(0o600)
    except OSError:
        pass
    report["status"] = "succeeded"
    report["finished_at"] = _utcnow()
    # A small sidecar makes completion explicit for automation and prevents a
    # partially imported destination from being mistaken for a usable database.
    marker = destination.with_name(destination.name + ".migration-complete.json")
    report["completion_marker"] = str(marker)
    marker.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def write_report(report: dict[str, Any], path: Path | None = None) -> None:
    if path is None:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
