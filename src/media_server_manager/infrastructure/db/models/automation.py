from __future__ import annotations

from sqlalchemy import Column, ForeignKey, Integer, Table, Text

from ...security.secrets import EncryptedText
from ..session import Base


class CollectionRuleRuns(Base):
    __table__ = Table(
        "collection_rule_runs", Base.metadata,
        Column("id", Integer(), primary_key=True),
        Column("rule_id", Integer(), ForeignKey("collection_rules.id", ondelete="CASCADE"), nullable=False),
        Column("job_id", Integer(), ForeignKey("jobs.id", ondelete="SET NULL")),
        Column("status", Text(), nullable=False),
        Column("matched_count", Integer(), nullable=False),
        Column("created_at", Text(), nullable=False),
    )

class CollectionRules(Base):
    __table__ = Table(
        "collection_rules", Base.metadata,
        Column("id", Integer(), primary_key=True),
        Column("server_id", Integer(), ForeignKey("servers.id", ondelete="CASCADE"), nullable=False),
        Column("library_id", Integer(), nullable=False),
        Column("name", Text(), nullable=False),
        Column("match_field", Text(), nullable=False),
        Column("match_value", Text(), nullable=False),
        Column("collection_title", Text(), nullable=False),
        Column("title_sort_mode", Text(), nullable=False),
        Column("enabled", Integer(), nullable=False),
        Column("created_at", Text(), nullable=False),
        Column("updated_at", Text(), nullable=False),
    )

class ContinueWatchingItems(Base):
    __table__ = Table(
        "continue_watching_items", Base.metadata,
        Column("id", Integer(), primary_key=True),
        Column("run_id", Integer(), ForeignKey("continue_watching_runs.id", ondelete="CASCADE"), nullable=False),
        Column("server_id", Integer(), ForeignKey("servers.id", ondelete="CASCADE"), nullable=False),
        Column("library_id", Integer(), nullable=False),
        Column("show_title", Text(), nullable=False),
        Column("show_rating_key", Text(), nullable=False),
        Column("episode_rating_key", Text(), nullable=False),
        Column("season", Integer(), nullable=False),
        Column("episode", Integer(), nullable=False),
        Column("episode_title", Text(), nullable=False),
        Column("duration", Integer(), nullable=False),
        Column("planned_offset", Integer(), nullable=False),
        Column("current_offset", Integer(), nullable=False),
        Column("view_count", Integer(), nullable=False),
        Column("status", Text(), nullable=False),
        Column("result", Text(), nullable=False),
        Column("created_at", Text(), nullable=False),
        Column("updated_at", Text(), nullable=False),
    )

class ContinueWatchingRuns(Base):
    __table__ = Table(
        "continue_watching_runs", Base.metadata,
        Column("id", Integer(), primary_key=True),
        Column("job_id", Integer(), ForeignKey("jobs.id", ondelete="SET NULL")),
        Column("server_id", Integer(), ForeignKey("servers.id", ondelete="CASCADE"), nullable=False),
        Column("library_id", Integer(), nullable=False),
        Column("mode", Text(), nullable=False),
        Column("status", Text(), nullable=False),
        Column("candidate_count", Integer(), nullable=False),
        Column("applied_count", Integer(), nullable=False),
        Column("error_count", Integer(), nullable=False),
        Column("created_at", Text(), nullable=False),
        Column("finished_at", Text()),
    )

class EpisodeAuditIgnores(Base):
    __table__ = Table(
        "episode_audit_ignores", Base.metadata,
        Column("id", Integer(), primary_key=True),
        Column("server_id", Integer(), ForeignKey("servers.id", ondelete="CASCADE"), nullable=False),
        Column("library_id", Integer(), nullable=False),
        Column("show_title", Text(), nullable=False),
        Column("plex_rating_key", Text(), nullable=False),
        Column("tmdb_id", Integer()),
        Column("season", Integer()),
        Column("episode", Integer()),
        Column("reason", Text(), nullable=False),
        Column("created_at", Text(), nullable=False),
    )

class EpisodeAuditItems(Base):
    __table__ = Table(
        "episode_audit_items", Base.metadata,
        Column("id", Integer(), primary_key=True),
        Column("run_id", Integer(), ForeignKey("episode_audit_runs.id", ondelete="CASCADE"), nullable=False),
        Column("show_title", Text(), nullable=False),
        Column("plex_rating_key", Text(), nullable=False),
        Column("tmdb_id", Integer()),
        Column("tmdb_title", Text(), nullable=False),
        Column("match_source", Text(), nullable=False),
        Column("season", Integer()),
        Column("episode", Integer()),
        Column("air_date", Text(), nullable=False),
        Column("status", Text(), nullable=False),
        Column("details", Text(), nullable=False),
        Column("created_at", Text(), nullable=False),
    )

class EpisodeAuditRuns(Base):
    __table__ = Table(
        "episode_audit_runs", Base.metadata,
        Column("id", Integer(), primary_key=True),
        Column("job_id", Integer(), ForeignKey("jobs.id", ondelete="SET NULL")),
        Column("server_id", Integer(), ForeignKey("servers.id", ondelete="CASCADE"), nullable=False),
        Column("library_id", Integer(), nullable=False),
        Column("status", Text(), nullable=False),
        Column("options", Text(), nullable=False),
        Column("total_shows", Integer(), nullable=False),
        Column("checked_shows", Integer(), nullable=False),
        Column("missing_count", Integer(), nullable=False),
        Column("unmatched_count", Integer(), nullable=False),
        Column("ambiguous_count", Integer(), nullable=False),
        Column("ignored_count", Integer(), nullable=False),
        Column("created_at", Text(), nullable=False),
        Column("finished_at", Text()),
    )

class EpisodeMatchOverrides(Base):
    __table__ = Table(
        "episode_match_overrides", Base.metadata,
        Column("server_id", Integer(), ForeignKey("servers.id", ondelete="CASCADE"), primary_key=True, nullable=False),
        Column("library_id", Integer(), primary_key=True, nullable=False),
        Column("plex_rating_key", Text(), primary_key=True, nullable=False),
        Column("tmdb_id", Integer(), nullable=False),
        Column("created_at", Text(), nullable=False),
        Column("updated_at", Text(), nullable=False),
    )

class MediaLibraries(Base):
    __table__ = Table(
        "media_libraries", Base.metadata,
        Column("server_id", Integer(), ForeignKey("servers.id", ondelete="CASCADE"), primary_key=True, nullable=False),
        Column("library_id", Integer(), primary_key=True, nullable=False),
        Column("title", Text(), nullable=False),
        Column("plex_type", Integer(), nullable=False),
        Column("animation_mode", Text(), nullable=False),
        Column("auto_animation", Integer(), nullable=False),
        Column("plex_synced_at", Text()),
        Column("tmdb_synced_at", Text()),
        Column("sync_job_id", Integer()),
        Column("sync_status", Text(), nullable=False),
        Column("sync_error", Text(), nullable=False),
        Column("updated_at", Text(), nullable=False),
        Column("display_order", Integer(), nullable=False),
        Column("auto_recheck_new_episodes", Integer(), nullable=False),
        Column("sort_key", Text(), nullable=False),
        Column("sort_direction", Text(), nullable=False),
    )

class NotificationChannels(Base):
    __table__ = Table(
        "notification_channels", Base.metadata,
        Column("id", Integer(), primary_key=True),
        Column("name", Text(), nullable=False),
        Column("channel_type", Text(), nullable=False),
        Column("url", EncryptedText(), nullable=False),
        Column("events", Text(), nullable=False),
        Column("enabled", Integer(), nullable=False),
        Column("created_at", Text(), nullable=False),
        Column("updated_at", Text(), nullable=False),
    )

class TagMappings(Base):
    __table__ = Table(
        "tag_mappings", Base.metadata,
        Column("source", Text(), primary_key=True),
        Column("target", Text(), nullable=False),
    )

class TagSuggestions(Base):
    __table__ = Table(
        "tag_suggestions", Base.metadata,
        Column("id", Integer(), primary_key=True),
        Column("server_id", Integer(), ForeignKey("servers.id", ondelete="CASCADE"), nullable=False),
        Column("field", Text(), nullable=False),
        Column("source", Text(), nullable=False),
        Column("suggested", Text(), nullable=False),
        Column("count", Integer(), nullable=False),
        Column("created_at", Text(), nullable=False),
    )

class WebhookEvents(Base):
    __table__ = Table(
        "webhook_events", Base.metadata,
        Column("id", Integer(), primary_key=True),
        Column("server_id", Integer(), ForeignKey("servers.id", ondelete="CASCADE"), nullable=False),
        Column("event", Text(), nullable=False),
        Column("summary", Text(), nullable=False),
        Column("payload", Text(), nullable=False),
        Column("status", Text(), nullable=False),
        Column("result", Text(), nullable=False),
        Column("source_ip", Text(), nullable=False),
        Column("created_at", Text(), nullable=False),
    )

class WebhookRateLimits(Base):
    __table__ = Table(
        "webhook_rate_limits", Base.metadata,
        Column("id", Integer(), primary_key=True),
        Column("server_id", Integer(), ForeignKey("servers.id", ondelete="CASCADE"), nullable=False),
        Column("source_ip", Text(), nullable=False),
        Column("created_at", Text(), nullable=False),
    )

class WebhookRules(Base):
    __table__ = Table(
        "webhook_rules", Base.metadata,
        Column("id", Integer(), primary_key=True),
        Column("server_id", Integer(), ForeignKey("servers.id", ondelete="CASCADE"), nullable=False),
        Column("event", Text(), nullable=False),
        Column("action", Text(), nullable=False),
        Column("enabled", Integer(), nullable=False),
        Column("created_at", Text(), nullable=False),
        Column("updated_at", Text(), nullable=False),
    )
