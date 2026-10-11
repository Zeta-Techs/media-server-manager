from __future__ import annotations

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

from sqlalchemy import select

from media_server_manager.application.job_orchestration.scheduling import next_run_at
from media_server_manager.application.job_orchestration.service import JobExecutionService
from media_server_manager.application.media_operations.rechecks import enqueue_due_rechecks
from media_server_manager.config import settings
from media_server_manager.infrastructure.db.models import WorkerHeartbeat
from media_server_manager.infrastructure.db.repositories.jobs import JobRepository
from media_server_manager.infrastructure.db.repositories.worker import WorkerRepository
from media_server_manager.infrastructure.db.runtime import decode_payload, utcnow
from media_server_manager.infrastructure.db.session import Database

from .contracts import JobContext
from .handlers import ApplicationTaskHandler, handler_registry


def worker_is_healthy(db_file: Path | None = None, stale_seconds: int = 15) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)
    database = Database.for_path(db_file or settings.database_path)
    try:
        with database.session() as session:
            rows = session.scalars(select(WorkerHeartbeat.heartbeat_at)).all()
        def parse_heartbeat(value: str) -> datetime:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed

        return any(parse_heartbeat(str(value)) >= cutoff for value in rows)
    except Exception:
        return False
    finally:
        database.engine.dispose()


class Worker:
    def __init__(self, db_file: Path | None = None, concurrency: int | None = None) -> None:
        self.db_file = Path(db_file or settings.database_path)
        self.database = Database.for_path(self.db_file)
        self.concurrency = max(1, concurrency or settings.worker_concurrency)
        self.worker_id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.runner = JobExecutionService(self.db_file, database=self.database)
        self.handlers = handler_registry(self.runner, self.db_file)
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
        with self.database.transaction() as uow:
            WorkerRepository(uow.session).recover_running_jobs()

    def _heartbeat(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_heartbeat < 5:
            return
        self._last_heartbeat = now
        with self.database.transaction() as uow:
            WorkerRepository(uow.session).heartbeat(
                self.worker_id,
                datetime.fromisoformat(self.started_at.replace("Z", "+00:00")),
                os.getpid(),
                self.concurrency,
            )

    def _claim_next_job(self, active_servers: set[int]) -> tuple[int, int] | None:
        try:
            with self.database.transaction() as uow:
                return WorkerRepository(uow.session).claim_next(self.worker_id, active_servers)
        except Exception:
            return None

    def _execute_job(self, job_id: int) -> None:
        try:
            with self.database.session() as session:
                job = JobRepository(session).get(job_id)
                if job is None:
                    raise RuntimeError(f"任务不存在：{job_id}")
                server_id = int(job.server_id)
                job_type = str(job.type)
                payload = decode_payload(job.payload)
            handler = self.handlers.get(job_type) or ApplicationTaskHandler(self.runner, job_type)
            handler.run(JobContext(job_id, server_id, self.database, payload))
        except Exception as exc:
            with self.database.transaction() as uow:
                WorkerRepository(uow.session).fail(job_id, str(exc))

    def _reap_finished(self) -> None:
        for future in list(self.futures):
            if not future.done():
                continue
            future.result()
            self.futures.pop(future, None)

    def _enqueue_due_schedules(self) -> None:
        now = utcnow()
        with self.database.session() as session:
            due_ids = WorkerRepository(session).due_schedule_ids(now)
        for schedule_id in due_ids:
            with self.database.session() as session:
                schedule = WorkerRepository(session).schedule(schedule_id)
                if schedule is None:
                    continue
                next_value = next_run_at(schedule.schedule_type, schedule.schedule_value)
            with self.database.transaction() as uow:
                repo = WorkerRepository(uow.session)
                # The repository re-checks the schedule inside an IMMEDIATE
                # write transaction, preventing duplicate dispatch by workers.
                repo.dispatch_schedule(schedule_id, utcnow(), next_value)

    def _cleanup_history(self) -> None:
        job_days = settings.job_retention_days
        webhook_days = settings.webhook_retention_days
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
        with self.database.transaction() as uow:
            WorkerRepository(uow.session).cleanup_history(job_cutoff, webhook_cutoff, utcnow())

    def _graceful_shutdown(self) -> None:
        running_ids = [job_id for job_id, _ in self.futures.values()]
        if running_ids:
            with self.database.transaction() as uow:
                WorkerRepository(uow.session).request_cancel(running_ids, utcnow())
        deadline = time.monotonic() + 35
        while self.futures and time.monotonic() < deadline:
            self._reap_finished()
            time.sleep(0.2)
        if running_ids:
            with self.database.transaction() as uow:
                WorkerRepository(uow.session).interrupt(running_ids)
        with self.database.transaction() as uow:
            WorkerRepository(uow.session).remove_heartbeat(self.worker_id)
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.runner.shutdown()
        self.database.engine.dispose()


