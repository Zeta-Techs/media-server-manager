"""Read and write indexes shared by the ORM metadata and Alembic.

The legacy schema was created without a consistent index policy.  These
indexes cover the queue, synchronization, webhook and media-library access
patterns used by the repositories while keeping the natural keys unchanged.
"""

from __future__ import annotations

from sqlalchemy import Index

from ..session import Base

_INDEXES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("ix_auth_attempts_lookup", "auth_attempts", ("username", "ip_address", "created_at")),
    ("ix_oauth_flows_expiry", "oauth_flows", ("expires_at",)),
    ("ix_servers_enabled", "servers", ("enabled",)),
    ("ix_jobs_queue", "jobs", ("status", "server_id", "id")),
    ("ix_job_logs_job", "job_logs", ("job_id", "id")),
    ("ix_schedules_due", "schedules", ("enabled", "next_run_at")),
    ("ix_change_sets_job", "change_sets", ("job_id",)),
    ("ix_changes_job", "changes", ("job_id", "change_set_id")),
    ("ix_tmdb_items_lookup", "tmdb_items", ("media_type", "tmdb_id", "season_number", "episode_number")),
    ("ix_tmdb_items_parent", "tmdb_items", ("parent_tmdb_id", "media_type")),
    ("ix_tmdb_images_lookup", "tmdb_images", ("media_type", "tmdb_id", "image_type")),
    ("ix_tmdb_sync_pages_run", "tmdb_sync_pages", ("run_id", "media_type", "page")),
    ("ix_tmdb_sync_runs_status", "tmdb_sync_runs", ("status", "server_id")),
    ("ix_media_libraries_server", "media_libraries", ("server_id", "library_id")),
    ("ix_media_library_items_lookup", "media_library_items", ("server_id", "library_id", "rating_key")),
    ("ix_media_library_items_tmdb", "media_library_items", ("server_id", "tmdb_id")),
    ("ix_plex_inventory_lookup", "plex_inventory_items", ("server_id", "library_id", "rating_key")),
    ("ix_media_match_results_lookup", "media_match_results", ("server_id", "library_id", "status")),
    ("ix_recheck_due", "media_library_recheck_requests", ("status", "next_run_at")),
    ("ix_webhook_events_server_time", "webhook_events", ("server_id", "created_at")),
    ("ix_webhook_rules_event", "webhook_rules", ("server_id", "event", "enabled")),
    ("ix_notification_channels_enabled", "notification_channels", ("enabled",)),
    ("ix_continue_watching_run", "continue_watching_items", ("run_id", "status")),
    ("ix_episode_audit_run", "episode_audit_items", ("run_id", "status")),
    ("ix_collection_rules_enabled", "collection_rules", ("server_id", "enabled")),
    ("ix_bulk_rows_batch", "media_library_bulk_rows", ("batch_id", "status")),
    ("ix_anime_schedule_next", "anime_show_schedule", ("next_sync_at", "schedule_status")),
    ("ix_migration_runs_status", "migration_runs", ("status", "started_at")),
)


def register_indexes() -> None:
    for name, table_name, columns in _INDEXES:
        table = Base.metadata.tables.get(table_name)
        if table is None:
            continue
        if name not in {index.name for index in table.indexes}:
            Index(name, *(table.c[column] for column in columns))


register_indexes()

__all__ = ["register_indexes"]
