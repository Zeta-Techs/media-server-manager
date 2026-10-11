from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Job, NotificationChannels, Schedule, Server, WebhookEvents
from .jobs import JobRepository


class OverviewRepository:
    """Read-only aggregate queries used by the dashboard and health UI."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def snapshot(self, limit: int = 5) -> dict[str, Any]:
        counts = JobRepository(self.session).status_counts()
        recent = JobRepository(self.session).list_jobs(limit)
        failed = list(
            self.session.execute(
                select(Job, Server.name)
                .join(Server, Server.id == Job.server_id)
                .where(Job.status == "failed")
                .order_by(Job.id.desc())
                .limit(limit)
            )
        )
        return {
            "server_count": int(self.session.scalar(select(func.count(Server.id))) or 0),
            "enabled_server_count": int(
                self.session.scalar(select(func.count(Server.id)).where(Server.enabled.is_(True))) or 0
            ),
            "running_jobs": counts.get("running", 0),
            "queued_jobs": counts.get("queued", 0),
            "recent_jobs": recent,
            "failed_jobs": [(row[0], row[1]) for row in failed],
            "webhook_events": int(self.session.scalar(select(func.count(WebhookEvents.__table__.c.id))) or 0),
            "active_schedules": int(
                self.session.scalar(select(func.count(Schedule.id)).where(Schedule.enabled.is_(True))) or 0
            ),
            "notification_channels": int(
                self.session.scalar(
                    select(func.count(NotificationChannels.__table__.c.id)).where(
                        NotificationChannels.__table__.c.enabled == 1
                    )
                )
                or 0
            ),
        }
