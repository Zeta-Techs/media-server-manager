from __future__ import annotations

QUEUE_BY_JOB_TYPE = {
    "localize": "plex_io", "all": "plex_io", "apply_change_set": "plex_io", "rollback": "plex_io",
    "webhook": "webhook", "media_library_refresh": "media_sync", "media_library_full_refresh": "media_sync",
    "plex_inventory_sync": "media_sync", "media_reconcile": "media_sync", "tmdb_catalog_sync": "catalog_sync",
    "notification_test": "notifications", "notification_event": "notifications", "maintenance": "maintenance",
}


def queue_for_job_type(job_type: str) -> str:
    return QUEUE_BY_JOB_TYPE.get(job_type, "media_sync")
