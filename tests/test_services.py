from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest

from media_server_manager_web.db import (
    SCHEMA_VERSION,
    IncompatibleSchemaError,
    connect,
    init_db,
    new_secret,
    reset_database,
    utcnow,
)
from media_server_manager_web.scheduling import (
    describe_schedule,
    format_utc,
    next_run_at,
    parse_utc,
    validate_schedule,
)
from media_server_manager_web.security import (
    hash_password,
    load_session_secret,
    new_csrf_token,
    verify_csrf,
    verify_password,
)
from media_server_manager_web.services import JobQueue


def add_server(db_file):
    now = utcnow()
    with connect(db_file) as db:
        server_id = db.execute(
            """
            INSERT INTO servers (name, address, token, webhook_secret, created_at, updated_at)
            VALUES ('Home', 'http://plex.local', 'token', ?, ?, ?)
            """,
            (new_secret(), now, now),
        ).lastrowid
        db.commit()
    return int(server_id)


def test_security_helpers_and_secret_sources(tmp_path, monkeypatch):
    stored = hash_password("secret")
    assert verify_password("secret", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("x", "broken")
    assert not verify_password("x", "other$salt$digest")
    assert verify_csrf("token", "token")
    assert not verify_csrf("token", "")
    assert len(new_csrf_token()) > 20
    db_file = tmp_path / "media_server_manager.db"
    first = load_session_secret(db_file)
    assert load_session_secret(db_file) == first
    with connect(db_file) as db:
        stored = db.execute("SELECT value FROM settings WHERE key = 'session_secret'").fetchone()["value"]
        assert stored == first
    monkeypatch.setenv("MSM_SECRET_KEY", "configured-secret-value")
    assert load_session_secret(db_file) == "configured-secret-value"


def test_old_database_requires_explicit_backup_reset(tmp_path):
    db_file = tmp_path / "media_server_manager.db"
    with sqlite3.connect(db_file) as db:
        db.execute("CREATE TABLE legacy_servers (id INTEGER PRIMARY KEY)")
    if sys.platform == "win32":
        db.close()
    db_file.with_name("media_server_manager.db-wal").write_bytes(b"legacy wal")
    db_file.with_name("media_server_manager.db-shm").write_bytes(b"legacy shm")

    with pytest.raises(IncompatibleSchemaError, match="reset-db --backup"):
        init_db(db_file)
    backup = reset_database(db_file, backup=True)
    assert backup is not None and backup.exists()
    assert backup.with_name(f"{backup.name}-wal").read_bytes() == b"legacy wal"
    assert backup.with_name(f"{backup.name}-shm").read_bytes() == b"legacy shm"
    with connect(db_file) as db:
        version = db.execute("SELECT version FROM schema_meta WHERE id = 1").fetchone()["version"]
    assert version == SCHEMA_VERSION


def test_schedule_validation_description_and_next_times():
    assert parse_utc(None) is None
    parsed = parse_utc("2026-07-10T00:00:00Z")
    assert parsed is not None and parsed.tzinfo == timezone.utc
    assert format_utc(parsed) == "2026-07-10T00:00:00Z"
    for kind, value in (("interval", "x"), ("interval", "0"), ("other", "1"), ("cron", "bad")):
        with pytest.raises(ValueError):
            validate_schedule(kind, value)
    base = datetime(2026, 7, 10, tzinfo=timezone.utc)
    assert next_run_at("interval", "30", base) == "2026-07-10T00:30:00Z"
    assert next_run_at("cron", "0 10 * * 0", base).startswith("2026-07-12T02:00:00")
    assert describe_schedule("interval", "bad") == "间隔时间无效"
    assert describe_schedule("interval", "120") == "每 2 小时执行一次"
    assert describe_schedule("interval", "15") == "每 15 分钟执行一次"
    assert describe_schedule("other", "") == "未设置"
    assert describe_schedule("cron", "bad") == "bad"
    assert describe_schedule("cron", "30 22 * * *") == "每天 22:30"
    assert describe_schedule("cron", "30 22 * * mon") == "每周一 22:30"
    assert describe_schedule("cron", "30 22 1 * *") == "每月 1 日 22:30"
    assert describe_schedule("cron", "0 1 * 1 *").startswith("Cron:")


def test_job_queue_read_cancel_retry_and_errors(tmp_path):
    db_file = tmp_path / "media_server_manager.db"
    init_db(db_file)
    queue = JobQueue(db_file)
    with pytest.raises(ValueError):
        queue.create_job("unknown", 1)
    with pytest.raises(ValueError):
        queue.create_job("all", 1)
    server_id = add_server(db_file)
    first = queue.create_job("all", server_id, {"scope": {}})
    assert queue.get_job(first)["payload"] == {"scope": {}}
    assert queue.list_jobs(limit=9999)[0]["id"] == first
    logs = queue.list_logs(first, limit=1)
    assert len(logs) == 1
    with connect(db_file) as db:
        db.execute(
            "INSERT INTO job_logs (job_id, message, created_at) VALUES (?, 'next', ?)",
            (first, utcnow()),
        )
        db.commit()
    assert queue.list_logs(first, after_id=logs[0]["id"])[0]["message"] == "next"
    assert queue.cancel_job(first) == "cancelled"
    assert queue.cancel_job(first) == "cancelled"
    retry = queue.retry_job(first)
    assert queue.get_job(retry)["retry_of"] == first
    with pytest.raises(ValueError):
        queue.retry_job(retry)
    with pytest.raises(ValueError):
        queue.get_job(999)
    with pytest.raises(ValueError):
        queue.cancel_job(999)
    with connect(db_file) as db:
        db.execute("UPDATE jobs SET status = 'succeeded', finished_at = ? WHERE id = ?", (utcnow(), retry))
        db.commit()
    rollback = queue.rollback_job(retry)
    assert queue.get_job(rollback)["type"] == "rollback"
    extra = queue.create_job("all", server_id, {})
    assert queue.cancel_queued_job(extra) is True
    queue.shutdown()


def test_preview_validation_and_worker_status(tmp_path):
    db_file = tmp_path / "media_server_manager.db"
    init_db(db_file)
    server_id = add_server(db_file)
    queue = JobQueue(db_file)
    queued = queue.create_job("localize", server_id, {"mode": "dry_run"})
    with pytest.raises(ValueError):
        queue.apply_preview_job(queued)
    with connect(db_file) as db:
        db.execute("UPDATE jobs SET status = 'succeeded', finished_at = ? WHERE id = ?", (utcnow(), queued))
        db.commit()
    with pytest.raises(ValueError):
        queue.apply_preview_job(queued)
    normal = queue.create_job("all", server_id, {})
    with connect(db_file) as db:
        db.execute("UPDATE jobs SET status = 'succeeded', finished_at = ? WHERE id = ?", (utcnow(), normal))
        db.execute(
            "INSERT INTO worker_heartbeats (worker_id, started_at, heartbeat_at, pid, concurrency) VALUES ('w', ?, ?, 1, 1)",
            (utcnow(), utcnow()),
        )
        db.commit()
    assert queue.worker_status()["online"] is True
    with pytest.raises(ValueError):
        queue.apply_preview_job(normal)
    stale = (
        (datetime.now(timezone.utc) - timedelta(minutes=5))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    with connect(db_file) as db:
        db.execute("UPDATE worker_heartbeats SET heartbeat_at = ?", (stale,))
        db.commit()
    assert queue.worker_status()["online"] is False
