from __future__ import annotations

import json
import os
import signal
import socket
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from media_server_manager_web.db import DB_FILE, connect, decode_payload, utcnow
from media_server_manager_web.rechecks import enqueue_due_rechecks
from media_server_manager_web.scheduling import next_run_at
from media_server_manager_web.tasks import TaskManager


def worker_is_healthy(db_file: Path | None = None, stale_seconds: int = 15) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)
    try:
        with connect(db_file or DB_FILE) as db:
            rows = db.execute("SELECT heartbeat_at FROM worker_heartbeats").fetchall()
        return any(
            datetime.fromisoformat(row["heartbeat_at"].replace("Z", "+00:00")) >= cutoff for row in rows
        )
    except Exception:
        return False


class Worker:
    def __init__(self, db_file: Path | None = None, concurrency: int = 2) -> None:
        self.db_file = Path(db_file or DB_FILE)
        self.concurrency = max(1, concurrency)
        self.worker_id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.runner = TaskManager(self.db_file)
        self.executor = ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="msm-job")
        self.stop_event = threading.Event()
        self.futures: dict[Future[None], tuple[int, int]] = {}
        self.started_at = utcnow()
        self._last_heartbeat = 0.0
        self._last_schedule_check = 0.0
        self._last_cleanup = 0.0

    def run(self) -> None:
        self._install_signal_handlers()
        self._recover_interrupted_jobs()
        self._heartbeat(force=True)
        try:
            while not self.stop_event.is_set():
                self.run_once()
                self.stop_event.wait(0.5)
        finally:
            self._graceful_shutdown()

    def run_once(self) -> None:
        self._reap_finished()
        self._heartbeat()
        now = time.monotonic()
        if now - self._last_schedule_check >= 1:
            self._last_schedule_check = now
            self._enqueue_due_schedules()
            enqueue_due_rechecks(self.db_file)
        if now - self._last_cleanup >= 3600:
            self._last_cleanup = now
            self._cleanup_history()
        while len(self.futures) < self.concurrency and not self.stop_event.is_set():
            active_servers = {server_id for _, server_id in self.futures.values()}
            claimed = self._claim_next_job(active_servers)
            if claimed is None:
                break
            job_id, server_id = claimed
            future = self.executor.submit(self._execute_job, job_id)
            self.futures[future] = (job_id, server_id)

    def _install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return

        def stop(_signum: int, _frame: Any) -> None:
            self.stop_event.set()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)

    def _recover_interrupted_jobs(self) -> None:
        with connect(self.db_file) as db:
            db.execute(
                """
                UPDATE jobs
                SET status = 'interrupted', finished_at = ?, error = 'Worker 重启，任务已中断'
                WHERE status = 'running'
                """,
                (utcnow(),),
            )
            db.execute("DELETE FROM worker_heartbeats")
            db.commit()

    def _heartbeat(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_heartbeat < 5:
            return
        self._last_heartbeat = now
        with connect(self.db_file) as db:
            db.execute(
                """
                INSERT INTO worker_heartbeats (
                    worker_id, started_at, heartbeat_at, pid, concurrency
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(worker_id) DO UPDATE SET
                    heartbeat_at = excluded.heartbeat_at,
                    pid = excluded.pid,
                    concurrency = excluded.concurrency
                """,
                (self.worker_id, self.started_at, utcnow(), os.getpid(), self.concurrency),
            )
            db.commit()

    def _claim_next_job(self, active_servers: set[int]) -> tuple[int, int] | None:
        with connect(self.db_file) as db:
            db.execute("BEGIN IMMEDIATE")
            clauses = [
                "jobs.status = 'queued'",
                "servers.enabled = 1",
                "NOT EXISTS (SELECT 1 FROM jobs active WHERE active.server_id=jobs.server_id AND active.status='running')",
            ]
            params: list[Any] = []
            if active_servers:
                placeholders = ",".join("?" for _ in active_servers)
                clauses.append(f"jobs.server_id NOT IN ({placeholders})")
                params.extend(sorted(active_servers))
            row = db.execute(
                f"""
                SELECT jobs.id, jobs.server_id
                FROM jobs JOIN servers ON servers.id = jobs.server_id
                WHERE {" AND ".join(clauses)}
                ORDER BY jobs.id ASC LIMIT 1
                """,
                params,
            ).fetchone()
            if row is None:
                db.commit()
                return None
            now = utcnow()
            try:
                cur = db.execute(
                    """
                    UPDATE jobs
                    SET status = 'running', worker_id = ?, claimed_at = ?, started_at = ?, stage = 'starting'
                    WHERE id = ? AND status = 'queued'
                    """,
                    (self.worker_id, now, now, row["id"]),
                )
                db.commit()
            except Exception:
                db.rollback()
                return None
            if cur.rowcount != 1:
                return None
            return int(row["id"]), int(row["server_id"])

    def _execute_job(self, job_id: int) -> None:
        try:
            self.runner._run_job(job_id)
        except Exception as exc:
            with connect(self.db_file) as db:
                db.execute(
                    """
                    UPDATE jobs SET status = 'failed', finished_at = ?, error = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (utcnow(), str(exc), job_id),
                )
                db.execute(
                    "INSERT INTO job_logs (job_id, message, created_at) VALUES (?, ?, ?)",
                    (job_id, f"Worker 执行失败：{exc}", utcnow()),
                )
                db.commit()

    def _reap_finished(self) -> None:
        for future in list(self.futures):
            if not future.done():
                continue
            future.result()
            self.futures.pop(future, None)

    def _enqueue_due_schedules(self) -> None:
        now = utcnow()
        with connect(self.db_file) as db:
            due_ids = [
                int(row["id"])
                for row in db.execute(
                    """
                    SELECT schedules.id FROM schedules
                    JOIN servers ON servers.id = schedules.server_id
                    WHERE schedules.enabled = 1 AND servers.enabled = 1
                      AND schedules.next_run_at IS NOT NULL AND schedules.next_run_at <= ?
                    ORDER BY schedules.next_run_at ASC
                    """,
                    (now,),
                ).fetchall()
            ]
        for schedule_id in due_ids:
            with connect(self.db_file) as db:
                db.execute("BEGIN IMMEDIATE")
                schedule = db.execute(
                    """
                    SELECT schedules.* FROM schedules
                    JOIN servers ON servers.id = schedules.server_id
                    WHERE schedules.id = ? AND schedules.enabled = 1 AND servers.enabled = 1
                      AND schedules.next_run_at <= ?
                    """,
                    (schedule_id, utcnow()),
                ).fetchone()
                if schedule is None:
                    db.commit()
                    continue
                schedule_payload = decode_payload(schedule["payload"])
                job_type = schedule_payload.get("type") or "all"
                payload = schedule_payload.get("payload") or {}
                cur = db.execute(
                    """
                    INSERT INTO jobs (type, server_id, status, payload, created_at)
                    VALUES (?, ?, 'queued', ?, ?)
                    """,
                    (
                        job_type,
                        schedule["server_id"],
                        json.dumps(payload, ensure_ascii=False),
                        utcnow(),
                    ),
                )
                job_id = int(cur.lastrowid or 0)
                db.execute(
                    "INSERT INTO job_logs (job_id, message, created_at) VALUES (?, ?, ?)",
                    (job_id, f"由定时任务「{schedule['name']}」创建。", utcnow()),
                )
                next_value = next_run_at(schedule["schedule_type"], schedule["schedule_value"])
                db.execute(
                    "UPDATE schedules SET last_run_at = ?, next_run_at = ? WHERE id = ?",
                    (utcnow(), next_value, schedule_id),
                )
                db.commit()

    def _cleanup_history(self) -> None:
        job_days = max(1, int(os.environ.get("MSM_JOB_RETENTION_DAYS", "90")))
        webhook_days = max(1, int(os.environ.get("MSM_WEBHOOK_RETENTION_DAYS", "30")))
        job_cutoff = (
            (datetime.now(timezone.utc) - timedelta(days=job_days))
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
        webhook_cutoff = (
            (datetime.now(timezone.utc) - timedelta(days=webhook_days))
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
        with connect(self.db_file) as db:
            db.execute(
                "DELETE FROM jobs WHERE status IN ('succeeded','failed','cancelled','interrupted') AND finished_at < ?",
                (job_cutoff,),
            )
            db.execute("DELETE FROM webhook_events WHERE created_at < ?", (webhook_cutoff,))
            db.execute("DELETE FROM webhook_rate_limits WHERE created_at < ?", (webhook_cutoff,))
            db.execute("DELETE FROM auth_attempts WHERE created_at < ?", (webhook_cutoff,))
            db.execute("DELETE FROM oauth_flows WHERE expires_at < ?", (utcnow(),))
            db.commit()

    def _graceful_shutdown(self) -> None:
        running_ids = [job_id for job_id, _ in self.futures.values()]
        if running_ids:
            placeholders = ",".join("?" for _ in running_ids)
            with connect(self.db_file) as db:
                db.execute(
                    f"UPDATE jobs SET cancel_requested_at = COALESCE(cancel_requested_at, ?) WHERE id IN ({placeholders})",
                    (utcnow(), *running_ids),
                )
                db.commit()
        deadline = time.monotonic() + 35
        while self.futures and time.monotonic() < deadline:
            self._reap_finished()
            time.sleep(0.2)
        if running_ids:
            placeholders = ",".join("?" for _ in running_ids)
            with connect(self.db_file) as db:
                db.execute(
                    f"""
                    UPDATE jobs SET status = 'interrupted', finished_at = ?, error = 'Worker 已停止'
                    WHERE id IN ({placeholders}) AND status IN ('running', 'cancelled')
                    """,
                    (utcnow(), *running_ids),
                )
                db.commit()
        with connect(self.db_file) as db:
            db.execute("DELETE FROM worker_heartbeats WHERE worker_id = ?", (self.worker_id,))
            db.commit()
        self.executor.shutdown(wait=False, cancel_futures=True)
