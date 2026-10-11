"""Typed persistence gateway for Plex/TMDB media-library synchronization."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import delete, insert, select, update
from sqlalchemy.orm import Session

from ..models import (
    MediaLibraries,
    MediaLibraryItems,
    MediaMatchResults,
    PlexInventoryItems,
    Server,
    TmdbItems,
)


class MediaLibrarySyncRepository:
    """Own all writes performed by the media-library synchronization use cases."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.libraries = MediaLibraries.__table__
        self.items = MediaLibraryItems.__table__
        self.matches = MediaMatchResults.__table__

    def enabled_server(self, server_id: int) -> dict[str, Any] | None:
        row = self.session.execute(
            select(Server.__table__).where(Server.__table__.c.id == server_id, Server.__table__.c.enabled == 1)
        ).mappings().first()
        return dict(row) if row else None

    def upsert_library(self, server_id: int, library_id: int, title: str, plex_type: int, auto_animation: bool, now: str) -> None:
        values = {
            "server_id": server_id,
            "library_id": library_id,
            "title": title,
            "plex_type": plex_type,
            "auto_animation": 1 if auto_animation else 0,
            "sync_status": "running",
            "sync_error": "",
            "updated_at": now,
        }
        current = self.session.execute(
            select(self.libraries.c.server_id).where(
                self.libraries.c.server_id == server_id,
                self.libraries.c.library_id == library_id,
            )
        ).first()
        if current is None:
            self.session.execute(insert(self.libraries).values(**values))
        else:
            self.session.execute(
                update(self.libraries)
                .where(self.libraries.c.server_id == server_id, self.libraries.c.library_id == library_id)
                .values(**values)
            )

    def item(self, server_id: int, library_id: int, rating_key: str) -> dict[str, Any] | None:
        row = self.session.execute(
            select(self.items).where(
                self.items.c.server_id == server_id,
                self.items.c.library_id == library_id,
                self.items.c.rating_key == rating_key,
            )
        ).mappings().first()
        return dict(row) if row else None

    def upsert_plex_item(
        self,
        server_id: int,
        library_id: int,
        metadata: dict[str, Any],
        *,
        plex_type: str | None = None,
        parent_rating_key: str = "",
        season_number: int | None = None,
        episode_number: int | None = None,
        values: dict[str, Any],
    ) -> None:
        """Upsert a Plex projection while preserving known TMDB identifiers."""

        rating_key = str(metadata.get("ratingKey") or "")
        previous = self.item(server_id, library_id, rating_key)
        previous_raw = _decode_json(previous.get("raw_json") if previous else "{}")
        raw_payload = {**previous_raw, **metadata}
        raw_payload["external_ids"] = {
            **(previous_raw.get("external_ids") or {}),
            **(metadata.get("external_ids") or {}),
            **(values.pop("external_ids", {}) or {}),
        }
        values.update(
            {
                "server_id": server_id,
                "library_id": library_id,
                "rating_key": rating_key,
                "plex_type": plex_type or str(metadata.get("type") or ""),
                "parent_rating_key": parent_rating_key,
                "season_number": season_number if season_number is not None else metadata.get("parentIndex"),
                "episode_number": episode_number if episode_number is not None else metadata.get("index"),
                "raw_json": json.dumps(raw_payload, ensure_ascii=False),
            }
        )
        if previous is not None:
            for field in ("release_date", "tmdb_media_type", "tmdb_id", "imdb_id", "tvdb_id", "resolution"):
                if not values.get(field) and previous.get(field) is not None:
                    values[field] = previous[field]
        if previous is None:
            self.session.execute(insert(self.items).values(**values))
        else:
            self.session.execute(
                update(self.items)
                .where(self.items.c.id == previous["id"])
                .values(**values)
            )
        tmdb_id = values.get("tmdb_id")
        normalized_type = str(values.get("plex_type") or "")
        if tmdb_id and normalized_type in {"movie", "show"} and rating_key and not rating_key.startswith("tmdb:"):
            media_type = "movie" if normalized_type == "movie" else "tv"
            managed = self.session.execute(
                select(self.items.c.rating_key).where(
                    self.items.c.server_id == server_id,
                    self.items.c.library_id == library_id,
                    self.items.c.tmdb_id == int(tmdb_id),
                    self.items.c.tmdb_media_type == media_type,
                    self.items.c.system_managed == 1,
                    self.items.c.rating_key != rating_key,
                )
            ).scalars().all()
            for managed_key in managed:
                self.session.execute(
                    delete(self.items).where(
                        self.items.c.server_id == server_id,
                        self.items.c.library_id == library_id,
                        (self.items.c.parent_rating_key == managed_key) | (self.items.c.rating_key == managed_key),
                    )
                )

    def remove_expected_episode(self, server_id: int, library_id: int, parent_rating_key: str, season: int, episode: int) -> None:
        self.session.execute(
            delete(self.items).where(
                self.items.c.server_id == server_id,
                self.items.c.library_id == library_id,
                self.items.c.parent_rating_key == parent_rating_key,
                self.items.c.plex_type == "episode",
                self.items.c.season_number == season,
                self.items.c.episode_number == episode,
                self.items.c.rating_key.like("tmdb:%"),
            )
        )

    def finish_plex(self, server_id: int, library_id: int, seen_keys: set[str], restricted: bool, now: str) -> None:
        if not restricted:
            rows = self.session.execute(
                select(self.items.c.rating_key).where(
                    self.items.c.server_id == server_id,
                    self.items.c.library_id == library_id,
                    ~self.items.c.rating_key.like("tmdb:%"),
                )
            ).scalars().all()
            stale = [key for key in rows if key not in seen_keys]
            if stale:
                self.session.execute(
                    delete(self.items).where(
                        self.items.c.server_id == server_id,
                        self.items.c.library_id == library_id,
                        self.items.c.rating_key.in_(stale),
                    )
                )
        self.session.execute(
            update(self.libraries)
            .where(self.libraries.c.server_id == server_id, self.libraries.c.library_id == library_id)
            .values(plex_synced_at=now, sync_status="idle", sync_error="", updated_at=now)
        )

    def sync_candidates(self, server_id: int, library_id: int, rating_keys: set[str] | None) -> list[dict[str, Any]]:
        query = select(self.items).where(
            self.items.c.server_id == server_id,
            self.items.c.library_id == library_id,
            self.items.c.plex_type.in_(["show", "movie"]),
        )
        if rating_keys:
            query = query.where(self.items.c.rating_key.in_(sorted(rating_keys)))
        return [dict(row) for row in self.session.execute(query.order_by(self.items.c.id)).mappings()]

    def update_tmdb_projection(self, item: dict[str, Any], payload: dict[str, Any], details: dict[str, Any], now: str) -> None:
        merged = {**payload, "tmdb": details}
        release = str(details.get("first_air_date") or details.get("release_date") or "")
        self.session.execute(
            update(self.items)
            .where(
                self.items.c.server_id == item["server_id"],
                self.items.c.library_id == item["library_id"],
                self.items.c.rating_key == item["rating_key"],
            )
            .values(
                raw_json=json.dumps(merged, ensure_ascii=False),
                release_date=release or item.get("release_date") or "",
                tmdb_id=details.get("id") or item.get("tmdb_id"),
                tmdb_media_type="tv" if item["plex_type"] == "show" else "movie",
                scanned_at=now,
            )
        )

    def season_placeholder(self, item: dict[str, Any], tmdb_id: int, season_number: int, payload: dict[str, Any], now: str) -> None:
        parent = str(item["rating_key"])
        key = f"tmdb-season:{tmdb_id}:{season_number}"
        exists = self.session.scalar(
            select(self.items.c.id).where(
                self.items.c.server_id == item["server_id"],
                self.items.c.library_id == item["library_id"],
                self.items.c.parent_rating_key == parent,
                self.items.c.plex_type == "season",
                self.items.c.season_number == season_number,
            )
            )
        if exists is None:
            first_air = str((payload.get("episodes") or [{}])[0].get("air_date") or "")[:10]
            self.session.execute(
                insert(self.items).values(
                    server_id=item["server_id"], library_id=item["library_id"], rating_key=key,
                    plex_type="season", parent_rating_key=parent, season_number=season_number,
                    title=str(payload.get("name") or f"第 {season_number} 季"), release_date=first_air,
                    raw_json=json.dumps({"tmdb": payload, "expected": True}, ensure_ascii=False), scanned_at=now,
                )
            )

    def cached_season(self, item: dict[str, Any], season_number: int) -> dict[str, Any] | None:
        row = self.session.execute(
            select(self.items).where(
                self.items.c.server_id == item["server_id"],
                self.items.c.library_id == item["library_id"],
                self.items.c.parent_rating_key == item["rating_key"],
                self.items.c.plex_type == "season",
                self.items.c.season_number == season_number,
            ).order_by(self.items.c.id).limit(1)
        ).mappings().first()
        return dict(row) if row else None

    def real_episode_exists(self, item: dict[str, Any], season_number: int, episode_number: int) -> bool:
        return self.session.scalar(
            select(self.items.c.id).where(
                self.items.c.server_id == item["server_id"],
                self.items.c.library_id == item["library_id"],
                self.items.c.parent_rating_key == item["rating_key"],
                self.items.c.plex_type == "episode",
                self.items.c.season_number == season_number,
                self.items.c.episode_number == episode_number,
                ~self.items.c.rating_key.like("tmdb:%"),
            ).limit(1)
        ) is not None

    def episode_placeholder(self, item: dict[str, Any], tmdb_id: int, season_number: int, episode_number: int, expected: dict[str, Any], now: str) -> None:
        key = f"tmdb:{tmdb_id}:{season_number}:{episode_number}"
        exists = self.session.scalar(
            select(self.items.c.id).where(
                self.items.c.server_id == item["server_id"],
                self.items.c.library_id == item["library_id"],
                self.items.c.rating_key == key,
            )
        )
        if exists is None:
            air_date = str(expected.get("air_date") or "")
            year = int(air_date[:4]) if air_date[:4].isdigit() else None
            self.session.execute(
                insert(self.items).values(
                    server_id=item["server_id"], library_id=item["library_id"], rating_key=key,
                    plex_type="episode", parent_rating_key=item["rating_key"], season_number=season_number,
                    episode_number=episode_number, title=str(expected.get("name") or f"E{episode_number:02d}"),
                    original_title=str(expected.get("original_name") or ""), year=year,
                    release_date=air_date[:10], duration=int(expected.get("runtime") or 0),
                    raw_json=json.dumps({"tmdb": expected, "expected": True}, ensure_ascii=False), scanned_at=now,
                )
            )

    def finish_tmdb(self, server_id: int, library_id: int, now: str) -> None:
        self.session.execute(
            update(self.libraries)
            .where(self.libraries.c.server_id == server_id, self.libraries.c.library_id == library_id)
            .values(tmdb_synced_at=now, sync_status="idle", sync_error="", updated_at=now)
        )

    def reconcile(self, server_id: int, now: str) -> int:
        self.session.execute(delete(self.matches).where(self.matches.c.server_id == server_id))
        items = [dict(row) for row in self.session.execute(select(TmdbItems.__table__).where(TmdbItems.__table__.c.media_type.in_(["movie", "tv", "season", "episode"]))).mappings()]
        inventory = [dict(row) for row in self.session.execute(select(PlexInventoryItems.__table__).where(PlexInventoryItems.__table__.c.server_id == server_id)).mappings()]
        by_tmdb = {(row["tmdb_id"], row["plex_type"]): row for row in inventory if row.get("tmdb_id")}
        by_title = {(str(row["title"]).casefold(), row.get("year")): row for row in inventory}
        rows: list[dict[str, Any]] = []
        for item in items:
            plex_type = "movie" if item["media_type"] == "movie" else "show" if item["media_type"] == "tv" else "episode"
            match = by_tmdb.get((item["tmdb_id"], plex_type))
            if match is None:
                release = str(item.get("release_date") or "")
                year = int(release[:4]) if release[:4].isdigit() else None
                match = by_title.get((str(item.get("title") or "").casefold(), year))
            source = "tmdb_id" if match and match.get("tmdb_id") else "title_year" if match else ""
            rows.append(
                {
                    "media_type": item["media_type"], "tmdb_id": item["tmdb_id"],
                    "season_number": item.get("season_number"), "episode_number": item.get("episode_number"),
                    "server_id": server_id, "library_id": match.get("library_id") if match else None,
                    "status": "present" if match else "missing", "match_source": source,
                    "confidence": 1.0 if source == "tmdb_id" else 0.7 if match else 0,
                    "plex_rating_key": match.get("rating_key") if match else "", "details": "{}", "updated_at": now,
                }
            )
        if rows:
            self.session.execute(insert(self.matches), rows)
        return len(items)


def _decode_json(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


__all__ = ["MediaLibrarySyncRepository"]
