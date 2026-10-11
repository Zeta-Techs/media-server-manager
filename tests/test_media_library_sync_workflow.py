from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from media_server_manager.application.media_operations import sync as media_sync
from media_server_manager.infrastructure.db.legacy_schema import init_db
from media_server_manager.infrastructure.db.repositories.auth import AuthRepository
from media_server_manager.infrastructure.db.session import Database


class _FakePlex:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def list_library(self):
        return [[1, 2, "Shows"]]

    def list_library_items(self, _library_id, _plex_type):
        return [{"ratingKey": "show-1", "type": "show", "title": "Show", "year": 2024}]

    def get_metadata(self, rating_key):
        return {
            "ratingKey": rating_key,
            "type": "show",
            "title": "Show",
            "year": 2024,
            "Guid": [{"id": "tmdb://42"}],
            "Genre": [{"tag": "Drama"}],
        }

    def get_children(self, _rating_key):
        return [{"ratingKey": "season-1", "type": "season", "title": "Season 1", "index": 1}]

    def list_show_episodes(self, _rating_key):
        return [{"ratingKey": "episode-1", "type": "episode", "title": "Episode 1", "parentIndex": 1, "index": 1}]


def _database(tmp_path: Path) -> Database:
    database_path = tmp_path / "data" / "media_server_manager.sqlite3"
    database_path.parent.mkdir(parents=True)
    init_db(database_path)
    database = Database.for_path(database_path)
    now = datetime.now(timezone.utc)
    with database.transaction() as uow:
        from media_server_manager.infrastructure.db.models import Server

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
        AuthRepository(uow.session).set_setting("tmdb_api_key", "test-key")
    return database


def test_media_library_sync_persists_plex_projection(tmp_path: Path, monkeypatch) -> None:
    database = _database(tmp_path)
    monkeypatch.setattr(media_sync, "PlexServer", _FakePlex)
    try:
        assert media_sync.sync_media_library(database, 1, 1) == 1
        with database.session() as session:
            rows = session.execute(
                text("SELECT rating_key, plex_type, parent_rating_key FROM media_library_items ORDER BY id")
            ).mappings().all()
            library = session.execute(text("SELECT sync_status, plex_synced_at FROM media_libraries")).mappings().one()
        assert [row["rating_key"] for row in rows] == ["show-1", "season-1", "episode-1"]
        assert rows[2]["parent_rating_key"] == "show-1"
        assert library["sync_status"] == "idle"
        assert library["plex_synced_at"]
    finally:
        database.engine.dispose()


def test_media_library_tmdb_sync_creates_missing_episode_placeholder(tmp_path: Path, monkeypatch) -> None:
    database = _database(tmp_path)
    monkeypatch.setattr(media_sync, "PlexServer", _FakePlex)

    class _FakeTmdb:
        def __init__(self, _token):
            pass

        def details(self, _media_type, _tmdb_id):
            return {"id": 42, "name": "Show", "first_air_date": "2024-01-01", "seasons": [{"season_number": 1}]}

        def season_details(self, _tmdb_id, _season_number):
            return {"name": "Season 1", "episodes": [{"episode_number": 1, "name": "Episode 1", "air_date": "2024-01-01"}, {"episode_number": 2, "name": "Episode 2", "air_date": "2024-01-08"}]}

        def search_tv(self, *_args, **_kwargs):
            return []

    monkeypatch.setattr(media_sync, "TMDBClient", _FakeTmdb)
    try:
        media_sync.sync_media_library(database, 1, 1)
        assert media_sync.sync_media_library_tmdb(database, 1, 1) == 1
        with database.session() as session:
            row = session.execute(
                text("SELECT raw_json FROM media_library_items WHERE rating_key = 'tmdb:42:1:2'")
            ).scalar_one()
            payload = json.loads(row)
        assert payload["expected"] is True
        assert payload["tmdb"]["episode_number"] == 2
    finally:
        database.engine.dispose()
