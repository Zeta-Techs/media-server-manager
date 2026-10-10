from __future__ import annotations

from collections.abc import Generator

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
    return sessionmaker(bind=create_engine_from_settings(app_settings), expire_on_commit=False)


def get_session(app_settings: Settings = settings) -> Generator[Session, None, None]:
    session = session_factory(app_settings)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
