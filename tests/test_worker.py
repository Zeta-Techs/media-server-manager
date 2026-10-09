from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone

from media_server_manager_web.db import connect, init_db, new_secret, utcnow
from media_server_manager_web.services import JobQueue
from media_server_manager_worker.worker import Worker, worker_is_healthy


def add_server(db_file, name):
    now = utcnow()
    with connect(db_file) as db:
        server_id = db.execute(
            """
            INSERT INTO servers (name, address, token, webhook_secret, created_at, updated_at)
            VALUES (?, 'http://plex.local', 'token', ?, ?, ?)
            """,
            (name, new_secret(), now, now),
        ).lastrowid
        db.commit()
    return int(server_id)


def test_worker_claims_different_servers_without_starvation(tmp_path):
    db_file = tmp_path / "media_server_manager.db"
    init_db(db_file)
    first = add_server(db_file, "A")
    second = add_server(db_file, "B")
    queue = JobQueue(db_file)
    a1 = queue.create_job("notification_event", first, {})
    a2 = queue.create_job("notification_event", first, {})
    b1 = queue.create_job("notification_event", second, {})
    gate = threading.Event()
    worker = Worker(db_file, concurrency=2)
    worker._execute_job = lambda _job_id: gate.wait(2)
    worker.run_once()
    claimed = {job_id for job_id, _ in worker.futures.values()}
    assert claimed == {a1, b1}
    assert queue.get_job(a2)["status"] == "queued"
    gate.set()
    for future in list(worker.futures):
        future.result(timeout=2)
    worker._graceful_shutdown()


def test_worker_restart_interrupts_running_but_keeps_queued(tmp_path):
    db_file = tmp_path / "media_server_manager.db"
    init_db(db_file)
    server_id = add_server(db_file, "A")
    queue = JobQueue(db_file)
    running = queue.create_job("notification_event", server_id, {})
    queued = queue.create_job("notification_event", server_id, {})
    with connect(db_file) as db:
        db.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (running,))
        db.commit()
    worker = Worker(db_file, concurrency=1)
    worker._recover_interrupted_jobs()
    assert queue.get_job(running)["status"] == "interrupted"
    assert queue.get_job(queued)["status"] == "queued"
    worker._graceful_shutdown()


def test_due_schedule_is_enqueued_once_and_advanced(tmp_path):
    db_file = tmp_path / "media_server_manager.db"
    init_db(db_file)
    server_id = add_server(db_file, "A")
    past = (
        (datetime.now(timezone.utc) - timedelta(minutes=1))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    now = utcnow()
    with connect(db_file) as db:
        db.execute(
            """
            INSERT INTO schedules (
                server_id, name, schedule_type, schedule_value, payload, enabled,
                next_run_at, created_at, updated_at
            ) VALUES (?, 'Daily', 'cron', '0 10 * * *', ?, 1, ?, ?, ?)
            """,
            (server_id, json.dumps({"type": "notification_event", "payload": {}}), past, now, now),
        )
        db.commit()
    worker = Worker(db_file, concurrency=1)
    worker._enqueue_due_schedules()
    worker._enqueue_due_schedules()
    with connect(db_file) as db:
        assert db.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"] == 1
        assert db.execute("SELECT next_run_at FROM schedules").fetchone()["next_run_at"] > utcnow()
    worker._graceful_shutdown()


def test_worker_executes_persisted_job(tmp_path):
    db_file = tmp_path / "media_server_manager.db"
    init_db(db_file)
    server_id = add_server(db_file, "A")
    queue = JobQueue(db_file)
    job_id = queue.create_job("notification_event", server_id, {"event": "test"})
    worker = Worker(db_file, concurrency=1)
    worker.run_once()
    for _ in range(40):
        worker.run_once()
        if queue.get_job(job_id)["status"] == "succeeded":
            break
        time.sleep(0.05)
    assert queue.get_job(job_id)["status"] == "succeeded"
    worker._graceful_shutdown()


def test_worker_heartbeat_health_cleanup_and_error_path(tmp_path, monkeypatch):
    db_file = tmp_path / "media_server_manager.db"
    init_db(db_file)
    server_id = add_server(db_file, "A")
    queue = JobQueue(db_file)
    old_job = queue.create_job("notification_event", server_id, {})
    old = "2000-01-01T00:00:00Z"
    with connect(db_file) as db:
        db.execute("UPDATE jobs SET status = 'succeeded', finished_at = ? WHERE id = ?", (old, old_job))
        db.execute(
            "INSERT INTO webhook_events (server_id, event, created_at) VALUES (?, 'x', ?)",
            (server_id, old),
        )
        db.commit()
    worker = Worker(db_file, concurrency=0)
    worker._heartbeat(force=True)
    assert worker_is_healthy(db_file)
    worker._heartbeat()
    worker._cleanup_history()
    with connect(db_file) as db:
        assert db.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"] == 0
        assert db.execute("SELECT COUNT(*) c FROM webhook_events").fetchone()["c"] == 0
    assert worker._claim_next_job(set()) is None
    assert worker_is_healthy(tmp_path / "missing" / "bad.db") is False
    worker._graceful_shutdown()


def test_worker_marks_unhandled_runner_error_failed(tmp_path):
    db_file = tmp_path / "media_server_manager.db"
    init_db(db_file)
    server_id = add_server(db_file, "A")
    queue = JobQueue(db_file)
    job_id = queue.create_job("notification_event", server_id, {})
    worker = Worker(db_file, concurrency=1)
    claimed = worker._claim_next_job(set())
    assert claimed == (job_id, server_id)
    worker.runner._run_job = lambda _job_id: (_ for _ in ()).throw(RuntimeError("boom"))
    worker._execute_job(job_id)
    assert queue.get_job(job_id)["status"] == "failed"
    assert "boom" in queue.list_logs(job_id)[-1]["message"]
    worker._graceful_shutdown()


def test_worker_run_stops_cleanly_and_signal_setup_skips_child_thread(tmp_path):
    db_file = tmp_path / "media_server_manager.db"
    init_db(db_file)
    worker = Worker(db_file, concurrency=1)
    worker.stop_event.set()
    worker.run()
    child = Worker(db_file, concurrency=1)
    thread = threading.Thread(target=child._install_signal_handlers)
    thread.start()
    thread.join()
    child._graceful_shutdown()
