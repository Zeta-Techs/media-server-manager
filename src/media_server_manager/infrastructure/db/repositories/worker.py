from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, aliased

from ..models import (
    AuthAttempt,
    Job,
    JobLog,
    OauthFlows,
    Schedule,
    Server,
    WebhookEvents,
    WebhookRateLimits,
    WorkerHeartbeat,
)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


class WorkerRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def recover_running_jobs(self) -> None:
        now = datetime.now(timezone.utc)
        self.session.execute(
            update(Job)
            .where(Job.status == "running")
            .values(status="interrupted", finished_at=now, error="Worker 重启，任务已中断")
        )
        self.session.execute(delete(WorkerHeartbeat))

    def heartbeat(self, worker_id: str, started_at: datetime, pid: int, concurrency: int) -> None:
        now = datetime.now(timezone.utc)
        heartbeat = self.session.get(WorkerHeartbeat, worker_id)
        if heartbeat is None:
            self.session.add(
                WorkerHeartbeat(
                    worker_id=worker_id,
                    started_at=started_at,
                    heartbeat_at=now,
                    pid=pid,
                    concurrency=concurrency,
                )
            )
        else:
            heartbeat.heartbeat_at = now
            heartbeat.pid = pid
            heartbeat.concurrency = concurrency

    def heartbeats(self) -> list[WorkerHeartbeat]:
        return list(self.session.scalars(select(WorkerHeartbeat).order_by(WorkerHeartbeat.heartbeat_at.desc())))

    def claim_next(self, worker_id: str, active_servers: set[int]) -> tuple[int, int] | None:
        self.session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        active = aliased(Job)
        query = (
            select(Job)
            .join(Server, Server.id == Job.server_id)
            .where(
                Job.status == "queued",
                Server.enabled.is_(True),
                ~select(active.id)
                .where(active.server_id == Job.server_id, active.status == "running")
                .exists(),
            )
            .order_by(Job.id)
        )
        if active_servers:
            query = query.where(~Job.server_id.in_(active_servers))
        job = self.session.scalar(query.limit(1))
        if job is None:
            return None
        now = datetime.now(timezone.utc)
        job.status = "running"
        job.worker_id = worker_id
        job.claimed_at = now
        job.started_at = now
        job.stage = "starting"
        self.session.flush()
        return int(job.id), int(job.server_id)

    def due_schedule_ids(self, now: str) -> list[int]:
        query = (
            select(Schedule.id)
            .join(Server, Server.id == Schedule.server_id)
            .where(
                Schedule.enabled.is_(True),
                Server.enabled.is_(True),
                Schedule.next_run_at.is_not(None),
                Schedule.next_run_at <= now,
            )
            .order_by(Schedule.next_run_at)
        )
        return [int(value) for value in self.session.scalars(query)]

    def schedule(self, schedule_id: int) -> Schedule | None:
        return self.session.get(Schedule, schedule_id)

    def dispatch_schedule(self, schedule_id: int, now: str, next_run: str | None) -> int | None:
        """Claim one due schedule and create its job in the same transaction."""
        self.session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        schedule = self.session.scalar(
            select(Schedule)
            .join(Server, Server.id == Schedule.server_id)
            .where(
                Schedule.id == schedule_id,
                Schedule.enabled.is_(True),
                Server.enabled.is_(True),
                Schedule.next_run_at.is_not(None),
                Schedule.next_run_at <= now,
            )
        )
        if schedule is None:
            return None
        try:
            payload = json.loads(schedule.payload or "{}")
        except (TypeError, ValueError):
            payload = {}
        job_payload = payload.get("payload") or {}
        job = Job(
            type=str(payload.get("type") or "all"),
            server_id=int(schedule.server_id),
            status="queued",
            payload=json.dumps(job_payload, ensure_ascii=False),
            created_at=datetime.now(timezone.utc),
        )
        self.session.add(job)
        self.session.flush()
        self.session.add(
            JobLog(
                job_id=job.id,
                message=f"由定时任务「{schedule.name}」创建。",
                created_at=datetime.now(timezone.utc),
            )
        )
        schedule.last_run_at = now
        schedule.next_run_at = next_run
        self.session.flush()
        return int(job.id)

    def cleanup_history(self, job_cutoff: str, webhook_cutoff: str, now: str) -> None:
        self.session.execute(
            delete(Job).where(
                Job.status.in_(["succeeded", "failed", "cancelled", "interrupted"]),
                Job.finished_at.is_not(None),
                Job.finished_at < job_cutoff,
            )
        )
        self.session.execute(delete(WebhookEvents).where(WebhookEvents.__table__.c.created_at < webhook_cutoff))
        self.session.execute(delete(WebhookRateLimits).where(WebhookRateLimits.__table__.c.created_at < webhook_cutoff))
        self.session.execute(delete(AuthAttempt).where(AuthAttempt.created_at < webhook_cutoff))
        self.session.execute(delete(OauthFlows).where(OauthFlows.__table__.c.expires_at < now))

    def request_cancel(self, job_ids: list[int], now: str) -> None:
        if not job_ids:
            return
        self.session.execute(
            update(Job)
            .where(Job.id.in_(job_ids), Job.cancel_requested_at.is_(None))
            .values(cancel_requested_at=_parse_datetime(now))
        )

    def fail(self, job_id: int, error: str) -> None:
        job = self.session.get(Job, job_id)
        if job is None or job.status != "running":
            return
        job.status = "failed"
        job.finished_at = datetime.now(timezone.utc)
        job.error = error
        self.session.add(JobLog(job_id=job_id, message=f"Worker 执行失败：{error}", created_at=datetime.now(timezone.utc)))

    def interrupt(self, job_ids: list[int]) -> None:
        if not job_ids:
            return
        now = datetime.now(timezone.utc)
        for job in self.session.scalars(select(Job).where(Job.id.in_(job_ids), Job.status.in_(["running", "cancelled"]))):
            job.status = "interrupted"
            job.finished_at = now
            job.error = "Worker 已停止"

    def remove_heartbeat(self, worker_id: str) -> None:
        heartbeat = self.session.get(WorkerHeartbeat, worker_id)
        if heartbeat is not None:
            self.session.delete(heartbeat)
