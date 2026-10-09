from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from apps.api.config import get_settings


class Base(DeclarativeBase):
    pass


def _engine_url() -> str:
    value = get_settings().database_url
    if value.startswith("sqlite://"):
        return value
    return value


engine = create_engine(
    _engine_url(),
    echo=get_settings().sql_echo,
    future=True,
    connect_args={"check_same_thread": False} if _engine_url().startswith("sqlite") else {},
    pool_pre_ping=True,
)
SessionFactory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
