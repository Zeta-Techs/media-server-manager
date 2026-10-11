from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from ...config import Settings, settings


class Base(DeclarativeBase):
    pass


def create_engine_from_settings(app_settings: Settings = settings) -> Engine:
    app_settings.ensure_directories()
    connect_args = {"check_same_thread": False, "timeout": 30} if app_settings.database_url.startswith("sqlite") else {}
    engine = create_engine(app_settings.database_url, future=True, pool_pre_ping=True, connect_args=connect_args)
    if app_settings.database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _configure_sqlite(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()
    return engine


def session_factory(app_settings: Settings = settings) -> sessionmaker[Session]:
    return Database(app_settings).session_factory


def get_session(app_settings: Settings = settings) -> Generator[Session, None, None]:
    database = Database(app_settings)
    try:
        with database.session() as session:
            yield session
    finally:
        database.engine.dispose()


class UnitOfWork:
    """Transaction boundary shared by application services and workers."""

    def __init__(self, factory: sessionmaker[Session], *, immediate: bool = False) -> None:
        self.session = factory()
        self.immediate = immediate

    def __enter__(self) -> "UnitOfWork":
        if self.immediate:
            self.session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if exc_type is None:
            self.session.commit()
        else:
            self.session.rollback()
        self.session.close()


class Database:
    """Configured SQLAlchemy engine and session factory.

    A single instance is intended to be shared by a Flask app or worker
    process.  Sessions remain short lived and are always owned by a UoW.
    """

    def __init__(self, app_settings: Settings = settings) -> None:
        self.settings = app_settings
        self.engine = create_engine_from_settings(app_settings)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        # SQLite creates a missing file with the process umask.  Credentials
        # and account data require a stricter mode even on hosts whose umask
        # is permissive; apply it immediately after the first engine handle is
        # opened (existing files are tightened by Settings.ensure_directories).
        if app_settings.database_url.startswith("sqlite:///"):
            database = app_settings.database_path
            if database.exists():
                try:
                    database.chmod(0o600)
                except OSError:
                    pass

    @classmethod
    def for_path(cls, database_path: Path, app_settings: Settings = settings) -> "Database":
        """Build a database handle for a compatibility/test database path."""

        database_path = Path(database_path).expanduser().resolve()
        database_path.parent.mkdir(parents=True, exist_ok=True)
        configured = replace(
            app_settings,
            data_dir=database_path.parent,
            database_url=f"sqlite:///{database_path.as_posix()}",
        )
        return cls(configured)

    @contextmanager
    def session(self) -> Generator[Session, None, None]:
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def transaction(self, *, immediate: bool = False) -> UnitOfWork:
        """Create a transaction boundary, optionally acquiring SQLite write intent."""

        return UnitOfWork(self.session_factory, immediate=immediate)
