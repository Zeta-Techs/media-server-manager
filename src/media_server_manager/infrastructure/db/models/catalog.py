from __future__ import annotations

from sqlalchemy import Column, ForeignKey, Integer, Table, Text

from ..session import Base


class TmdbImages(Base):
    __table__ = Table(
        "tmdb_images", Base.metadata,
        Column("id", Integer(), primary_key=True),
        Column("media_type", Text(), nullable=False),
        Column("tmdb_id", Integer(), nullable=False),
        Column("season_number", Integer()),
        Column("episode_number", Integer()),
        Column("image_type", Text(), nullable=False),
        Column("source_path", Text(), nullable=False),
        Column("cache_path", Text(), nullable=False),
        Column("mime_type", Text(), nullable=False),
        Column("checksum", Text(), nullable=False),
        Column("status", Text(), nullable=False),
        Column("updated_at", Text(), nullable=False),
    )

class TmdbItems(Base):
    __table__ = Table(
        "tmdb_items", Base.metadata,
        Column("id", Integer(), primary_key=True),
        Column("tmdb_id", Integer(), nullable=False),
        Column("media_type", Text(), nullable=False),
        Column("parent_tmdb_id", Integer()),
        Column("season_number", Integer()),
        Column("episode_number", Integer()),
        Column("title", Text(), nullable=False),
        Column("original_title", Text(), nullable=False),
        Column("release_date", Text(), nullable=False),
        Column("status", Text(), nullable=False),
        Column("overview", Text(), nullable=False),
        Column("poster_path", Text(), nullable=False),
        Column("backdrop_path", Text(), nullable=False),
        Column("still_path", Text(), nullable=False),
        Column("genres", Text(), nullable=False),
        Column("raw_json", Text(), nullable=False),
        Column("fetched_at", Text(), nullable=False),
        Column("updated_at", Text(), nullable=False),
        Column("expires_at", Text(), nullable=False),
        Column("checksum", Text(), nullable=False),
        Column("imdb_id", Text(), nullable=False),
        Column("tvdb_id", Text(), nullable=False),
    )

class TmdbRelations(Base):
    __table__ = Table(
        "tmdb_relations", Base.metadata,
        Column("parent_id", Integer(), primary_key=True, nullable=False),
        Column("child_id", Integer(), primary_key=True, nullable=False),
        Column("parent_type", Text(), primary_key=True, nullable=False),
        Column("child_type", Text(), primary_key=True, nullable=False),
        Column("season_number", Integer()),
        Column("episode_number", Integer()),
    )

class TmdbSyncPages(Base):
    __table__ = Table(
        "tmdb_sync_pages", Base.metadata,
        Column("id", Integer(), primary_key=True),
        Column("run_id", Integer(), ForeignKey("tmdb_sync_runs.id", ondelete="CASCADE"), nullable=False),
        Column("media_type", Text(), nullable=False),
        Column("page", Integer(), nullable=False),
        Column("checksum", Text(), nullable=False),
        Column("fetched_at", Text(), nullable=False),
    )

class TmdbSyncRuns(Base):
    __table__ = Table(
        "tmdb_sync_runs", Base.metadata,
        Column("id", Integer(), primary_key=True),
        Column("server_id", Integer(), ForeignKey("servers.id", ondelete="SET NULL")),
        Column("media_types", Text(), nullable=False),
        Column("filters", Text(), nullable=False),
        Column("current_page", Integer(), nullable=False),
        Column("total_pages", Integer(), nullable=False),
        Column("processed", Integer(), nullable=False),
        Column("errors", Integer(), nullable=False),
        Column("status", Text(), nullable=False),
        Column("error", Text(), nullable=False),
        Column("created_at", Text(), nullable=False),
        Column("started_at", Text()),
        Column("finished_at", Text()),
    )
