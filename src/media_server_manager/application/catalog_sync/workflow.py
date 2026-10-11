"""SQLAlchemy-backed catalog synchronization workflows."""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

from ...core import ServerConfig, extract_external_ids
from ...infrastructure.db.models import Server
from ...infrastructure.db.repositories.auth import AuthRepository
from ...infrastructure.db.repositories.catalog_workflow import CatalogWorkflowRepository
from ...infrastructure.db.session import Database
from ...infrastructure.integrations.plex.client import PlexServer
from ...infrastructure.integrations.tmdb import TMDBClient


def _server_config(database: Database, server_id: int) -> ServerConfig:
    with database.transaction() as uow:
        server = uow.session.get(Server, server_id)
        if server is None or not server.enabled:
            raise ValueError("服务器不存在或已停用")
        identifier = AuthRepository(uow.session).setting("plex_client_identifier") or "media-server-manager"
        return ServerConfig(
            name=server.name,
            address=server.address,
            token=server.token,
            skip_libraries=[item for item in server.skip_libraries.replace("；", ";").split(";") if item],
            pinyin_mode=server.pinyin_mode,
            client_identifier=identifier,
        )


def _upsert_details(repo: CatalogWorkflowRepository, tmdb: TMDBClient, media_type: str, tmdb_id: int, details: dict[str, Any], now: str) -> None:
    repo.upsert_tmdb_item(media_type, tmdb_id, details, now=now)
    if media_type != "tv":
        return
    for season in details.get("seasons") or []:
        season_number = int(season.get("season_number") or 0)
        season_details = tmdb.season_details(tmdb_id, season_number)
        repo.upsert_tmdb_item("season", tmdb_id, season_details, parent_id=tmdb_id, season_number=season_number, now=now)
        repo.upsert_relation(tmdb_id, tmdb_id, "tv", "season", season_number=season_number)
        for episode in season_details.get("episodes") or []:
            episode_number = int(episode.get("episode_number") or 0)
            repo.upsert_tmdb_item(
                "episode", tmdb_id, episode, parent_id=tmdb_id,
                season_number=season_number, episode_number=episode_number, now=now,
            )
            repo.upsert_relation(tmdb_id, tmdb_id, "season", "episode", season_number=season_number, episode_number=episode_number)


def _cache_image(database: Database, repo: CatalogWorkflowRepository, media_type: str, tmdb_id: int, image_type: str, source_path: str, now: str) -> None:
    if not source_path:
        return
    root = database.settings.data_dir / "cache" / "tmdb" / media_type / str(tmdb_id)
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{image_type}{Path(source_path).suffix or '.jpg'}"
    if not target.exists():
        response = requests.get(f"https://image.tmdb.org/t/p/w780{source_path}", timeout=30)
        response.raise_for_status()
        with tempfile.NamedTemporaryFile(dir=root, delete=False) as temporary:
            temporary.write(response.content)
            temporary_path = Path(temporary.name)
        temporary_path.replace(target)
    repo.upsert_image({
        "media_type": media_type,
        "tmdb_id": tmdb_id,
        "season_number": None,
        "episode_number": None,
        "image_type": image_type,
        "source_path": source_path,
        "cache_path": str(target),
        "mime_type": "image/png" if target.suffix.lower() == ".png" else "image/jpeg",
        "checksum": hashlib.sha256(target.read_bytes()).hexdigest(),
        "status": "ready",
        "updated_at": now,
    })


def sync_catalog(database: Database, payload: dict[str, Any], *, progress: Callable[[dict[str, Any]], None] | None = None, cancelled: Callable[[], bool] | None = None) -> dict[str, int]:
    with database.transaction() as uow:
        token = AuthRepository(uow.session).setting("tmdb_api_key")
    tmdb = TMDBClient(token)
    media_types = payload.get("media_types") or ["movie", "tv"]
    filters = dict(payload.get("filters") or {})
    max_pages = max(1, min(int(payload.get("max_pages") or 1), 500))
    processed = errors = 0
    for media_type in media_types:
        for page in range(1, max_pages + 1):
            if cancelled and cancelled():
                return {"processed": processed, "errors": errors}
            data = tmdb.discover(media_type, page=page, language=filters.get("language", "zh-CN"), region=filters.get("region", "CN"), include_adult=bool(filters.get("include_adult", False)), sort_by=filters.get("sort_by", "popularity.desc"), primary_release_year=filters.get("year") if media_type == "movie" else None, first_air_date_year=filters.get("year") if media_type == "tv" else None)
            results = data.get("results") or []
            now = datetime.now(timezone.utc).isoformat()
            prepared: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
            for item in results:
                try:
                    tmdb_id = int(item["id"])
                    prepared.append((tmdb_id, item, tmdb.details(media_type, tmdb_id)))
                except Exception:
                    errors += 1
            with database.transaction() as uow:
                repo = CatalogWorkflowRepository(uow.session)
                for tmdb_id, item, details in prepared:
                    try:
                        repo.upsert_tmdb_item(media_type, tmdb_id, item, now=now)
                        _upsert_details(repo, tmdb, media_type, tmdb_id, details, now)
                        _cache_image(database, repo, media_type, tmdb_id, "poster", details.get("poster_path") or item.get("poster_path") or "", now)
                        _cache_image(database, repo, media_type, tmdb_id, "backdrop", details.get("backdrop_path") or item.get("backdrop_path") or "", now)
                        processed += 1
                    except Exception:
                        errors += 1
            if progress:
                progress({"stage": f"tmdb_{media_type}", "page": page, "processed_delta": len(results), "total": max_pages * len(media_types)})
            if page >= int(data.get("total_pages") or page):
                break
    return {"processed": processed, "errors": errors}


def scan_plex_inventory(database: Database, server_id: int, library_ids: list[int] | None = None, *, progress: Callable[[dict[str, Any]], None] | None = None) -> int:
    plex = PlexServer(_server_config(database, server_id), auto_login=False)
    libraries = plex.list_library()
    wanted = set(library_ids or [int(item[0]) for item in libraries])
    count = 0
    for library in libraries:
        library_id, plex_type = int(library[0]), int(library[1])
        if library_id not in wanted or plex_type not in {1, 2}:
            continue
        for key in plex.list_media_keys(library, print_counts=False):
            metadata = plex.get_metadata(str(key))
            entries = [metadata]
            if metadata.get("type") == "show":
                entries.extend({**episode, "_parent_rating_key": str(key)} for episode in plex.list_show_episodes(str(key)))
            with database.transaction() as uow:
                repo = CatalogWorkflowRepository(uow.session)
                for entry in entries:
                    ids = extract_external_ids(entry)
                    repo.upsert_inventory({
                        "server_id": server_id,
                        "library_id": library_id,
                        "rating_key": str(entry.get("ratingKey") or ""),
                        "plex_type": "episode" if entry is not metadata else entry.get("type") or "",
                        "title": entry.get("title") or "",
                        "year": entry.get("year"),
                        "parent_rating_key": str(entry.get("parentRatingKey") or entry.get("grandparentRatingKey") or entry.get("_parent_rating_key") or ""),
                        "season_number": entry.get("parentIndex"),
                        "episode_number": entry.get("index"),
                        "tmdb_id": int(ids["tmdb"]) if ids.get("tmdb") else None,
                        "imdb_id": ids.get("imdb") or "",
                        "tvdb_id": ids.get("tvdb") or "",
                        "raw_json": json.dumps(entry, ensure_ascii=False),
                        "scanned_at": datetime.now(timezone.utc).isoformat(),
                    })
                    count += 1
            if progress:
                progress({"processed_delta": len(entries), "library": library[2]})
    return count


__all__ = ["scan_plex_inventory", "sync_catalog"]
