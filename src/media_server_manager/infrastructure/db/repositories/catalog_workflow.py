"""Typed persistence operations for catalog and Plex synchronization.

The public compatibility functions in :mod:`catalog_sync` still accept a
database path for older integrations.  New application services use this
repository instead, so the normal synchronization path owns one SQLAlchemy
session per transaction and never exposes a DB-API cursor.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..models import PlexInventoryItems, TmdbImages, TmdbItems, TmdbRelations


class CatalogWorkflowRepository:
    """Persistence gateway for TMDB and Plex synchronization workflows."""

    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _match_item(media_type: str, tmdb_id: int, season_number: int | None, episode_number: int | None):
        table = TmdbItems.__table__
        conditions = [table.c.media_type == media_type, table.c.tmdb_id == tmdb_id]
        for column, value in ((table.c.season_number, season_number), (table.c.episode_number, episode_number)):
            conditions.append(column.is_(None) if value is None else column == value)
        return conditions

    def upsert_tmdb_item(
        self,
        media_type: str,
        tmdb_id: int,
        payload: dict[str, Any],
        *,
        parent_id: int | None = None,
        season_number: int | None = None,
        episode_number: int | None = None,
        now: str,
    ) -> int:
        """Insert or update one catalog item using its natural key."""

        table = TmdbItems.__table__
        external_ids = payload.get("external_ids") or {}
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        values: dict[str, Any] = {
            "tmdb_id": tmdb_id,
            "media_type": media_type,
            "imdb_id": str(external_ids.get("imdb_id") or payload.get("imdb_id") or ""),
            "tvdb_id": str(external_ids.get("tvdb_id") or payload.get("tvdb_id") or ""),
            "parent_tmdb_id": parent_id,
            "season_number": season_number,
            "episode_number": episode_number,
            "title": payload.get("title") or payload.get("name") or "",
            "original_title": payload.get("original_title") or payload.get("original_name") or "",
            "release_date": payload.get("release_date") or payload.get("first_air_date") or payload.get("air_date") or "",
            "status": payload.get("status") or "",
            "overview": payload.get("overview") or "",
            "poster_path": payload.get("poster_path") or "",
            "backdrop_path": payload.get("backdrop_path") or "",
            "still_path": payload.get("still_path") or "",
            "genres": json.dumps(payload.get("genres") or payload.get("genre_ids") or [], ensure_ascii=False),
            "raw_json": raw,
            "fetched_at": now,
            "updated_at": now,
            "expires_at": "",
            "checksum": hashlib.sha256(raw.encode()).hexdigest(),
        }
        existing = self.session.scalar(select(table.c.id).where(*self._match_item(media_type, tmdb_id, season_number, episode_number)))
        if existing is None:
            result = self.session.execute(table.insert().values(**values))
            return int(result.inserted_primary_key[0])
        self.session.execute(update(table).where(table.c.id == existing).values(**values))
        return int(existing)

    def upsert_relation(
        self,
        parent_id: int,
        child_id: int,
        parent_type: str,
        child_type: str,
        season_number: int | None = None,
        episode_number: int | None = None,
    ) -> None:
        table = TmdbRelations.__table__
        key = dict(parent_id=parent_id, child_id=child_id, parent_type=parent_type, child_type=child_type)
        if self.session.scalar(select(table.c.parent_id).where(*[table.c[key_name] == value for key_name, value in key.items()])) is None:
            self.session.execute(table.insert().values(**key, season_number=season_number, episode_number=episode_number))

    def upsert_image(self, values: dict[str, Any]) -> None:
        table = TmdbImages.__table__
        key_names = ("media_type", "tmdb_id", "season_number", "episode_number", "image_type")
        conditions = []
        for name in key_names:
            value = values[name]
            column = table.c[name]
            conditions.append(column.is_(None) if value is None else column == value)
        existing = self.session.scalar(select(table.c.id).where(*conditions))
        if existing is None:
            self.session.execute(table.insert().values(**values))
        else:
            self.session.execute(update(table).where(table.c.id == existing).values(**values))

    def upsert_inventory(self, values: dict[str, Any]) -> None:
        table = PlexInventoryItems.__table__
        existing = self.session.scalar(
            select(table.c.id).where(
                table.c.server_id == values["server_id"],
                table.c.library_id == values["library_id"],
                table.c.rating_key == values["rating_key"],
            )
        )
        if existing is None:
            self.session.execute(table.insert().values(**values))
        else:
            self.session.execute(update(table).where(table.c.id == existing).values(**values))


__all__ = ["CatalogWorkflowRepository"]
