"""Temporary boundary for SQL used by the compatibility application.

New routes and services must use repositories.  Keeping the adapter in one
module makes the remaining legacy surface searchable and removable.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence, cast

from sqlalchemy import text
from sqlalchemy.engine import CursorResult, Engine
from sqlalchemy.exc import ResourceClosedError
from sqlalchemy.orm import Session

from ...config import settings
from ..security.secrets import SecretBox

_SECRET_WORDS = ("key", "secret", "token", "credential", "password")


def _secret_box() -> SecretBox | None:
    key = os.environ.get("MSM_LOCAL_ENCRYPTION_KEY", "").strip() or settings.encryption_key.strip()
    return SecretBox(key) if key else None


def _setting_is_secret(value: object) -> bool:
    lowered = str(value).lower()
    return any(word in lowered for word in _SECRET_WORDS)


def _protect_parameters(sql: str, parameters: Any) -> Any:
    """Encrypt known legacy write positions without changing old SQL callers."""

    box = _secret_box()
    if box is None or not parameters or not isinstance(parameters, (tuple, list)):
        return parameters
    values = list(parameters)
    normalized = re.sub(r"\s+", " ", sql.strip().lower())
    if "insert into servers" in normalized and len(values) >= 7:
        values[2] = box.encrypt(str(values[2])) if values[2] and not SecretBox.is_encrypted(str(values[2])) else values[2]
        values[6] = box.encrypt(str(values[6])) if values[6] and not SecretBox.is_encrypted(str(values[6])) else values[6]
    elif "update servers set name" in normalized and "token" in normalized and len(values) >= 3:
        values[2] = box.encrypt(str(values[2])) if values[2] and not SecretBox.is_encrypted(str(values[2])) else values[2]
    elif "update servers set webhook_secret" in normalized and values:
        values[0] = box.encrypt(str(values[0])) if values[0] and not SecretBox.is_encrypted(str(values[0])) else values[0]
    elif "update servers set token" in normalized and values:
        values[0] = box.encrypt(str(values[0])) if values[0] and not SecretBox.is_encrypted(str(values[0])) else values[0]
    elif "update oauth_flows set token" in normalized and values:
        values[0] = box.encrypt(str(values[0])) if values[0] and not SecretBox.is_encrypted(str(values[0])) else values[0]
    elif "insert into notification_channels" in normalized and len(values) >= 3:
        values[2] = box.encrypt(str(values[2])) if values[2] and not SecretBox.is_encrypted(str(values[2])) else values[2]
    elif "insert into settings" in normalized and len(values) >= 2 and _setting_is_secret(values[0]):
        values[1] = box.encrypt(str(values[1])) if values[1] and not SecretBox.is_encrypted(str(values[1])) else values[1]
    elif "update settings set value" in normalized and len(values) >= 2 and _setting_is_secret(values[1]):
        values[0] = box.encrypt(str(values[0])) if values[0] and not SecretBox.is_encrypted(str(values[0])) else values[0]
    return tuple(values)


class _LegacyRow:
    """Small compatibility row backed by a SQLAlchemy result row.

    The compatibility repositories historically used both ``row[0]`` and
    ``row["column"]``.  SQLAlchemy's ``Row`` intentionally keeps string lookup
    on ``_mapping``; this adapter preserves the old read contract while the
    actual execution remains SQLAlchemy Core.
    """

    def __init__(self, values: Sequence[Any], keys: Sequence[str]) -> None:
        self._values = tuple(values)
        self._keys = tuple(keys)
        self._mapping = dict(zip(self._keys, self._values, strict=False))

    def __getitem__(self, key: int | str) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return self._mapping[key]

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def keys(self) -> list[str]:
        return list(self._keys)

    def get(self, key: str, default: Any = None) -> Any:
        return self._mapping.get(key, default)


def _decrypt_row(row: _LegacyRow) -> _LegacyRow:
    box = _secret_box()
    if box is None:
        return row
    names = [name.lower() for name in row.keys()]
    values = list(row)
    setting_key = values[names.index("key")] if "key" in names else ""
    for index, name in enumerate(names):
        if name in {"token", "webhook_secret", "url"} or (name == "value" and _setting_is_secret(setting_key)):
            value = values[index]
            if isinstance(value, str) and SecretBox.is_encrypted(value):
                values[index] = box.decrypt(value)
    return _LegacyRow(values, names)


class LegacyCursor:
    """Cursor-shaped view over a SQLAlchemy ``CursorResult``."""

    def __init__(self, result: CursorResult[Any], lastrowid: int | None = None) -> None:
        self._result = result
        self.rowcount = result.rowcount
        cursor = getattr(result, "cursor", None)
        self.lastrowid = lastrowid if lastrowid is not None else getattr(cursor, "lastrowid", None)
        try:
            keys = list(result.keys())
        except ResourceClosedError:
            keys = []
        self._keys = [str(key) for key in keys]

    def _convert(self, row: Any) -> _LegacyRow | None:
        if row is None:
            return None
        return _decrypt_row(_LegacyRow(tuple(row), self._keys))

    def fetchone(self) -> _LegacyRow | None:
        return self._convert(self._result.fetchone())

    def fetchall(self) -> list[_LegacyRow]:
        return [item for item in (self._convert(row) for row in self._result.fetchall()) if item is not None]

    def __iter__(self) -> Iterator[_LegacyRow]:
        for row in self._result:
            converted = self._convert(row)
            if converted is not None:
                yield converted


class LegacyConnection:
    """Small DB-API facade with the context semantics used by old modules."""

    def __init__(self, session: Session, *, dispose_engine: bool = True) -> None:
        self._session = session
        self._connection = session.connection()
        self._engine = cast(Engine, session.get_bind())
        self._dispose_engine = dispose_engine

    def __enter__(self) -> "LegacyConnection":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        try:
            if exc_type is None:
                self._session.commit()
            else:
                self._session.rollback()
        finally:
            self._session.close()
            if self._dispose_engine:
                self._engine.dispose()

    def __getattr__(self, name: str):
        return getattr(self._session, name)

    def _active_connection(self):
        """Return a live Core connection after a Session commit/rollback.

        SQLAlchemy releases the checked-out connection when a Session
        transaction ends.  The compatibility facade supports explicit
        commits for resumable imports, so it must reacquire the connection
        before the next DB-API-shaped operation.
        """

        if getattr(self._connection, "closed", False):
            self._connection = self._session.connection()
        return self._connection

    @staticmethod
    def _session_statement(sql: str, parameters: Sequence[Any]) -> tuple[Any, dict[str, Any]]:
        """Convert the frozen positional SQL contract to SQLAlchemy text."""

        values = list(parameters or ())
        index = 0

        def replace(_match: re.Match[str]) -> str:
            nonlocal index
            token = f":p{index}"
            index += 1
            return token

        statement = re.sub(r"\?", replace, sql)
        return text(statement), {f"p{i}": value for i, value in enumerate(values)}

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> LegacyCursor:
        protected = _protect_parameters(sql, parameters)
        # ``exec_driver_sql`` treats a list as executemany input.  Legacy
        # callers often build one positional parameter list dynamically, so
        # normalize that single execution to a tuple first.
        if isinstance(protected, list):
            protected = tuple(protected)
        statement, bindings = self._session_statement(sql, protected)
        result = self._session.execute(statement, bindings)
        lastrowid = None
        if sql.lstrip().lower().startswith("insert"):
            lastrowid = self._session.execute(text("SELECT last_insert_rowid()")).scalar_one()
        return LegacyCursor(result, int(lastrowid) if lastrowid is not None else None)

    def executemany(self, sql: str, parameters: Iterable[Sequence[Any]]) -> LegacyCursor:
        statement, _ = self._session_statement(sql, ())
        bindings = [
            {
                f"p{i}": value
                for i, value in enumerate(_protect_parameters(sql, item))
            }
            for item in parameters
        ]
        result = self._session.execute(statement, bindings)
        return LegacyCursor(result)

    def executescript(self, script: str) -> None:
        """Run a frozen migration script through the SQLAlchemy-owned driver.

        SQLite has no portable multi-statement Core API.  This method is kept
        exclusively for the historical Alembic bootstrap revision; regular
        repositories use ``execute``/``executemany`` and never call it.
        """

        connection = self._active_connection()
        raw = cast(Any, getattr(connection.connection, "driver_connection", connection.connection))
        raw.executescript(script)

    def commit(self) -> None:
        self._session.commit()

    def rollback(self) -> None:
        self._session.rollback()


class SessionSqlConnection(LegacyConnection):
    """DB-API shaped facade whose transaction is owned by a SQLAlchemy Session.

    A few compatibility services still issue carefully audited SQL while they
    are being migrated to repositories.  They must participate in the same
    session lifecycle as the rest of the application, rather than opening a
    second unmanaged sqlite connection.  The facade deliberately exposes only
    the small DB-API surface those services use (``execute``, ``executemany``
    and context-manager transaction handling).
    """

    def __init__(self, session: Session, *, dispose_engine: bool = True) -> None:
        super().__init__(session, dispose_engine=dispose_engine)

    @property
    def sqlalchemy_session(self) -> Session:
        """Return the owning SQLAlchemy session for repository adapters.

        The DB-API methods remain available only for the compatibility surface;
        first-party callers can pass this session directly to a typed
        Repository without opening a second connection.
        """

        return self._session

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        try:
            if exc_type is None:
                self._session.commit()
            else:
                self._session.rollback()
        finally:
            self._session.close()
            if self._dispose_engine:
                self._engine.dispose()


def connect_session(database: Path, handle: Any | None = None) -> SessionSqlConnection:
    """Open a compatibility SQL facade through the canonical Database handle."""

    dispose_engine = handle is None
    if handle is None:
        from .session import Database

        handle = Database.for_path(Path(database).expanduser().resolve(), settings)
    return SessionSqlConnection(handle.session_factory(), dispose_engine=dispose_engine)


def connect_legacy(database: Path) -> LegacyConnection:
    """Return a DB-API compatibility connection owned by SQLAlchemy.

    The legacy application still expects sqlite3 cursor semantics.  The
    engine owns the connection and pool, so this adapter keeps that API at a
    single migration boundary while new code uses ORM sessions.
    """

    database = Path(database).expanduser().resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    from .session import Database

    return LegacyConnection(Database.for_path(database, settings).session_factory())


def execute_legacy(database: Path, sql: str, parameters: tuple[Any, ...] = ()) -> list[_LegacyRow]:
    """Execute one compatibility query and close its short-lived connection."""

    with connect_legacy(database) as connection:
        return list(connection.execute(sql, parameters).fetchall())
