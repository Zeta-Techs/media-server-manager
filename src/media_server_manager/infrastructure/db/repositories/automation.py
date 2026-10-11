from __future__ import annotations

import json
from typing import Any

from sqlalchemy import delete, func, insert, or_, select, update
from sqlalchemy.orm import Session

from ..models import (
    CollectionRuleRuns,
    CollectionRules,
    ContinueWatchingItems,
    ContinueWatchingRuns,
    EpisodeAuditIgnores,
    EpisodeAuditItems,
    EpisodeAuditRuns,
    EpisodeMatchOverrides,
    Job,
    NotificationChannels,
    Schedule,
    Server,
    TagMappings,
    TagSuggestions,
    WebhookEvents,
    WebhookRateLimits,
    WebhookRules,
)


class AutomationRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_schedules(self) -> list[dict[str, Any]]:
        rows = self.session.execute(
            select(Schedule, Server.name)
            .join(Server, Server.id == Schedule.server_id)
            .order_by(Schedule.id.desc())
        )
        result: list[dict[str, Any]] = []
        for schedule, server_name in rows:
            data = {column.name: getattr(schedule, column.name) for column in Schedule.__table__.columns}
            data["server_name"] = server_name
            try:
                data["payload"] = json.loads(str(data.get("payload") or "{}"))
            except json.JSONDecodeError:
                data["payload"] = {}
            result.append(data)
        return result

    def save_schedule(
        self,
        *,
        schedule_id: int | None,
        server_id: int,
        name: str,
        schedule_type: str,
        schedule_value: str,
        payload: dict[str, Any],
        enabled: bool,
        next_run_at: str | None,
        now: str,
    ) -> Schedule:
        schedule = self.session.get(Schedule, schedule_id) if schedule_id else None
        if schedule is None:
            schedule = Schedule(
                server_id=server_id,
                name=name,
                schedule_type=schedule_type,
                schedule_value=schedule_value,
                payload=json.dumps(payload, ensure_ascii=False),
                enabled=enabled,
                next_run_at=next_run_at,
                created_at=now,
                updated_at=now,
            )
            self.session.add(schedule)
        else:
            schedule.server_id = server_id
            schedule.name = name
            schedule.schedule_type = schedule_type
            schedule.schedule_value = schedule_value
            schedule.payload = json.dumps(payload, ensure_ascii=False)
            schedule.enabled = enabled
            schedule.next_run_at = next_run_at
            schedule.updated_at = now
        self.session.flush()
        return schedule

    def delete_schedule(self, schedule_id: int) -> bool:
        schedule = self.session.get(Schedule, schedule_id)
        if schedule is None:
            return False
        self.session.delete(schedule)
        self.session.flush()
        return True

    def tags(self) -> dict[str, str]:
        rows = self.session.execute(
            select(TagMappings.__table__.c.source, TagMappings.__table__.c.target).order_by(
                TagMappings.__table__.c.source
            )
        )
        return {str(source): str(target) for source, target in rows}

    def replace_tags(self, tags: dict[str, str]) -> None:
        self.session.execute(TagMappings.__table__.delete())
        self.session.execute(
            TagMappings.__table__.insert(),
            [{"source": source, "target": target} for source, target in sorted(tags.items())],
        )

    def tag_suggestions(self, server_id: int | None = None) -> list[dict[str, Any]]:
        query = select(TagSuggestions.__table__).order_by(
            TagSuggestions.__table__.c.count.desc(), TagSuggestions.__table__.c.source
        )
        if server_id is not None:
            query = query.where(TagSuggestions.__table__.c.server_id == server_id)
        return [dict(row._mapping) for row in self.session.execute(query)]

    def continue_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        runs, jobs = ContinueWatchingRuns.__table__, Job.__table__
        servers = Server.__table__
        query = select(runs, servers.c.name.label("server_name"), jobs.c.status.label("job_status")).join(
            servers, servers.c.id == runs.c.server_id
        ).outerjoin(jobs, jobs.c.id == runs.c.job_id).order_by(runs.c.id.desc()).limit(limit)
        return [dict(row._mapping) for row in self.session.execute(query)]

    def continue_items(self, run_id: int) -> list[dict[str, Any]]:
        table = ContinueWatchingItems.__table__
        return [dict(row._mapping) for row in self.session.execute(select(table).where(table.c.run_id == run_id).order_by(table.c.show_title, table.c.season, table.c.episode))]

    def create_continue_run(self, job_id: int, server_id: int, library_id: int, mode: str, now: str, candidate_count: int = 0) -> int:
        result = self.session.execute(
            insert(ContinueWatchingRuns.__table__).values(
                job_id=job_id,
                server_id=server_id,
                library_id=library_id,
                mode=mode,
                status="running",
                candidate_count=candidate_count,
                created_at=now,
            )
        )
        return int(result.inserted_primary_key[0])

    def add_continue_items(self, values: list[dict[str, Any]]) -> list[int]:
        table = ContinueWatchingItems.__table__
        ids: list[int] = []
        for value in values:
            result = self.session.execute(insert(table).values(**value))
            ids.append(int(result.inserted_primary_key[0]))
        return ids

    def finish_continue_run(self, run_id: int, *, status: str, now: str, candidate_count: int | None = None, applied_count: int | None = None, error_count: int | None = None) -> None:
        values: dict[str, Any] = {"status": status, "finished_at": now}
        if candidate_count is not None:
            values["candidate_count"] = candidate_count
        if applied_count is not None:
            values["applied_count"] = applied_count
        if error_count is not None:
            values["error_count"] = error_count
        self.session.execute(update(ContinueWatchingRuns.__table__).where(ContinueWatchingRuns.__table__.c.id == run_id).values(**values))

    def continue_candidates(self, item_ids: list[int], server_id: int) -> list[dict[str, Any]]:
        table = ContinueWatchingItems.__table__
        rows = self.session.execute(
            select(table).where(
                table.c.id.in_(item_ids),
                table.c.server_id == server_id,
                table.c.status == "candidate",
            ).order_by(table.c.id)
        ).mappings()
        return [dict(row) for row in rows]

    def update_continue_item(self, item_id: int, values: dict[str, Any]) -> None:
        self.session.execute(update(ContinueWatchingItems.__table__).where(ContinueWatchingItems.__table__.c.id == item_id).values(**values))

    def update_continue_run(self, run_id: int, values: dict[str, Any]) -> None:
        self.session.execute(update(ContinueWatchingRuns.__table__).where(ContinueWatchingRuns.__table__.c.id == run_id).values(**values))

    def candidate_servers(self, item_ids: list[int]) -> list[dict[str, Any]]:
        table = ContinueWatchingItems.__table__
        return [dict(row._mapping) for row in self.session.execute(select(table.c.server_id, table.c.library_id).where(table.c.id.in_(item_ids), table.c.status == "candidate").distinct())]

    def list_webhook_rules(self) -> list[dict[str, Any]]:
        table = WebhookRules.__table__
        return [dict(row._mapping) for row in self.session.execute(select(table).order_by(table.c.id.desc()))]

    def save_webhook_rule(self, values: dict[str, Any], now: str) -> None:
        table = WebhookRules.__table__
        rule_id = values.get("id")
        payload = {"server_id": int(values["server_id"]), "event": values.get("event") or "library.new", "action": values.get("action") or "localize", "enabled": 1 if values.get("enabled", True) else 0, "updated_at": now}
        if rule_id:
            self.session.execute(update(table).where(table.c.id == int(rule_id)).values(**payload))
        else:
            self.session.execute(table.insert().values(**payload, created_at=now))

    def delete_webhook_rule(self, rule_id: int) -> None:
        self.session.execute(delete(WebhookRules.__table__).where(WebhookRules.__table__.c.id == rule_id))

    def list_notification_channels(self) -> list[dict[str, Any]]:
        table = NotificationChannels.__table__
        return [dict(row._mapping) for row in self.session.execute(select(table).order_by(table.c.id.desc()))]

    def notification_channel(self, channel_id: int) -> dict[str, Any] | None:
        row = self.session.execute(select(NotificationChannels.__table__).where(NotificationChannels.__table__.c.id == channel_id)).mappings().first()
        return dict(row) if row else None

    def save_notification_channel(self, values: dict[str, Any], now: str, events: str) -> None:
        table = NotificationChannels.__table__
        channel_id = values.get("id")
        payload = {"name": values.get("name") or "Webhook", "channel_type": values.get("channel_type") or "webhook", "url": values.get("url") or "", "events": events, "enabled": 1 if values.get("enabled", True) else 0, "updated_at": now}
        if channel_id:
            self.session.execute(update(table).where(table.c.id == int(channel_id)).values(**payload))
        else:
            self.session.execute(table.insert().values(**payload, created_at=now))

    def delete_notification_channel(self, channel_id: int) -> None:
        self.session.execute(delete(NotificationChannels.__table__).where(NotificationChannels.__table__.c.id == channel_id))

    def enabled_webhook_server(self, server_id: int) -> Server | None:
        return self.session.scalar(select(Server).where(Server.id == server_id, Server.enabled.is_(True)))

    def allow_webhook(self, server_id: int, source_ip: str, cutoff: str, now: str, limit: int = 60) -> bool:
        table = WebhookRateLimits.__table__
        self.session.execute(delete(table).where(table.c.created_at < cutoff))
        count = int(self.session.scalar(select(func.count()).where(table.c.server_id == server_id, table.c.source_ip == source_ip, table.c.created_at >= cutoff)) or 0)
        if count >= limit:
            return False
        self.session.execute(table.insert().values(server_id=server_id, source_ip=source_ip, created_at=now))
        return True

    def record_webhook(self, server_id: int, event: str, summary: str, payload: str, source_ip: str, now: str) -> int:
        result = self.session.execute(WebhookEvents.__table__.insert().values(server_id=server_id, event=event, summary=summary, payload=payload, status="received", source_ip=source_ip, created_at=now))
        return int(result.inserted_primary_key[0])

    def update_webhook_result(self, event_id: int, result: str) -> None:
        self.session.execute(update(WebhookEvents.__table__).where(WebhookEvents.__table__.c.id == event_id).values(result=result))

    def matching_webhook_rules(self, server_id: int, event: str) -> list[dict[str, Any]]:
        table = WebhookRules.__table__
        return [dict(row._mapping) for row in self.session.execute(select(table).where(table.c.server_id == server_id, table.c.event == event, table.c.enabled == 1))]

    def webhook_events(self, limit: int = 100) -> list[dict[str, Any]]:
        events, servers = WebhookEvents.__table__, Server.__table__
        query = select(events, servers.c.name.label("server_name")).join(servers, servers.c.id == events.c.server_id).order_by(events.c.id.desc()).limit(limit)
        return [dict(row._mapping) for row in self.session.execute(query)]

    def collection_rules(self) -> list[dict[str, Any]]:
        rules, servers = CollectionRules.__table__, Server.__table__
        query = select(rules, servers.c.name.label("server_name")).join(servers, servers.c.id == rules.c.server_id).order_by(rules.c.id.desc())
        return [dict(row._mapping) for row in self.session.execute(query)]

    def collection_rule(self, rule_id: int) -> dict[str, Any] | None:
        row = self.session.execute(select(CollectionRules.__table__).where(CollectionRules.__table__.c.id == rule_id)).mappings().first()
        return dict(row) if row else None

    def save_collection_rule(self, values: dict[str, Any], now: str, rule_id: int | None = None) -> int:
        table = CollectionRules.__table__
        payload = {"server_id": int(values["server_id"]), "library_id": int(values["library_id"]), "name": values.get("name") or "合集规则", "match_field": values.get("match_field") or "title", "match_value": values.get("match_value") or "", "collection_title": values.get("collection_title") or "", "title_sort_mode": values.get("title_sort_mode") or "pinyin", "enabled": 1 if values.get("enabled", True) else 0, "updated_at": now}
        if rule_id:
            self.session.execute(update(table).where(table.c.id == rule_id).values(**payload))
            return rule_id
        result = self.session.execute(table.insert().values(**payload, created_at=now))
        return int(result.inserted_primary_key[0])

    def delete_collection_rule(self, rule_id: int) -> None:
        self.session.execute(delete(CollectionRules.__table__).where(CollectionRules.__table__.c.id == rule_id))

    def record_collection_rule_run(self, rule_id: int, job_id: int, status: str, matched_count: int, now: str) -> int:
        table = CollectionRuleRuns.__table__
        result = self.session.execute(
            insert(table).values(
                rule_id=rule_id,
                job_id=job_id,
                status=status,
                matched_count=matched_count,
                created_at=now,
            )
        )
        return int(result.inserted_primary_key[0])

    def replace_tag_suggestions(self, server_id: int, suggestions: dict[str, dict[str, int]], now: str) -> None:
        table = TagSuggestions.__table__
        self.session.execute(delete(table).where(table.c.server_id == server_id))
        self.session.execute(
            insert(table),
            [
                {
                    "server_id": server_id,
                    "field": field,
                    "source": source,
                    "suggested": source,
                    "count": int(count),
                    "created_at": now,
                }
                for field, values in suggestions.items()
                for source, count in values.items()
            ],
        )

    def enabled_notification_channels(self) -> list[dict[str, Any]]:
        table = NotificationChannels.__table__
        return [dict(row) for row in self.session.execute(select(table).where(table.c.enabled == 1)).mappings()]

    def episode_match_overrides(self, server_id: int | None = None, library_id: int | None = None) -> list[dict[str, Any]]:
        table = EpisodeMatchOverrides.__table__
        query = select(table)
        if server_id is not None:
            query = query.where(table.c.server_id == server_id)
        if library_id is not None:
            query = query.where(table.c.library_id == library_id)
        return [dict(row._mapping) for row in self.session.execute(query.order_by(table.c.server_id, table.c.library_id, table.c.plex_rating_key))]

    def save_episode_match_override(self, server_id: int, library_id: int, rating_key: str, tmdb_id: int, now: str) -> None:
        table = EpisodeMatchOverrides.__table__
        self.session.execute(insert(table).values(server_id=server_id, library_id=library_id, plex_rating_key=rating_key, tmdb_id=tmdb_id, created_at=now, updated_at=now).prefix_with("OR REPLACE"))

    def delete_episode_match_override(self, server_id: int, library_id: int, rating_key: str) -> None:
        table = EpisodeMatchOverrides.__table__
        self.session.execute(delete(table).where(table.c.server_id == server_id, table.c.library_id == library_id, table.c.plex_rating_key == rating_key))

    def episode_audit_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        runs, servers, jobs = EpisodeAuditRuns.__table__, Server.__table__, Job.__table__
        query = select(runs, servers.c.name.label("server_name"), jobs.c.status.label("job_status")).join(servers, servers.c.id == runs.c.server_id).outerjoin(jobs, jobs.c.id == runs.c.job_id).order_by(runs.c.id.desc()).limit(limit)
        return [dict(row._mapping) for row in self.session.execute(query)]

    def episode_audit_run(self, run_id: int) -> dict[str, Any] | None:
        runs, servers, jobs = EpisodeAuditRuns.__table__, Server.__table__, Job.__table__
        row = self.session.execute(select(runs, servers.c.name.label("server_name"), jobs.c.status.label("job_status")).join(servers, servers.c.id == runs.c.server_id).outerjoin(jobs, jobs.c.id == runs.c.job_id).where(runs.c.id == run_id)).mappings().first()
        return dict(row) if row else None

    def episode_audit_items(self, run_id: int, status: str = "", query_text: str = "") -> list[dict[str, Any]]:
        table = EpisodeAuditItems.__table__
        query = select(table).where(table.c.run_id == run_id)
        if status:
            query = query.where(table.c.status == status)
        if query_text:
            pattern = f"%{query_text}%"
            query = query.where(or_(table.c.show_title.like(pattern), table.c.tmdb_title.like(pattern)))
        return [dict(row._mapping) for row in self.session.execute(query.order_by(table.c.show_title, table.c.season, table.c.episode, table.c.id))]

    def episode_audit_item_with_run(self, item_id: int) -> dict[str, Any] | None:
        items, runs = EpisodeAuditItems.__table__, EpisodeAuditRuns.__table__
        row = self.session.execute(select(items, runs.c.server_id, runs.c.library_id).join(runs, runs.c.id == items.c.run_id).where(items.c.id == item_id)).mappings().first()
        return dict(row) if row else None

    def episode_audit_ignores(self, server_id: int | None = None, library_id: int | None = None) -> list[dict[str, Any]]:
        table = EpisodeAuditIgnores.__table__
        query = select(table)
        if server_id is not None:
            query = query.where(table.c.server_id == server_id)
        if library_id is not None:
            query = query.where(table.c.library_id == library_id)
        return [dict(row._mapping) for row in self.session.execute(query.order_by(table.c.show_title, table.c.season, table.c.episode))]

    def save_episode_ignore(self, values: dict[str, Any], now: str) -> int:
        table = EpisodeAuditIgnores.__table__
        duplicate = self.session.execute(select(table.c.id).where(table.c.server_id == values["server_id"], table.c.library_id == values["library_id"], table.c.plex_rating_key == values["plex_rating_key"], func.coalesce(table.c.tmdb_id, -1) == func.coalesce(values.get("tmdb_id"), -1), func.coalesce(table.c.season, -1) == func.coalesce(values.get("season"), -1), func.coalesce(table.c.episode, -1) == func.coalesce(values.get("episode"), -1))).scalar()
        if duplicate:
            self.session.execute(delete(table).where(table.c.id == duplicate))
        result = self.session.execute(insert(table).values(**values, created_at=now))
        return int(result.inserted_primary_key[0])

    def delete_episode_ignore(self, ignore_id: int) -> None:
        self.session.execute(delete(EpisodeAuditIgnores.__table__).where(EpisodeAuditIgnores.__table__.c.id == ignore_id))

    def mark_episode_items_ignored(self, item: dict[str, Any], scope: str, reason: str) -> None:
        table = EpisodeAuditItems.__table__
        conditions = [table.c.run_id == item["run_id"], table.c.status.in_(["present", "missing", "unmatched_show", "ambiguous_match"])]
        if scope == "show":
            plex_key = str(item.get("plex_rating_key") or "")
            show_title = str(item.get("show_title") or "")
            tmdb_condition = table.c.tmdb_id == item["tmdb_id"] if item.get("tmdb_id") is not None else table.c.tmdb_id.is_(None)
            conditions.append(or_(table.c.plex_rating_key == plex_key, tmdb_condition, table.c.show_title == show_title))
        else:
            conditions.append(table.c.id == item["id"])
        rows = self.session.execute(select(table.c.id, table.c.details).where(*conditions)).mappings().all()
        status_value = "ignored_show" if scope == "show" else "ignored_missing"
        for row in rows:
            try:
                details = json.loads(row["details"] or "{}")
            except (TypeError, json.JSONDecodeError):
                details = {}
            details["ignore_reason"] = reason
            self.session.execute(update(table).where(table.c.id == row["id"]).values(status=status_value, details=json.dumps(details, ensure_ascii=False)))

    def refresh_episode_audit_counts(self, run_id: int) -> None:
        table, runs = EpisodeAuditItems.__table__, EpisodeAuditRuns.__table__
        values = {
            "missing_count": select(func.count()).where(table.c.run_id == run_id, table.c.status == "missing").scalar_subquery(),
            "unmatched_count": select(func.count()).where(table.c.run_id == run_id, table.c.status == "unmatched_show").scalar_subquery(),
            "ambiguous_count": select(func.count()).where(table.c.run_id == run_id, table.c.status == "ambiguous_match").scalar_subquery(),
            "ignored_count": select(func.count()).where(table.c.run_id == run_id, table.c.status.in_(["ignored_missing", "ignored_show"])).scalar_subquery(),
        }
        self.session.execute(update(runs).where(runs.c.id == run_id).values(**values))

    def create_episode_audit_run(self, job_id: int, server_id: int, library_id: int, options: str, now: str) -> int:
        result = self.session.execute(
            insert(EpisodeAuditRuns.__table__).values(
                job_id=job_id,
                server_id=server_id,
                library_id=library_id,
                status="running",
                options=options,
                created_at=now,
            )
        )
        return int(result.inserted_primary_key[0])

    def update_episode_audit_run(self, run_id: int, values: dict[str, Any]) -> None:
        self.session.execute(update(EpisodeAuditRuns.__table__).where(EpisodeAuditRuns.__table__.c.id == run_id).values(**values))

    def add_episode_audit_item(self, values: dict[str, Any]) -> int:
        result = self.session.execute(insert(EpisodeAuditItems.__table__).values(**values))
        return int(result.inserted_primary_key[0])

    def episode_match_override(self, server_id: int, library_id: int, rating_key: str) -> dict[str, Any] | None:
        table = EpisodeMatchOverrides.__table__
        row = self.session.execute(
            select(table).where(
                table.c.server_id == server_id,
                table.c.library_id == library_id,
                table.c.plex_rating_key == rating_key,
            )
        ).mappings().first()
        return dict(row) if row else None

    def episode_ignores(
        self,
        server_id: int,
        library_id: int,
        plex_rating_key: str,
        tmdb_id: int | None,
        show_title: str,
    ) -> list[dict[str, Any]]:
        table = EpisodeAuditIgnores.__table__
        query = select(table).where(
            table.c.server_id == server_id,
            table.c.library_id == library_id,
            or_(
                table.c.plex_rating_key == plex_rating_key,
                (table.c.tmdb_id.is_not(None) & (table.c.tmdb_id == tmdb_id)),
                table.c.show_title == show_title,
            ),
        ).order_by(table.c.season.is_not(None), table.c.id.desc())
        return [dict(row) for row in self.session.execute(query).mappings()]
