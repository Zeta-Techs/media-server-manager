from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from media_server_manager.infrastructure.db.legacy_schema import init_db
from media_server_manager.infrastructure.db.models import (
    PlexInventoryItems,
    Server,
    TmdbImages,
    TmdbItems,
    TmdbRelations,
)
from media_server_manager.infrastructure.db.repositories.catalog_workflow import CatalogWorkflowRepository
from media_server_manager.infrastructure.db.session import Database


def test_catalog_workflow_repository_uses_natural_keys(tmp_path: Path) -> None:
    database_path = tmp_path / "data" / "media_server_manager.sqlite3"
    database_path.parent.mkdir(parents=True)
    init_db(database_path)
    database = Database.for_path(database_path)
    try:
        with database.transaction() as uow:
            now = datetime.now(timezone.utc)
            uow.session.add(
                Server(
                    id=1,
                    name="Plex",
                    address="http://plex",
                    token="token",
                    webhook_secret="secret",
                    created_at=now,
                    updated_at=now,
                )
            )
            uow.session.flush()
            repository = CatalogWorkflowRepository(uow.session)
            repository.upsert_tmdb_item("movie", 10, {"title": "旧标题"}, now="2026-01-01T00:00:00Z")
            repository.upsert_tmdb_item("movie", 10, {"title": "新标题"}, now="2026-01-01T00:00:01Z")
            repository.upsert_relation(10, 10, "tv", "season", season_number=1)
            repository.upsert_relation(10, 10, "tv", "season", season_number=1)
            repository.upsert_image(
                {
                    "media_type": "movie",
                    "tmdb_id": 10,
                    "season_number": None,
                    "episode_number": None,
                    "image_type": "poster",
                    "source_path": "/poster.jpg",
                    "cache_path": "data/cache/tmdb/movie/10/poster.jpg",
                    "mime_type": "image/jpeg",
                    "checksum": "abc",
                    "status": "ready",
                    "updated_at": "2026-01-01T00:00:00Z",
                }
            )
            repository.upsert_inventory(
                {
                    "server_id": 1,
                    "library_id": 2,
                    "rating_key": "movie-10",
                    "plex_type": "movie",
                    "title": "Movie",
                    "year": 2026,
                    "parent_rating_key": "",
                    "season_number": None,
                    "episode_number": None,
                    "tmdb_id": 10,
                    "imdb_id": "",
                    "tvdb_id": "",
                    "raw_json": "{}",
                    "scanned_at": "2026-01-01T00:00:00Z",
                }
            )
            repository.upsert_inventory(
                {
                    "server_id": 1,
                    "library_id": 2,
                    "rating_key": "movie-10",
                    "plex_type": "movie",
                    "title": "Movie updated",
                    "year": 2026,
                    "parent_rating_key": "",
                    "season_number": None,
                    "episode_number": None,
                    "tmdb_id": 10,
                    "imdb_id": "",
                    "tvdb_id": "",
                    "raw_json": "{}",
                    "scanned_at": "2026-01-01T00:00:01Z",
                }
            )
        with database.session() as session:
            assert session.scalar(select(TmdbItems.__table__.c.title)) == "新标题"
            assert session.scalar(select(TmdbRelations.__table__.c.parent_id).limit(2)) == 10
            assert len(session.execute(select(TmdbRelations.__table__)).all()) == 1
            assert len(session.execute(select(TmdbImages.__table__)).all()) == 1
            assert session.scalar(select(PlexInventoryItems.__table__.c.title)) == "Movie updated"
    finally:
        database.engine.dispose()
