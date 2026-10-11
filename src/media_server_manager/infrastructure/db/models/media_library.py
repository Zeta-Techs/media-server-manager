from __future__ import annotations

from sqlalchemy import Column, Float, ForeignKey, Integer, Table, Text

from ..session import Base


class AnimeManualOverrides(Base):
    __table__ = Table(
        "anime_manual_overrides", Base.metadata,
        Column("tmdb_id", Integer(), primary_key=True),
        Column("year", Integer(), nullable=False),
        Column("quarter", Text(), nullable=False),
        Column("reason", Text(), nullable=False),
        Column("created_at", Text(), nullable=False),
        Column("updated_at", Text(), nullable=False),
    )

class AnimeMatchResults(Base):
    __table__ = Table(
        "anime_match_results", Base.metadata,
        Column("id", Integer(), primary_key=True),
        Column("tmdb_id", Integer(), nullable=False),
        Column("media_type", Text(), nullable=False),
        Column("season_number", Integer()),
        Column("episode_number", Integer()),
        Column("server_id", Integer(), ForeignKey("servers.id", ondelete="CASCADE"), nullable=False),
        Column("library_id", Integer()),
        Column("status", Text(), nullable=False),
        Column("match_source", Text(), nullable=False),
        Column("confidence", Float(), nullable=False),
        Column("plex_rating_key", Text(), nullable=False),
        Column("details", Text(), nullable=False),
        Column("updated_at", Text(), nullable=False),
    )

class AnimeSeasonItems(Base):
    __table__ = Table(
        "anime_season_items", Base.metadata,
        Column("season_id", Integer(), ForeignKey("anime_seasons.id", ondelete="CASCADE"), primary_key=True, nullable=False),
        Column("tmdb_id", Integer(), primary_key=True, nullable=False),
        Column("discover_rank", Integer(), nullable=False),
        Column("popularity", Float(), nullable=False),
        Column("vote_average", Float(), nullable=False),
        Column("first_discovered_at", Text(), nullable=False),
        Column("updated_at", Text(), nullable=False),
    )

class AnimeSeasons(Base):
    __table__ = Table(
        "anime_seasons", Base.metadata,
        Column("id", Integer(), primary_key=True),
        Column("year", Integer(), nullable=False),
        Column("quarter", Text(), nullable=False),
        Column("start_date", Text(), nullable=False),
        Column("end_date", Text(), nullable=False),
        Column("status", Text(), nullable=False),
        Column("sync_policy", Text(), nullable=False),
        Column("last_synced_at", Text()),
        Column("next_sync_at", Text()),
        Column("created_at", Text(), nullable=False),
        Column("updated_at", Text(), nullable=False),
    )

class AnimeShowSchedule(Base):
    __table__ = Table(
        "anime_show_schedule", Base.metadata,
        Column("tmdb_id", Integer(), primary_key=True),
        Column("last_aired_episode_date", Text()),
        Column("last_aired_season", Integer()),
        Column("last_aired_episode", Integer()),
        Column("next_air_date", Text()),
        Column("next_air_season", Integer()),
        Column("next_air_episode", Integer()),
        Column("schedule_source", Text(), nullable=False),
        Column("last_schedule_checked_at", Text()),
        Column("next_sync_at", Text()),
        Column("active_until", Text()),
        Column("sync_mode", Text(), nullable=False),
        Column("schedule_status", Text(), nullable=False),
        Column("updated_at", Text(), nullable=False),
    )

class AnimeSyncEvents(Base):
    __table__ = Table(
        "anime_sync_events", Base.metadata,
        Column("id", Integer(), primary_key=True),
        Column("tmdb_id", Integer(), nullable=False),
        Column("event_type", Text(), nullable=False),
        Column("scheduled_at", Text(), nullable=False),
        Column("executed_at", Text()),
        Column("status", Text(), nullable=False),
        Column("error", Text(), nullable=False),
        Column("job_id", Integer(), ForeignKey("jobs.id", ondelete="CASCADE")),
    )

class MediaLibraryBulkBatches(Base):
    __table__ = Table(
        "media_library_bulk_batches", Base.metadata,
        Column("id", Integer(), primary_key=True),
        Column("server_id", Integer(), ForeignKey("servers.id", ondelete="CASCADE"), nullable=False),
        Column("status", Text(), nullable=False),
        Column("resolve_job_id", Integer(), ForeignKey("jobs.id", ondelete="SET NULL")),
        Column("confirm_job_id", Integer(), ForeignKey("jobs.id", ondelete="SET NULL")),
        Column("created_at", Text(), nullable=False),
        Column("updated_at", Text(), nullable=False),
        Column("error", Text(), nullable=False),
    )

class MediaLibraryBulkRows(Base):
    __table__ = Table(
        "media_library_bulk_rows", Base.metadata,
        Column("id", Integer(), primary_key=True),
        Column("batch_id", Integer(), ForeignKey("media_library_bulk_batches.id", ondelete="CASCADE"), nullable=False),
        Column("line_number", Integer(), nullable=False),
        Column("input_text", Text(), nullable=False),
        Column("input_kind", Text(), nullable=False),
        Column("status", Text(), nullable=False),
        Column("media_type", Text(), nullable=False),
        Column("tmdb_id", Integer()),
        Column("title", Text(), nullable=False),
        Column("original_title", Text(), nullable=False),
        Column("year", Integer()),
        Column("poster_path", Text(), nullable=False),
        Column("genres", Text(), nullable=False),
        Column("animation_kind", Text(), nullable=False),
        Column("candidates", Text(), nullable=False),
        Column("selected", Integer(), nullable=False),
        Column("custom_genres", Text(), nullable=False),
        Column("custom_animation_kind", Text(), nullable=False),
        Column("target_library_id", Integer()),
        Column("message", Text(), nullable=False),
        Column("raw_json", Text(), nullable=False),
        Column("created_at", Text(), nullable=False),
        Column("updated_at", Text(), nullable=False),
    )

class MediaLibraryItems(Base):
    __table__ = Table(
        "media_library_items", Base.metadata,
        Column("id", Integer(), primary_key=True),
        Column("server_id", Integer(), ForeignKey("servers.id", ondelete="CASCADE"), nullable=False),
        Column("library_id", Integer(), nullable=False),
        Column("rating_key", Text(), nullable=False),
        Column("plex_type", Text(), nullable=False),
        Column("parent_rating_key", Text(), nullable=False),
        Column("season_number", Integer()),
        Column("episode_number", Integer()),
        Column("title", Text(), nullable=False),
        Column("original_title", Text(), nullable=False),
        Column("year", Integer()),
        Column("added_at", Text()),
        Column("release_date", Text(), nullable=False),
        Column("rating", Float()),
        Column("audience_rating", Float()),
        Column("duration", Integer()),
        Column("content_rating", Text(), nullable=False),
        Column("genre", Text(), nullable=False),
        Column("thumb", Text(), nullable=False),
        Column("art", Text(), nullable=False),
        Column("raw_json", Text(), nullable=False),
        Column("scanned_at", Text(), nullable=False),
        Column("tmdb_media_type", Text(), nullable=False),
        Column("tmdb_id", Integer()),
        Column("imdb_id", Text(), nullable=False),
        Column("tvdb_id", Text(), nullable=False),
        Column("source", Text(), nullable=False),
        Column("system_managed", Integer(), nullable=False),
        Column("resolution", Text(), nullable=False),
        Column("animation_kind", Text(), nullable=False),
        Column("system_genres", Text(), nullable=False),
    )

class MediaLibraryRecheckRequests(Base):
    __table__ = Table(
        "media_library_recheck_requests", Base.metadata,
        Column("id", Integer(), primary_key=True),
        Column("server_id", Integer(), ForeignKey("servers.id", ondelete="CASCADE"), nullable=False),
        Column("library_id", Integer(), nullable=False),
        Column("rating_key", Text(), nullable=False),
        Column("first_event_at", Text(), nullable=False),
        Column("last_event_at", Text(), nullable=False),
        Column("next_run_at", Text(), nullable=False),
        Column("attempt", Integer(), nullable=False),
        Column("status", Text(), nullable=False),
        Column("job_id", Integer(), ForeignKey("jobs.id", ondelete="SET NULL")),
        Column("last_error", Text(), nullable=False),
        Column("dispatched_event_at", Text(), nullable=False),
        Column("missing_count", Integer()),
        Column("created_at", Text(), nullable=False),
        Column("updated_at", Text(), nullable=False),
    )

class MediaMatchResults(Base):
    __table__ = Table(
        "media_match_results", Base.metadata,
        Column("id", Integer(), primary_key=True),
        Column("media_type", Text(), nullable=False),
        Column("tmdb_id", Integer(), nullable=False),
        Column("season_number", Integer()),
        Column("episode_number", Integer()),
        Column("server_id", Integer(), ForeignKey("servers.id", ondelete="CASCADE"), nullable=False),
        Column("library_id", Integer()),
        Column("status", Text(), nullable=False),
        Column("match_source", Text(), nullable=False),
        Column("confidence", Float(), nullable=False),
        Column("plex_rating_key", Text(), nullable=False),
        Column("details", Text(), nullable=False),
        Column("updated_at", Text(), nullable=False),
    )

class PlexInventoryItems(Base):
    __table__ = Table(
        "plex_inventory_items", Base.metadata,
        Column("id", Integer(), primary_key=True),
        Column("server_id", Integer(), ForeignKey("servers.id", ondelete="CASCADE"), nullable=False),
        Column("library_id", Integer(), nullable=False),
        Column("rating_key", Text(), nullable=False),
        Column("plex_type", Text(), nullable=False),
        Column("title", Text(), nullable=False),
        Column("year", Integer()),
        Column("parent_rating_key", Text(), nullable=False),
        Column("season_number", Integer()),
        Column("episode_number", Integer()),
        Column("tmdb_id", Integer()),
        Column("imdb_id", Text(), nullable=False),
        Column("tvdb_id", Text(), nullable=False),
        Column("raw_json", Text(), nullable=False),
        Column("scanned_at", Text(), nullable=False),
    )
