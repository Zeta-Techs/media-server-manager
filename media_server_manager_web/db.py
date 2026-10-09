from __future__ import annotations

import json
import os
import secrets
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from media_server_manager.core import CONFIG_DIR, TEMPLATE_TAGS_FILE, ServerConfig, split_skip_libraries

SCHEMA_VERSION = 5
DB_FILE = CONFIG_DIR / "media_server_manager.db"
DEFAULT_USERNAME = "admin"


class IncompatibleSchemaError(RuntimeError):
    pass


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_secret() -> str:
    return secrets.token_urlsafe(32)


class ManagedConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        result = super().__exit__(exc_type, exc_value, traceback)
        self.close()
        return result


def connect(db_file: Path | None = None) -> sqlite3.Connection:
    db_file = Path(db_file or DB_FILE)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_file, timeout=30, check_same_thread=False, factory=ManagedConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db(db_file: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(db_file)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def row_to_dict(row: sqlite3.Row | None) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


def rows_to_dicts(rows: List[sqlite3.Row]) -> List[Dict[str, Any]]:
    return [dict(row) for row in rows]


def _existing_schema_version(db_file: Path) -> int | None:
    if not db_file.exists() or db_file.stat().st_size == 0:
        return None
    uri = f"{db_file.resolve().as_uri()}?mode=ro&immutable=1"
    db = sqlite3.connect(uri, uri=True)
    incompatible = False
    result: int | None = None
    try:
        db.row_factory = sqlite3.Row
        tables = {
            row["name"]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        if not tables:
            return None
        if "schema_meta" not in tables:
            incompatible = True
        else:
            row = db.execute("SELECT version FROM schema_meta WHERE id = 1").fetchone()
            result = int(row["version"]) if row else 0
    finally:
        db.close()
        del db
    if incompatible:
        raise IncompatibleSchemaError(
            "检测到旧版数据库。请先运行 `python -m media_server_manager_admin reset-db --backup`。"
        )
    return result


def init_db(db_file: Path | None = None) -> None:
    db_file = Path(db_file or DB_FILE)
    version = _existing_schema_version(db_file)
    if version not in (None, 2, 3, 4, SCHEMA_VERSION):
        raise IncompatibleSchemaError(
            f"数据库版本 {version} 与应用版本 {SCHEMA_VERSION} 不兼容。"
            "请运行 `python -m media_server_manager_admin reset-db --backup`。"
        )

    with get_db(db_file) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS auth_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                ip_address TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_auth_attempts_lookup
                ON auth_attempts(username, ip_address, created_at);

            CREATE TABLE IF NOT EXISTS servers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                address TEXT NOT NULL,
                token TEXT NOT NULL,
                skip_libraries TEXT NOT NULL DEFAULT '',
                pinyin_mode TEXT NOT NULL DEFAULT 'first_letter',
                auth_source TEXT NOT NULL DEFAULT 'manual',
                webhook_secret TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS oauth_flows (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                pin_id INTEGER NOT NULL,
                token TEXT NOT NULL DEFAULT '',
                resources TEXT NOT NULL DEFAULT '[]',
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                server_id INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('queued','running','succeeded','failed','cancelled','interrupted')),
                stage TEXT NOT NULL DEFAULT '',
                current_library TEXT NOT NULL DEFAULT '',
                total INTEGER NOT NULL DEFAULT 0,
                processed INTEGER NOT NULL DEFAULT 0,
                changes INTEGER NOT NULL DEFAULT 0,
                errors INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL DEFAULT '{}',
                worker_id TEXT NOT NULL DEFAULT '',
                retry_of INTEGER,
                created_at TEXT NOT NULL,
                claimed_at TEXT,
                started_at TEXT,
                cancel_requested_at TEXT,
                finished_at TEXT,
                FOREIGN KEY(server_id) REFERENCES servers(id) ON DELETE CASCADE,
                FOREIGN KEY(retry_of) REFERENCES jobs(id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs(status, id);
            CREATE INDEX IF NOT EXISTS idx_jobs_server_status ON jobs(server_id, status);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_one_running_per_server
                ON jobs(server_id) WHERE status = 'running';

            CREATE TABLE IF NOT EXISTS job_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_job_logs_job_id ON job_logs(job_id, id);

            CREATE TABLE IF NOT EXISTS worker_heartbeats (
                worker_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                heartbeat_at TEXT NOT NULL,
                pid INTEGER NOT NULL,
                concurrency INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                schedule_type TEXT NOT NULL CHECK (schedule_type IN ('cron', 'interval')),
                schedule_value TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}',
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                next_run_at TEXT,
                last_run_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(server_id) REFERENCES servers(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_schedules_due ON schedules(enabled, next_run_at);

            CREATE TABLE IF NOT EXISTS change_sets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                server_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT '{}',
                source_change_set_id INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE,
                FOREIGN KEY(server_id) REFERENCES servers(id) ON DELETE CASCADE,
                FOREIGN KEY(source_change_set_id) REFERENCES change_sets(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                change_set_id INTEGER NOT NULL,
                job_id INTEGER NOT NULL,
                server_id INTEGER NOT NULL,
                library_id INTEGER NOT NULL DEFAULT 0,
                media_type TEXT NOT NULL DEFAULT '',
                rating_key TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                field TEXT NOT NULL,
                old_value TEXT NOT NULL DEFAULT 'null',
                new_value TEXT NOT NULL DEFAULT 'null',
                old_locked INTEGER,
                apply_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (apply_status IN ('pending','applied','conflict','failed')),
                apply_error TEXT NOT NULL DEFAULT '',
                applied INTEGER NOT NULL DEFAULT 0,
                rollback_status TEXT NOT NULL DEFAULT '',
                rollback_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                applied_at TEXT,
                rolled_back_at TEXT,
                FOREIGN KEY(change_set_id) REFERENCES change_sets(id) ON DELETE CASCADE,
                FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE,
                FOREIGN KEY(server_id) REFERENCES servers(id) ON DELETE CASCADE,
                UNIQUE(change_set_id, rating_key, field)
            );

            CREATE TABLE IF NOT EXISTS webhook_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server_id INTEGER NOT NULL,
                event TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'received',
                result TEXT NOT NULL DEFAULT '',
                source_ip TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(server_id) REFERENCES servers(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_webhook_events_created ON webhook_events(created_at);

            CREATE TABLE IF NOT EXISTS webhook_rate_limits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server_id INTEGER NOT NULL,
                source_ip TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(server_id) REFERENCES servers(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_webhook_rate_lookup
                ON webhook_rate_limits(server_id, source_ip, created_at);

            CREATE TABLE IF NOT EXISTS webhook_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server_id INTEGER NOT NULL,
                event TEXT NOT NULL,
                action TEXT NOT NULL CHECK (action IN ('localize','notify','record')),
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(server_id) REFERENCES servers(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS tag_mappings (
                source TEXT PRIMARY KEY,
                target TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS collection_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server_id INTEGER NOT NULL,
                library_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                match_field TEXT NOT NULL,
                match_value TEXT NOT NULL,
                collection_title TEXT NOT NULL,
                title_sort_mode TEXT NOT NULL DEFAULT 'pinyin',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(server_id) REFERENCES servers(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS collection_rule_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id INTEGER NOT NULL,
                job_id INTEGER,
                status TEXT NOT NULL,
                matched_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY(rule_id) REFERENCES collection_rules(id) ON DELETE CASCADE,
                FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS tag_suggestions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server_id INTEGER NOT NULL,
                field TEXT NOT NULL,
                source TEXT NOT NULL,
                suggested TEXT NOT NULL DEFAULT '',
                count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(server_id, field, source),
                FOREIGN KEY(server_id) REFERENCES servers(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS notification_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                channel_type TEXT NOT NULL DEFAULT 'webhook',
                url TEXT NOT NULL,
                events TEXT NOT NULL DEFAULT '[]',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS episode_match_overrides (
                server_id INTEGER NOT NULL,
                library_id INTEGER NOT NULL,
                plex_rating_key TEXT NOT NULL,
                tmdb_id INTEGER NOT NULL,
                imdb_id TEXT NOT NULL DEFAULT '',
                tvdb_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(server_id, library_id, plex_rating_key),
                FOREIGN KEY(server_id) REFERENCES servers(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS episode_audit_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER,
                server_id INTEGER NOT NULL,
                library_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                options TEXT NOT NULL DEFAULT '{}',
                total_shows INTEGER NOT NULL DEFAULT 0,
                checked_shows INTEGER NOT NULL DEFAULT 0,
                missing_count INTEGER NOT NULL DEFAULT 0,
                unmatched_count INTEGER NOT NULL DEFAULT 0,
                ambiguous_count INTEGER NOT NULL DEFAULT 0,
                ignored_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                finished_at TEXT,
                FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE SET NULL,
                FOREIGN KEY(server_id) REFERENCES servers(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS episode_audit_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                show_title TEXT NOT NULL,
                plex_rating_key TEXT NOT NULL DEFAULT '',
                tmdb_id INTEGER,
                tmdb_title TEXT NOT NULL DEFAULT '',
                match_source TEXT NOT NULL DEFAULT '',
                season INTEGER,
                episode INTEGER,
                air_date TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES episode_audit_runs(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS episode_audit_ignores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server_id INTEGER NOT NULL,
                library_id INTEGER NOT NULL,
                show_title TEXT NOT NULL DEFAULT '',
                plex_rating_key TEXT NOT NULL DEFAULT '',
                tmdb_id INTEGER,
                season INTEGER,
                episode INTEGER,
                reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(server_id) REFERENCES servers(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS continue_watching_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER,
                server_id INTEGER NOT NULL,
                library_id INTEGER NOT NULL,
                mode TEXT NOT NULL DEFAULT 'preview',
                status TEXT NOT NULL DEFAULT 'running',
                candidate_count INTEGER NOT NULL DEFAULT 0,
                applied_count INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                finished_at TEXT,
                FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE SET NULL,
                FOREIGN KEY(server_id) REFERENCES servers(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS continue_watching_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                server_id INTEGER NOT NULL,
                library_id INTEGER NOT NULL,
                show_title TEXT NOT NULL DEFAULT '',
                show_rating_key TEXT NOT NULL DEFAULT '',
                episode_rating_key TEXT NOT NULL,
                season INTEGER NOT NULL DEFAULT 0,
                episode INTEGER NOT NULL DEFAULT 0,
                episode_title TEXT NOT NULL DEFAULT '',
                duration INTEGER NOT NULL DEFAULT 0,
                planned_offset INTEGER NOT NULL DEFAULT 0,
                current_offset INTEGER NOT NULL DEFAULT 0,
                view_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'candidate',
                result TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES continue_watching_runs(id) ON DELETE CASCADE,
                FOREIGN KEY(server_id) REFERENCES servers(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS tmdb_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tmdb_id INTEGER NOT NULL,
                media_type TEXT NOT NULL CHECK (media_type IN ('movie','tv','season','episode')),
                imdb_id TEXT NOT NULL DEFAULT '',
                tvdb_id TEXT NOT NULL DEFAULT '',
                parent_tmdb_id INTEGER,
                season_number INTEGER,
                episode_number INTEGER,
                title TEXT NOT NULL DEFAULT '',
                original_title TEXT NOT NULL DEFAULT '',
                release_date TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                overview TEXT NOT NULL DEFAULT '',
                poster_path TEXT NOT NULL DEFAULT '',
                backdrop_path TEXT NOT NULL DEFAULT '',
                still_path TEXT NOT NULL DEFAULT '',
                genres TEXT NOT NULL DEFAULT '[]',
                raw_json TEXT NOT NULL DEFAULT '{}',
                fetched_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expires_at TEXT NOT NULL DEFAULT '',
                checksum TEXT NOT NULL DEFAULT '',
                UNIQUE(media_type, tmdb_id, season_number, episode_number)
            );
            CREATE INDEX IF NOT EXISTS idx_tmdb_items_lookup ON tmdb_items(media_type, release_date, title);
            CREATE INDEX IF NOT EXISTS idx_tmdb_items_parent ON tmdb_items(parent_tmdb_id, media_type);

            CREATE TABLE IF NOT EXISTS tmdb_relations (
                parent_id INTEGER NOT NULL,
                child_id INTEGER NOT NULL,
                parent_type TEXT NOT NULL,
                child_type TEXT NOT NULL,
                season_number INTEGER,
                episode_number INTEGER,
                PRIMARY KEY(parent_id, child_id, parent_type, child_type)
            );

            CREATE TABLE IF NOT EXISTS tmdb_sync_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server_id INTEGER,
                media_types TEXT NOT NULL DEFAULT '[]',
                filters TEXT NOT NULL DEFAULT '{}',
                current_page INTEGER NOT NULL DEFAULT 0,
                total_pages INTEGER NOT NULL DEFAULT 0,
                processed INTEGER NOT NULL DEFAULT 0,
                errors INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'queued',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                FOREIGN KEY(server_id) REFERENCES servers(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS tmdb_sync_pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                media_type TEXT NOT NULL,
                page INTEGER NOT NULL,
                checksum TEXT NOT NULL DEFAULT '',
                fetched_at TEXT NOT NULL,
                UNIQUE(run_id, media_type, page),
                FOREIGN KEY(run_id) REFERENCES tmdb_sync_runs(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS tmdb_images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                media_type TEXT NOT NULL,
                tmdb_id INTEGER NOT NULL,
                season_number INTEGER,
                episode_number INTEGER,
                image_type TEXT NOT NULL,
                source_path TEXT NOT NULL,
                cache_path TEXT NOT NULL,
                mime_type TEXT NOT NULL DEFAULT 'image/jpeg',
                checksum TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                updated_at TEXT NOT NULL,
                UNIQUE(media_type, tmdb_id, season_number, episode_number, image_type)
            );

            CREATE TABLE IF NOT EXISTS plex_inventory_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server_id INTEGER NOT NULL,
                library_id INTEGER NOT NULL,
                rating_key TEXT NOT NULL,
                plex_type TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                year INTEGER,
                parent_rating_key TEXT NOT NULL DEFAULT '',
                season_number INTEGER,
                episode_number INTEGER,
                tmdb_id INTEGER,
                imdb_id TEXT NOT NULL DEFAULT '',
                tvdb_id TEXT NOT NULL DEFAULT '',
                raw_json TEXT NOT NULL DEFAULT '{}',
                scanned_at TEXT NOT NULL,
                UNIQUE(server_id, library_id, rating_key),
                FOREIGN KEY(server_id) REFERENCES servers(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_plex_inventory_ids ON plex_inventory_items(server_id, tmdb_id, plex_type);

            CREATE TABLE IF NOT EXISTS media_libraries (
                server_id INTEGER NOT NULL,
                library_id INTEGER NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                plex_type INTEGER NOT NULL DEFAULT 0,
                animation_mode TEXT NOT NULL DEFAULT 'auto' CHECK (animation_mode IN ('auto','animation','normal')),
                auto_animation INTEGER NOT NULL DEFAULT 0 CHECK (auto_animation IN (0,1)),
                plex_synced_at TEXT,
                tmdb_synced_at TEXT,
                sync_job_id INTEGER,
                sync_status TEXT NOT NULL DEFAULT 'idle',
                sync_error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY(server_id, library_id),
                FOREIGN KEY(server_id) REFERENCES servers(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_media_libraries_server ON media_libraries(server_id, plex_type);

            CREATE TABLE IF NOT EXISTS media_library_recheck_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server_id INTEGER NOT NULL,
                library_id INTEGER NOT NULL,
                rating_key TEXT NOT NULL,
                first_event_at TEXT NOT NULL,
                last_event_at TEXT NOT NULL,
                next_run_at TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','running','succeeded','retrying','failed')),
                job_id INTEGER,
                last_error TEXT NOT NULL DEFAULT '',
                dispatched_event_at TEXT NOT NULL DEFAULT '',
                missing_count INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(server_id, library_id, rating_key),
                FOREIGN KEY(server_id) REFERENCES servers(id) ON DELETE CASCADE,
                FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_media_library_recheck_due
                ON media_library_recheck_requests(status, next_run_at);

            CREATE TABLE IF NOT EXISTS media_library_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server_id INTEGER NOT NULL,
                library_id INTEGER NOT NULL,
                rating_key TEXT NOT NULL,
                plex_type TEXT NOT NULL DEFAULT '',
                parent_rating_key TEXT NOT NULL DEFAULT '',
                season_number INTEGER,
                episode_number INTEGER,
                title TEXT NOT NULL DEFAULT '',
                original_title TEXT NOT NULL DEFAULT '',
                year INTEGER,
                added_at TEXT,
                release_date TEXT NOT NULL DEFAULT '',
                rating REAL,
                audience_rating REAL,
                duration INTEGER,
                content_rating TEXT NOT NULL DEFAULT '',
                genre TEXT NOT NULL DEFAULT '',
                thumb TEXT NOT NULL DEFAULT '',
                art TEXT NOT NULL DEFAULT '',
                raw_json TEXT NOT NULL DEFAULT '{}',
                scanned_at TEXT NOT NULL,
                UNIQUE(server_id, library_id, rating_key),
                FOREIGN KEY(server_id) REFERENCES servers(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_media_library_items_lookup
                ON media_library_items(server_id, library_id, plex_type, parent_rating_key);

            CREATE TABLE IF NOT EXISTS media_library_bulk_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                resolve_job_id INTEGER,
                confirm_job_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(server_id) REFERENCES servers(id) ON DELETE CASCADE,
                FOREIGN KEY(resolve_job_id) REFERENCES jobs(id) ON DELETE SET NULL,
                FOREIGN KEY(confirm_job_id) REFERENCES jobs(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS media_library_bulk_rows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL,
                line_number INTEGER NOT NULL,
                input_text TEXT NOT NULL,
                input_kind TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                media_type TEXT NOT NULL DEFAULT '',
                tmdb_id INTEGER,
                title TEXT NOT NULL DEFAULT '',
                original_title TEXT NOT NULL DEFAULT '',
                year INTEGER,
                poster_path TEXT NOT NULL DEFAULT '',
                genres TEXT NOT NULL DEFAULT '[]',
                animation_kind TEXT NOT NULL DEFAULT '真人',
                candidates TEXT NOT NULL DEFAULT '[]',
                selected INTEGER NOT NULL DEFAULT 0,
                custom_genres TEXT NOT NULL DEFAULT '[]',
                custom_animation_kind TEXT NOT NULL DEFAULT '',
                target_library_id INTEGER,
                message TEXT NOT NULL DEFAULT '',
                raw_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(batch_id, line_number),
                FOREIGN KEY(batch_id) REFERENCES media_library_bulk_batches(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_media_library_bulk_rows_batch ON media_library_bulk_rows(batch_id, line_number);

            CREATE TABLE IF NOT EXISTS media_match_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                media_type TEXT NOT NULL,
                tmdb_id INTEGER NOT NULL,
                season_number INTEGER,
                episode_number INTEGER,
                server_id INTEGER NOT NULL,
                library_id INTEGER,
                status TEXT NOT NULL,
                match_source TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 0,
                plex_rating_key TEXT NOT NULL DEFAULT '',
                details TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL,
                UNIQUE(media_type, tmdb_id, season_number, episode_number, server_id, library_id),
                FOREIGN KEY(server_id) REFERENCES servers(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_media_match_status ON media_match_results(server_id, status, media_type);
            """
        )
        # Added after the initial media-library cache schema.  Keep this as a
        # lightweight migration so existing installations retain their data.
        media_library_columns = {row["name"] for row in db.execute("PRAGMA table_info(media_libraries)").fetchall()}
        if "display_order" not in media_library_columns:
            db.execute("ALTER TABLE media_libraries ADD COLUMN display_order INTEGER NOT NULL DEFAULT 0")
            current_server = None
            index = 0
            for row in db.execute("SELECT server_id, library_id FROM media_libraries ORDER BY server_id, title COLLATE NOCASE").fetchall():
                if row["server_id"] != current_server:
                    current_server = row["server_id"]
                    index = 0
                db.execute("UPDATE media_libraries SET display_order = ? WHERE server_id = ? AND library_id = ?", (index, row["server_id"], row["library_id"]))
                index += 1
        if "auto_recheck_new_episodes" not in media_library_columns:
            db.execute("ALTER TABLE media_libraries ADD COLUMN auto_recheck_new_episodes INTEGER NOT NULL DEFAULT 0")
        recheck_columns = {row["name"] for row in db.execute("PRAGMA table_info(media_library_recheck_requests)").fetchall()}
        if "dispatched_event_at" not in recheck_columns:
            db.execute("ALTER TABLE media_library_recheck_requests ADD COLUMN dispatched_event_at TEXT NOT NULL DEFAULT ''")
        if "missing_count" not in recheck_columns:
            db.execute("ALTER TABLE media_library_recheck_requests ADD COLUMN missing_count INTEGER")
        if "sort_key" not in media_library_columns:
            db.execute("ALTER TABLE media_libraries ADD COLUMN sort_key TEXT NOT NULL DEFAULT ''")
        if "sort_direction" not in media_library_columns:
            db.execute("ALTER TABLE media_libraries ADD COLUMN sort_direction TEXT NOT NULL DEFAULT 'asc'")
        item_columns = {row["name"] for row in db.execute("PRAGMA table_info(media_library_items)").fetchall()}
        for name, definition in {
            "tmdb_media_type": "TEXT NOT NULL DEFAULT ''",
            "tmdb_id": "INTEGER",
            "imdb_id": "TEXT NOT NULL DEFAULT ''",
            "tvdb_id": "TEXT NOT NULL DEFAULT ''",
            "source": "TEXT NOT NULL DEFAULT 'plex'",
            "system_managed": "INTEGER NOT NULL DEFAULT 0",
            "resolution": "TEXT NOT NULL DEFAULT ''",
            "animation_kind": "TEXT NOT NULL DEFAULT ''",
            "system_genres": "TEXT NOT NULL DEFAULT '[]'",
        }.items():
            if name not in item_columns:
                db.execute(f"ALTER TABLE media_library_items ADD COLUMN {name} {definition}")
        tmdb_item_columns = {row["name"] for row in db.execute("PRAGMA table_info(tmdb_items)").fetchall()}
        for name, definition in {"imdb_id": "TEXT NOT NULL DEFAULT ''", "tvdb_id": "TEXT NOT NULL DEFAULT ''"}.items():
            if name not in tmdb_item_columns:
                db.execute(f"ALTER TABLE tmdb_items ADD COLUMN {name} {definition}")
        # Backfill external ids already present in cached Plex payloads.
        for row in db.execute("SELECT server_id, library_id, rating_key, raw_json, imdb_id, tvdb_id FROM media_library_items WHERE imdb_id='' OR tvdb_id='' OR tmdb_id IS NULL").fetchall():
            payload = decode_payload(row["raw_json"])
            ids = payload.get("external_ids") or {}
            if not ids:
                continue
            db.execute("UPDATE media_library_items SET tmdb_id=COALESCE(tmdb_id, ?), tmdb_media_type=CASE WHEN COALESCE(tmdb_media_type,'')='' AND ? IS NOT NULL THEN CASE WHEN plex_type='movie' THEN 'movie' WHEN plex_type='show' THEN 'tv' ELSE '' END ELSE tmdb_media_type END, imdb_id=CASE WHEN imdb_id='' THEN ? ELSE imdb_id END, tvdb_id=CASE WHEN tvdb_id='' THEN ? ELSE tvdb_id END WHERE server_id=? AND library_id=? AND rating_key=?", (int(ids["tmdb"]) if ids.get("tmdb") else None, int(ids["tmdb"]) if ids.get("tmdb") else None, str(ids.get("imdb") or ""), str(ids.get("tvdb") or ""), row["server_id"], row["library_id"], row["rating_key"]))
        db.execute(
            "INSERT INTO schema_meta (id, version, created_at) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET version = excluded.version",
            (SCHEMA_VERSION, utcnow()),
        )
        _seed_tags(db)
    try:
        os.chmod(db_file, 0o600)
    except OSError:
        pass


def _seed_tags(db: sqlite3.Connection) -> None:
    count = db.execute("SELECT COUNT(*) AS count FROM tag_mappings").fetchone()["count"]
    if count or not TEMPLATE_TAGS_FILE.exists():
        return
    tags = json.loads(TEMPLATE_TAGS_FILE.read_text(encoding="utf-8"))
    db.executemany(
        "INSERT INTO tag_mappings (source, target) VALUES (?, ?)",
        [(str(source), str(target)) for source, target in tags.items()],
    )


def reset_database(db_file: Path | None = None, backup: bool = True) -> Path | None:
    db_file = Path(db_file or DB_FILE)
    backup_path: Path | None = None
    if db_file.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = db_file.with_name(f"{db_file.name}.backup-{stamp}")
        if backup:
            shutil.copy2(str(db_file), str(backup_path))
            db_file.unlink()
        else:
            db_file.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{db_file}{suffix}")
        if sidecar.exists():
            if backup and backup_path is not None:
                shutil.copy2(str(sidecar), f"{backup_path}{suffix}")
                sidecar.unlink()
            else:
                sidecar.unlink()
    init_db(db_file)
    return backup_path


def server_config_from_row(
    row: sqlite3.Row | Dict[str, Any], db_file: Path | None = None
) -> ServerConfig:
    data = dict(row)
    client_identifier = get_setting("plex_client_identifier", db_file) or "media-server-manager"
    return ServerConfig(
        name=data.get("name", ""),
        address=data["address"],
        token=data["token"],
        skip_libraries=split_skip_libraries(data.get("skip_libraries", "")),
        pinyin_mode=data.get("pinyin_mode", "first_letter"),
        client_identifier=client_identifier,
    )


def decode_payload(value: str | None) -> Dict[str, Any]:
    try:
        payload = json.loads(value or "{}")
        return payload if isinstance(payload, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def get_setting(key: str, db_file: Path | None = None) -> str:
    with get_db(db_file) as db:
        row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else ""


def set_setting(key: str, value: str, db_file: Path | None = None) -> None:
    with get_db(db_file) as db:
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def load_tags(db_file: Path | None = None) -> Dict[str, str]:
    with get_db(db_file) as db:
        rows = db.execute("SELECT source, target FROM tag_mappings ORDER BY source").fetchall()
    return {row["source"]: row["target"] for row in rows}


def save_tags(tags: Dict[str, str], db_file: Path | None = None) -> None:
    with get_db(db_file) as db:
        db.execute("DELETE FROM tag_mappings")
        db.executemany(
            "INSERT INTO tag_mappings (source, target) VALUES (?, ?)",
            sorted((source, target) for source, target in tags.items()),
        )
