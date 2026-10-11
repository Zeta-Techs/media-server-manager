"""Domain vocabulary for the durable job queue."""

from __future__ import annotations

from enum import StrEnum


class JobType(StrEnum):
    ALL = "all"
    LOCALIZE = "localize"
    APPLY_CHANGE_SET = "apply_change_set"
    ROLLBACK = "rollback"
    WEBHOOK = "webhook"
    MAINTENANCE = "maintenance"
    COLLECTION_RULE = "collection_rule"
    TAG_SUGGESTIONS = "tag_suggestions"
    EPISODE_AUDIT = "episode_audit"
    CONTINUE_WATCHING_PREVIEW = "continue_watching_preview"
    CONTINUE_WATCHING_APPLY = "continue_watching_apply"
    NOTIFICATION_TEST = "notification_test"
    NOTIFICATION_EVENT = "notification_event"
    TMDB_CATALOG_SYNC = "tmdb_catalog_sync"
    PLEX_INVENTORY_SYNC = "plex_inventory_sync"
    MEDIA_RECONCILE = "media_reconcile"
    MEDIA_SYNC = "media_sync"
    MEDIA_LIBRARY_REFRESH = "media_library_refresh"
    MEDIA_LIBRARY_TMDB_REFRESH = "media_library_tmdb_refresh"
    MEDIA_LIBRARY_FULL_REFRESH = "media_library_full_refresh"
    MEDIA_LIBRARY_SHOW_RECHECK = "media_library_show_recheck"
    MEDIA_LIBRARY_BULK_RESOLVE = "media_library_bulk_resolve"
    MEDIA_LIBRARY_BULK_CONFIRM = "media_library_bulk_confirm"
    MEDIA_LIBRARY_SEARCH_ADD = "media_library_search_add"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


JOB_TYPES = frozenset(item.value for item in JobType)
TERMINAL_JOB_STATUSES = frozenset(
    {
        JobStatus.SUCCEEDED.value,
        JobStatus.FAILED.value,
        JobStatus.CANCELLED.value,
        JobStatus.INTERRUPTED.value,
    }
)

__all__ = ["JOB_TYPES", "TERMINAL_JOB_STATUSES", "JobStatus", "JobType"]
