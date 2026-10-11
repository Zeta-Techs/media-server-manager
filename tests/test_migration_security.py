from __future__ import annotations

import sqlite3
from pathlib import Path

from media_server_manager_admin.migrate import migrate_legacy
from media_server_manager_admin.secrets import rotate_secrets
from media_server_manager_web.db import connect, init_db


def test_legacy_secret_is_reencrypted_and_rotatable(tmp_path: Path) -> None:
    source = tmp_path / "legacy.db"
    init_db(source)
    with sqlite3.connect(source) as connection:
        connection.execute(
            "INSERT INTO servers(name,address,token,webhook_secret,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            ("Plex", "http://plex", "plain-token", "plain-webhook", "now", "now"),
        )
        connection.commit()

    destination = tmp_path / "data" / "media_server_manager.sqlite3"
    report = migrate_legacy(source, destination, encryption_key="new-key")
    assert report["status"] == "succeeded"
    with sqlite3.connect(destination) as connection:
        raw = connection.execute("SELECT token FROM servers").fetchone()[0]
        assert raw.startswith("enc:v1:")

    result = rotate_secrets(destination, "new-key", "rotated-key")
    assert result["rotated_values"] >= 2
    with sqlite3.connect(destination) as connection:
        rotated = connection.execute("SELECT token FROM servers").fetchone()[0]
        assert rotated.startswith("enc:v1:")


def test_legacy_migration_dry_run_does_not_create_destination(tmp_path: Path) -> None:
    source = tmp_path / "legacy.db"
    init_db(source)
    destination = tmp_path / "data" / "media_server_manager.sqlite3"
    report = migrate_legacy(source, destination, dry_run=True)
    assert report["status"] == "validated"
    assert not destination.exists()


def test_legacy_migration_resume_uses_failed_table_checkpoint(tmp_path: Path) -> None:
    source = tmp_path / "legacy.db"
    init_db(source)
    with sqlite3.connect(source) as connection:
        connection.execute(
            "INSERT INTO servers(name,address,token,webhook_secret,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            ("Plex", "http://plex", "plain-token", "plain-webhook", "now", "now"),
        )
        connection.commit()

    destination = tmp_path / "data" / "media_server_manager.sqlite3"
    try:
        migrate_legacy(source, destination)
    except RuntimeError as exc:
        assert "加密密钥" in str(exc)
    else:  # pragma: no cover - the import must reject plaintext secrets
        raise AssertionError("缺少目标密钥时导入不应成功")

    with sqlite3.connect(destination) as connection:
        assert connection.execute(
            "SELECT status, current_table FROM migration_runs"
        ).fetchone()[0] == "failed"

    report = migrate_legacy(source, destination, encryption_key="resume-key", resume=True)
    assert report["status"] == "succeeded"
    with sqlite3.connect(destination) as connection:
        status, current_table, token = connection.execute(
            "SELECT status, current_table, (SELECT token FROM servers LIMIT 1) FROM migration_runs"
        ).fetchone()
        assert status == "succeeded"
        assert current_table == ""
        assert token.startswith("enc:v1:")


def test_legacy_connection_encrypts_new_server_values(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "runtime.db"
    init_db(database)
    monkeypatch.setenv("MSM_LOCAL_ENCRYPTION_KEY", "runtime-key")
    with connect(database) as connection:
        connection.execute(
            "INSERT INTO servers(name,address,token,skip_libraries,pinyin_mode,auth_source,webhook_secret,enabled,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("Plex", "http://plex", "token", "", "first_letter", "manual", "webhook", 1, "now", "now"),
        )
    with sqlite3.connect(database) as raw:
        token, webhook = raw.execute("SELECT token, webhook_secret FROM servers").fetchone()
        assert token.startswith("enc:v1:")
        assert webhook.startswith("enc:v1:")
    with connect(database) as connection:
        row = connection.execute("SELECT token, webhook_secret FROM servers").fetchone()
        assert row["token"] == "token"
        assert row["webhook_secret"] == "webhook"
