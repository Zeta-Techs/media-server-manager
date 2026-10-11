from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from ..models import Job, JobLog, MediaLibraries, MediaLibraryRecheckRequests, Server


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _after(value: str, seconds: int) -> str:
    return (datetime.fromisoformat(value.replace("Z", "+00:00")) + timedelta(seconds=seconds)).isoformat(timespec="microseconds").replace("+00:00", "Z")


class RecheckRepository:
    """Persistence and concurrency rules for automatic episode rechecks."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.libraries = MediaLibraries.__table__
        self.requests = MediaLibraryRecheckRequests.__table__
        self.jobs = Job.__table__
        self.logs = JobLog.__table__

    def request(self, server_id: int, event: str, metadata: dict[str, Any]) -> str:
        if event != "library.new":
            return "not_new_item"
        if metadata.get("type") != "episode":
            return "not_episode"
        try:
            library_id = int(metadata.get("librarySectionID") or 0)
        except (TypeError, ValueError):
            return "library_not_found"
        library = self.session.execute(select(self.libraries).where(self.libraries.c.server_id == server_id, self.libraries.c.library_id == library_id)).mappings().first()
        if library is None:
            return "library_not_found"
        if int(library["plex_type"] or 0) != 2 or not library["auto_recheck_new_episodes"]:
            return "auto_recheck_disabled"
        show_key = str(metadata.get("grandparentRatingKey") or "").strip()
        if not show_key:
            return "show_key_missing"
        now = utcnow()
        current = self.session.execute(select(self.requests).where(self.requests.c.server_id == server_id, self.requests.c.library_id == library_id, self.requests.c.rating_key == show_key)).mappings().first()
        if current and current["status"] in {"pending", "running", "retrying"}:
            self.session.execute(update(self.requests).where(self.requests.c.id == current["id"]).values(last_event_at=now, next_run_at=_after(now, 300), updated_at=now))
            return "auto_recheck_merged"
        if current and current["status"] == "succeeded" and str(current["last_event_at"]) >= _after(now, -300):
            self.session.execute(update(self.requests).where(self.requests.c.id == current["id"]).values(last_event_at=now, next_run_at=_after(now, 300), status="pending", updated_at=now))
            return "auto_recheck_merged"
        values = {
            "server_id": server_id, "library_id": library_id, "rating_key": show_key,
            "first_event_at": now, "last_event_at": now, "next_run_at": _after(now, 300),
            "attempt": 0, "status": "pending", "job_id": None, "last_error": "",
            "dispatched_event_at": "", "missing_count": None, "created_at": now, "updated_at": now,
        }
        if current:
            self.session.execute(update(self.requests).where(self.requests.c.id == current["id"]).values(**values))
        else:
            self.session.execute(insert(self.requests).values(**values))
        return "auto_recheck_queued"

    def finish(self, request: Any, job: Any, now: str) -> None:
        request = dict(request)
        job = dict(job)
        if job["status"] == "succeeded":
            status = "pending" if request["last_event_at"] > request["dispatched_event_at"] else "succeeded"
            self.session.execute(update(self.requests).where(self.requests.c.id == request["id"]).values(status=status, next_run_at=_after(request["last_event_at"], 300), attempt=0, last_error="", updated_at=now))
        elif job["status"] in {"failed", "interrupted"} and request["attempt"] == 0:
            self.session.execute(update(self.requests).where(self.requests.c.id == request["id"]).values(status="retrying", attempt=1, next_run_at=_after(now, 60), last_error=job.get("error") or "", updated_at=now))
            self.session.execute(insert(self.logs).values(job_id=job["id"], message="自动重检失败，60 秒后重试一次。", created_at=datetime.now(timezone.utc)))  # type: ignore[arg-type]
        else:
            self.session.execute(update(self.requests).where(self.requests.c.id == request["id"]).values(status="failed", last_error=job.get("error") or "任务已取消", updated_at=now))

    def enqueue_due(self) -> None:
        now = utcnow()
        running = self.session.execute(select(self.requests).where(self.requests.c.status == "running")).mappings().all()
        for request in running:
            job = self.session.execute(select(self.jobs).where(self.jobs.c.id == request["job_id"])).mappings().first()
            if job and job["status"] in {"succeeded", "failed", "interrupted", "cancelled"}:
                self.finish(request, job, now)
        due = self.session.execute(
            select(self.requests)
            .join(Server, Server.id == self.requests.c.server_id)
            .join(self.libraries, (self.libraries.c.server_id == self.requests.c.server_id) & (self.libraries.c.library_id == self.requests.c.library_id))
            .where(self.requests.c.status.in_(["pending", "retrying"]), self.requests.c.next_run_at <= now, Server.enabled.is_(True), self.libraries.c.plex_type == 2, self.libraries.c.auto_recheck_new_episodes == 1)
            .order_by(self.requests.c.next_run_at, self.requests.c.id)
        ).mappings().all()
        for request in due:
            busy = self.session.scalar(select(self.jobs.c.id).where(self.jobs.c.server_id == request["server_id"], self.jobs.c.status.in_(["queued", "running"]), self.jobs.c.type.in_(["media_library_refresh", "media_library_full_refresh", "media_library_tmdb_refresh", "media_library_show_recheck"])).limit(1))
            if busy:
                continue
            payload = {"server_id": request["server_id"], "library_id": request["library_id"], "rating_key": request["rating_key"], "auto_recheck_request_id": request["id"], "attempt": request["attempt"]}
            result = self.session.execute(insert(self.jobs).values(type="media_library_show_recheck", server_id=request["server_id"], status="queued", payload=json.dumps(payload, ensure_ascii=False), created_at=datetime.now(timezone.utc)))  # type: ignore[arg-type]
            job_id = int(result.inserted_primary_key[0])
            self.session.execute(update(self.requests).where(self.requests.c.id == request["id"]).values(status="running", job_id=job_id, dispatched_event_at=self.requests.c.last_event_at, updated_at=now))
            self.session.execute(insert(self.logs).values(job_id=job_id, message="由 Plex 新增集事件创建，执行指定电视剧的 Plex → TMDB → 缺集对比。", created_at=datetime.now(timezone.utc)))  # type: ignore[arg-type]

    def retry(self, source_job: dict[str, Any]) -> int:
        payload = source_job.get("payload") or {}
        request = self.session.execute(select(self.requests).where(self.requests.c.id == payload.get("auto_recheck_request_id"))).mappings().first()
        if request is None:
            raise ValueError("自动重检请求不存在")
        if request["job_id"] != source_job["id"] or request["status"] in {"pending", "retrying"}:
            raise ValueError("该剧集已有更新的自动重检请求")
        active = self.session.scalar(select(self.jobs.c.id).where(self.jobs.c.id == request["job_id"], self.jobs.c.status.in_(["queued", "running"])))
        if active:
            raise ValueError("该剧集已有重检任务正在执行")
        now = utcnow()
        payload = {**payload, "attempt": 1}
        result = self.session.execute(insert(self.jobs).values(type="media_library_show_recheck", server_id=source_job["server_id"], status="queued", payload=json.dumps(payload, ensure_ascii=False), retry_of=source_job["id"], created_at=datetime.now(timezone.utc)))  # type: ignore[arg-type]
        job_id = int(result.inserted_primary_key[0])
        self.session.execute(update(self.requests).where(self.requests.c.id == request["id"]).values(status="running", attempt=1, job_id=job_id, dispatched_event_at=self.requests.c.last_event_at, last_error="", updated_at=now))
        self.session.execute(insert(self.logs).values(job_id=job_id, message="手动重试自动重检任务。", created_at=datetime.now(timezone.utc)))  # type: ignore[arg-type]
        return job_id

    def set_missing_count(self, request_id: int, job_id: int, count: int) -> None:
        self.session.execute(
            update(self.requests)
            .where(self.requests.c.id == request_id, self.requests.c.job_id == job_id)
            .values(missing_count=count)
        )
