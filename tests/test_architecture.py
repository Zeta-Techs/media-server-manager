from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from media_server_manager_admin.migrate import migrate_legacy
from media_server_manager_web.db import init_db
from src.media_server_manager.infrastructure.db.models import Server
from src.media_server_manager.infrastructure.db.session import Base
from src.media_server_manager.infrastructure.security import SecretBox
from src.media_server_manager.infrastructure.storage.backup import backup_sqlite


def test_secret_box_round_trip_and_legacy_passthrough() -> None:
    box = SecretBox("unit-test-key")
    encrypted = box.encrypt("plex-token")
    assert encrypted.startswith("enc:v1:")
    assert box.decrypt(encrypted) == "plex-token"
    assert box.decrypt("legacy-value") == "legacy-value"


def test_orm_secret_columns_are_encrypted_at_rest(monkeypatch) -> None:
    monkeypatch.setenv("MSM_LOCAL_ENCRYPTION_KEY", "unit-test-key")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[Server.__table__])
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        session.add(
            Server(
                name="Plex",
                address="http://plex",
                token="token",
                webhook_secret="webhook",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
        assert session.scalars(select(Server)).one().token == "token"
    with engine.connect() as connection:
        raw = connection.exec_driver_sql("SELECT token FROM servers").scalar_one()
        assert raw.startswith("enc:v1:")
    monkeypatch.delenv("MSM_LOCAL_ENCRYPTION_KEY", raising=False)


def test_sqlite_backup_is_consistent(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    init_db(source)
    with sqlite3.connect(source) as db:
        db.execute("INSERT INTO settings(key, value) VALUES ('test', 'value')")
        db.commit()
    backup = backup_sqlite(source, tmp_path / "backups")
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT value FROM settings WHERE key='test'").fetchone()[0] == "value"
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_legacy_migration_preserves_business_rows(tmp_path: Path) -> None:
    source = tmp_path / "legacy.db"
    init_db(source)
    with sqlite3.connect(source) as db:
        db.execute(
            "INSERT INTO users(username,password_hash,created_at,updated_at) VALUES ('admin','hash','now','now')"
        )
        db.commit()
    destination = tmp_path / "data" / "media_server_manager.sqlite3"
    report = migrate_legacy(source, destination)
    assert report["counts"]["users"] == 1
    with sqlite3.connect(destination) as db:
        assert db.execute("SELECT username FROM users").fetchone()[0] == "admin"
