from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from media_server_manager.config import settings
from media_server_manager.domain.jobs import JOB_TYPES, TERMINAL_JOB_STATUSES  # noqa: F401
from media_server_manager.infrastructure.db.models import Server
from media_server_manager.infrastructure.db.repositories.jobs import JobRepository
from media_server_manager.infrastructure.db.repositories.worker import WorkerRepository
from media_server_manager.infrastructure.db.runtime import decode_payload
from media_server_manager.infrastructure.db.session import Database

# Deprecated test/extension compatibility alias.  New callers pass an
# explicit path or rely on Settings.database_path below.
DB_FILE = settings.database_path

class JobQueue:
    def __init__(self, db_file: Path | None = None, database: Database | None = None) -> None:
        self.db_file = Path(db_file or settings.database_path)
        self.database = database or Database.for_path(self.db_file)

    def shutdown(self) -> None:
        if self.database is not None:
            self.database.engine.dispose()

    def create_job(
        self,
        job_type: str,
        server_id: int,
        payload: Optional[Dict[str, Any]] = None,
        retry_of: int | None = None,
    ) -> int:
        if job_type not in JOB_TYPES:
            raise ValueError("不支持的任务类型")
        payload = payload or {}
        with self.database.transaction() as uow:
            job = JobRepository(uow.session).create(job_type, server_id, payload, retry_of)
            return int(job.id)

    @staticmethod
    def _job_dict(job, server_name: str | None) -> Dict[str, Any]:
        data = {column.name: getattr(job, column.name) for column in job.__table__.columns}
        for key, value in list(data.items()):
            if isinstance(value, datetime):
                data[key] = value.isoformat(timespec="seconds").replace("+00:00", "Z")
        data["server_name"] = server_name
        data["payload"] = decode_payload(data.get("payload"))
        return data

    def get_job(self, job_id: int) -> Dict[str, Any]:
        with self.database.session() as session:
            row = JobRepository(session).get(job_id)
            if row is None:
                raise ValueError("任务不存在")
            server_name = session.scalar(select(Server.name).where(Server.id == row.server_id))
            return self._job_dict(row, server_name)

    def list_jobs(self, limit: int = 50) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self.database.session() as session:
            return [self._job_dict(job, name) for job, name in JobRepository(session).list_jobs(limit)]

    def list_logs(self, job_id: int, after_id: int = 0, limit: int = 300) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit or 300), 2000))
        with self.database.session() as session:
            return [
                {
                    "id": log.id,
                    "job_id": log.job_id,
                    "message": log.message,
                    "created_at": log.created_at.isoformat(timespec="seconds").replace("+00:00", "Z")
                    if isinstance(log.created_at, datetime)
                    else log.created_at,
                }
                for log in JobRepository(session).logs(job_id, after_id, limit)
            ]

    def cancel_job(self, job_id: int) -> str:
        with self.database.transaction() as uow:
            job = JobRepository(uow.session).get(job_id)
            if job is None:
                raise ValueError("任务不存在")
            if job.status == "queued":
                job.status = "cancelled"
                job.finished_at = datetime.now(timezone.utc)
                job.error = "用户取消"
                status = "cancelled"
            elif job.status == "running":
                job.cancel_requested_at = job.cancel_requested_at or datetime.now(timezone.utc)
                status = "cancelling"
            else:
                status = job.status
            return status

    def cancel_queued_job(self, job_id: int) -> bool:
        return self.cancel_job(job_id) in {"cancelled", "cancelling"}

    def retry_job(self, source_job_id: int) -> int:
        source = self.get_job(source_job_id)
        if source["status"] not in {"failed", "cancelled", "interrupted"}:
            raise ValueError("只有失败、取消或中断的任务可以重试")
        if source["type"] == "media_library_show_recheck":
            from media_server_manager.application.media_operations.rechecks import retry_recheck

            return retry_recheck(self.db_file, source)
        return self.create_job(
            source["type"], int(source["server_id"]), source.get("payload") or {}, source_job_id
        )

    def rollback_job(self, source_job_id: int) -> int:
        source = self.get_job(source_job_id)
        return self.create_job("rollback", int(source["server_id"]), {"source_job_id": source_job_id})

    def apply_preview_job(self, source_job_id: int) -> int:
        source = self.get_job(source_job_id)
        payload = source.get("payload") or {}
        if source.get("status") != "succeeded":
            raise ValueError("预览任务成功结束后才能执行")
        if source.get("type") == "localize" and payload.get("mode") == "dry_run":
            with self.database.session() as session:
                change_set_id = JobRepository(session).latest_change_set(source_job_id)
            if change_set_id is None:
                raise ValueError("预览任务没有可执行的变更快照")
            return self.create_job(
                "apply_change_set",
                int(source["server_id"]),
                {"source_job_id": source_job_id, "source_change_set_id": int(change_set_id)},
            )
        if source.get("type") == "continue_watching_preview":
            with self.database.session() as session:
                repository = JobRepository(session)
                run_id = repository.preview_run(source_job_id)
                if run_id is None:
                    raise ValueError("未找到该预览任务的继续观看候选")
                item_ids = repository.candidate_item_ids(run_id)
            if not item_ids:
                raise ValueError("该预览任务没有可执行的候选剧集")
            return self.create_job(
                "continue_watching_apply",
                int(source["server_id"]),
                {"item_ids": item_ids, "source_preview_job_id": source_job_id},
            )
        raise ValueError("只有本地化预览任务或继续观看预览任务可以直接执行")

    def worker_status(self, stale_seconds: int = 15) -> Dict[str, Any]:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)
        with self.database.session() as session:
            workers = [
                {
                    "worker_id": heartbeat.worker_id,
                    "started_at": heartbeat.started_at.isoformat() if isinstance(heartbeat.started_at, datetime) else heartbeat.started_at,
                    "heartbeat_at": heartbeat.heartbeat_at.isoformat()
                    if isinstance(heartbeat.heartbeat_at, datetime)
                    else heartbeat.heartbeat_at,
                    "pid": heartbeat.pid,
                    "concurrency": heartbeat.concurrency,
                }
                for heartbeat in WorkerRepository(session).heartbeats()
            ]
        def parse_heartbeat(value: str) -> datetime:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)

        online = any(parse_heartbeat(row["heartbeat_at"]) >= cutoff for row in workers)
        return {"online": online, "workers": workers}


