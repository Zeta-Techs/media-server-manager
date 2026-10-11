from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import desc, func, insert, select, update
from sqlalchemy.orm import Session

from ..models import (
    Changes,
    ChangeSets,
    ContinueWatchingItems,
    ContinueWatchingRuns,
    Job,
    JobLog,
    Schedule,
    Server,
)


class JobRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, job_type: str, server_id: int, payload: dict, retry_of: int | None = None) -> Job:
        server = self.session.scalar(select(Server).where(Server.id == server_id, Server.enabled.is_(True)))
        if server is None:
            raise ValueError("服务器不存在或已禁用")
        job = Job(
            type=job_type,
            server_id=server_id,
            status="queued",
            payload=json.dumps(payload, ensure_ascii=False),
            retry_of=retry_of,
            created_at=datetime.now(timezone.utc),
        )
        self.session.add(job)
        self.session.flush()
        self.add_log(job.id, "任务已加入队列。")
        return job

    def add_log(self, job_id: int, message: str) -> JobLog:
        log = JobLog(job_id=job_id, message=message, created_at=datetime.now(timezone.utc))
        self.session.add(log)
        self.session.flush()
        return log

    def get(self, job_id: int) -> Job | None:
        return self.session.get(Job, job_id)

    def list_jobs(self, limit: int) -> list[tuple[Job, str]]:
        rows = self.session.execute(
            select(Job, Server.name).outerjoin(Server, Server.id == Job.server_id).order_by(desc(Job.id)).limit(limit)
        )
        return [(row[0], row[1]) for row in rows]

    def logs(self, job_id: int, after_id: int, limit: int) -> list[JobLog]:
        query = select(JobLog).where(JobLog.job_id == job_id)
        if after_id:
            query = query.where(JobLog.id > after_id).order_by(JobLog.id).limit(limit)
        else:
            query = query.order_by(desc(JobLog.id)).limit(limit)
        logs = list(self.session.scalars(query))
        return list(reversed(logs)) if not after_id else logs

    def status_counts(self, server_id: int | None = None) -> dict[str, int]:
        """Return queue counts through ORM queries for diagnostics and health checks."""
        query = select(Job.status, func.count(Job.id)).group_by(Job.status)
        if server_id is not None:
            query = query.where(Job.server_id == server_id)
        return {str(status): int(count) for status, count in self.session.execute(query)}

    def enabled_schedule_count(self, server_id: int) -> int:
        return int(
            self.session.scalar(
                select(func.count(Schedule.id)).where(
                    Schedule.server_id == server_id,
                    Schedule.enabled.is_(True),
                )
            )
            or 0
        )

    def changes(self, job_id: int, limit: int) -> list[dict[str, object]]:
        rows = self.session.execute(
            select(Changes.__table__).where(Changes.__table__.c.job_id == job_id).order_by(Changes.__table__.c.id).limit(limit)
        )
        return [dict(row._mapping) for row in rows]

    def latest_change_set(self, job_id: int) -> int | None:
        return self.session.scalar(
            select(ChangeSets.__table__.c.id)
            .where(ChangeSets.__table__.c.job_id == job_id)
            .order_by(ChangeSets.__table__.c.id.desc())
            .limit(1)
        )

    def preview_run(self, job_id: int) -> int | None:
        return self.session.scalar(
            select(ContinueWatchingRuns.__table__.c.id)
            .where(
                ContinueWatchingRuns.__table__.c.job_id == job_id,
                ContinueWatchingRuns.__table__.c.mode == "preview",
                ContinueWatchingRuns.__table__.c.status == "succeeded",
            )
            .order_by(ContinueWatchingRuns.__table__.c.id.desc())
            .limit(1)
        )

    def candidate_item_ids(self, run_id: int) -> list[int]:
        return [
            int(value)
            for value in self.session.scalars(
                select(ContinueWatchingItems.__table__.c.id).where(
                    ContinueWatchingItems.__table__.c.run_id == run_id,
                    ContinueWatchingItems.__table__.c.status == "candidate",
                )
            )
        ]

    def cancel_requested(self, job_id: int) -> bool:
        job = self.session.get(Job, job_id)
        return bool(job and (job.cancel_requested_at is not None or job.status == "cancelled"))

    def mark_started(self, job_id: int, *, now: datetime) -> None:
        job = self.session.get(Job, job_id)
        if job is None:
            raise ValueError("任务不存在")
        job.status = "running"
        job.started_at = job.started_at or now
        job.claimed_at = job.claimed_at or now
        job.stage = "starting"

    def mark_finished(self, job_id: int, status: str, *, now: datetime, error: str = "") -> None:
        job = self.session.get(Job, job_id)
        if job is None:
            raise ValueError("任务不存在")
        job.status = status
        job.finished_at = now
        if error:
            job.error = error

    def update_progress(self, job_id: int, fields: dict[str, Any], increments: dict[str, int]) -> None:
        job = self.session.get(Job, job_id)
        if job is None:
            return
        for name, value in fields.items():
            setattr(job, name, value)
        for name, value in increments.items():
            setattr(job, name, int(getattr(job, name) or 0) + int(value))

    def add_logs(self, messages: Iterable[tuple[int, str, str | datetime]]) -> None:
        for job_id, message, created_at in messages:
            self.session.add(JobLog(job_id=job_id, message=message, created_at=created_at))

    def create_change_set(
        self,
        job_id: int,
        server_id: int,
        mode: str,
        scope: str,
        created_at: str,
        source_change_set_id: int | None = None,
    ) -> int:
        table = ChangeSets.__table__
        result = self.session.execute(
            insert(table).values(
                job_id=job_id,
                server_id=server_id,
                mode=mode,
                scope=scope,
                source_change_set_id=source_change_set_id,
                created_at=created_at,
            )
        )
        return int(result.inserted_primary_key[0])

    def change_set(self, change_set_id: int, mode: str | None = None) -> dict[str, Any] | None:
        table = ChangeSets.__table__
        query = select(table).where(table.c.id == change_set_id)
        if mode is not None:
            query = query.where(table.c.mode == mode)
        row = self.session.execute(query).mappings().first()
        return dict(row) if row else None

    def changes_for_set(self, change_set_id: int) -> list[dict[str, Any]]:
        table = Changes.__table__
        return [dict(row) for row in self.session.execute(select(table).where(table.c.change_set_id == change_set_id).order_by(table.c.id)).mappings()]

    def changes_for_rollback(self, job_id: int) -> list[dict[str, Any]]:
        table = Changes.__table__
        rows = self.session.execute(
            select(table).where(
                table.c.job_id == job_id,
                table.c.applied == 1,
                table.c.rollback_status == "",
            ).order_by(table.c.id)
        )
        return [dict(row) for row in rows.mappings()]

    def add_changes(self, rows: Iterable[dict[str, Any]]) -> None:
        table = Changes.__table__
        values = list(rows)
        if values:
            self.session.execute(insert(table), values)

    def update_change(self, change_id: int, values: dict[str, Any]) -> None:
        self.session.execute(update(Changes.__table__).where(Changes.__table__.c.id == change_id).values(**values))
