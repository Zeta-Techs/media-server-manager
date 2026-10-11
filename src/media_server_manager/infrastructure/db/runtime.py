from __future__ import annotations

import json
import os
import secrets
import shutil
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional, Sequence

from sqlalchemy import create_engine

from media_server_manager.config import settings
from media_server_manager.core import CONFIG_DIR, ServerConfig, split_skip_libraries

from .legacy_sql import LegacyConnection, SessionSqlConnection, connect_session

SCHEMA_VERSION = 5
# Keep the legacy filename when an old CONFIG_DIR deployment is explicitly
# selected.  New deployments use the canonical SQLite filename under
# MSM_DATA_DIR; this lets the browser and migration tests continue to exercise
# their isolated legacy fixtures during the transition.
_legacy_config_env = bool(
    os.environ.get("MSM_CONFIG_DIR", "").strip() or os.environ.get("MSM_CONFIG_PATH", "").strip()
)
DB_FILE = CONFIG_DIR / ("media_server_manager.db" if _legacy_config_env else "media_server_manager.sqlite3")
DEFAULT_USERNAME = "admin"


class IncompatibleSchemaError(RuntimeError):
    pass


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def connect(db_file: Path | None = None) -> SessionSqlConnection:
    db_file = Path(db_file or DB_FILE)
    return connect_session(db_file)


@contextmanager
def get_db(db_file: Path | None = None) -> Iterator[LegacyConnection]:
    # ``connect`` is session-backed.  Let its context manager commit or roll
    # back the owning SQLAlchemy Session so the compatibility helper cannot
    # leave a pooled connection open.
    with connect(db_file) as conn:
        yield conn


def row_to_dict(row: Mapping[str, Any] | Any | None) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    mapping = getattr(row, "_mapping", None)
    return dict(mapping if mapping is not None else row)


def rows_to_dicts(rows: Sequence[Any]) -> list[Dict[str, Any]]:
    return [row_to_dict(row) or {} for row in rows]


def _existing_schema_version(db_file: Path) -> int | None:
    if not db_file.exists() or db_file.stat().st_size == 0:
        return None
    # SQLite's URI read-only mode keeps schema probing from creating or
    # mutating a database while still using SQLAlchemy's canonical engine.
    uri = f"sqlite:///file:{db_file.resolve().as_posix()}?mode=ro&immutable=1&uri=true"
    engine = create_engine(uri, connect_args={"uri": True}, pool_pre_ping=True)
    incompatible = False
    result: int | None = None
    with engine.connect() as connection:
        tables = {
            str(row[0])
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        # Alembic creates this marker before running the first revision.  It is
        # not application data and must not be mistaken for an old schema.
        if tables <= {"alembic_version"}:
            return None
        if not tables:
            return None
        if "schema_meta" not in tables:
            incompatible = True
        else:
            row = connection.exec_driver_sql("SELECT version FROM schema_meta WHERE id = 1").fetchone()
            result = int(row[0]) if row else 0
    engine.dispose()
    if incompatible:
        raise IncompatibleSchemaError(
            "检测到旧版数据库。请先运行 `python -m media_server_manager_admin reset-db --backup`。"
        )
    return result


def init_db(db_file: Path | None = None) -> None:
    """Compatibility bridge; schema bootstrap is owned by Alembic 0001."""
    from .legacy_schema import init_db as _bootstrap
    _bootstrap(db_file)

def reset_database(db_file: Path | None = None, backup: bool = True) -> Path | None:
    db_file = Path(db_file or DB_FILE)
    backup_path: Path | None = None
    if db_file.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = db_file.with_name(f"{db_file.name}.backup-{stamp}")
        if backup:
            shutil.copy2(str(db_file), str(backup_path))
            db_file.unlink()
        else:
            db_file.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{db_file}{suffix}")
        if sidecar.exists():
            if backup and backup_path is not None:
                shutil.copy2(str(sidecar), f"{backup_path}{suffix}")
                sidecar.unlink()
            else:
                sidecar.unlink()
    init_db(db_file)
    return backup_path


def server_config_from_row(row: Mapping[str, Any] | Any, db_file: Path | None = None) -> ServerConfig:
    data = row_to_dict(row) or {}
    client_identifier = get_setting("plex_client_identifier", db_file) or "media-server-manager"
    return ServerConfig(
        name=data.get("name", ""),
        address=data["address"],
        token=data["token"],
        skip_libraries=split_skip_libraries(data.get("skip_libraries", "")),
        pinyin_mode=data.get("pinyin_mode", "first_letter"),
        client_identifier=client_identifier,
    )


def decode_payload(value: str | None) -> Dict[str, Any]:
    try:
        payload = json.loads(value or "{}")
        return payload if isinstance(payload, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def get_setting(key: str, db_file: Path | None = None) -> str:
    from .repositories.auth import AuthRepository
    from .session import Database

    database = Database.for_path(Path(db_file), settings) if db_file else Database(settings)
    try:
        with database.transaction() as uow:
            return AuthRepository(uow.session).setting(key)
    finally:
        database.engine.dispose()


def set_setting(key: str, value: str, db_file: Path | None = None) -> None:
    from .repositories.auth import AuthRepository
    from .session import Database

    database = Database.for_path(Path(db_file), settings) if db_file else Database(settings)
    try:
        with database.transaction() as uow:
            AuthRepository(uow.session).set_setting(key, value)
    finally:
        database.engine.dispose()


def load_tags(db_file: Path | None = None) -> Dict[str, str]:
    from .repositories.automation import AutomationRepository
    from .session import Database

    database = Database.for_path(Path(db_file), settings) if db_file else Database(settings)
    try:
        with database.transaction() as uow:
            return AutomationRepository(uow.session).tags()
    finally:
        database.engine.dispose()


def save_tags(tags: Dict[str, str], db_file: Path | None = None) -> None:
    from .repositories.automation import AutomationRepository
    from .session import Database

    database = Database.for_path(Path(db_file), settings) if db_file else Database(settings)
    try:
        with database.transaction() as uow:
            AutomationRepository(uow.session).replace_tags(tags)
    finally:
        database.engine.dispose()
