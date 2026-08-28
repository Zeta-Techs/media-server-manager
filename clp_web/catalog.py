from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import requests

from clp.core import PlexServer, extract_external_ids

from .db import connect, get_setting, server_config_from_row, utcnow
from .tmdb import TMDBClient


def _cache_root() -> Path:
    root = Path(os.environ.get("MSM_CONFIG_DIR", os.environ.get("CLP_CONFIG_DIR", "config"))) / "media_cache" / "tmdb"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _upsert_item(db: Any, media_type: str, tmdb_id: int, payload: dict[str, Any], parent_id: int | None = None,
                 season_number: int | None = None, episode_number: int | None = None) -> None:
    title = payload.get("title") or payload.get("name") or ""
    original = payload.get("original_title") or payload.get("original_name") or ""
    release = payload.get("release_date") or payload.get("first_air_date") or payload.get("air_date") or ""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    checksum = hashlib.sha256(raw.encode()).hexdigest()
    now = utcnow()
    db.execute("""
        INSERT INTO tmdb_items (tmdb_id, media_type, parent_tmdb_id, season_number, episode_number, title,
          original_title, release_date, status, overview, poster_path, backdrop_path, still_path, genres,
          raw_json, fetched_at, updated_at, expires_at, checksum)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(media_type, tmdb_id, season_number, episode_number) DO UPDATE SET
          parent_tmdb_id=excluded.parent_tmdb_id, title=excluded.title, original_title=excluded.original_title,
          release_date=excluded.release_date, status=excluded.status, overview=excluded.overview,
          poster_path=excluded.poster_path, backdrop_path=excluded.backdrop_path, still_path=excluded.still_path,
          genres=excluded.genres, raw_json=excluded.raw_json, fetched_at=excluded.fetched_at,
          updated_at=excluded.updated_at, expires_at=excluded.expires_at, checksum=excluded.checksum
    """, (tmdb_id, media_type, parent_id, season_number, episode_number, title, original, release,
          payload.get("status") or "", payload.get("overview") or "", payload.get("poster_path") or "",
          payload.get("backdrop_path") or "", payload.get("still_path") or "", json.dumps(payload.get("genres") or payload.get("genre_ids") or [], ensure_ascii=False),
          raw, now, now, "", checksum))


def _cache_image(db_file: Path, media_type: str, tmdb_id: int, image_type: str, source_path: str,
                 season_number: int | None = None, episode_number: int | None = None) -> None:
    if not source_path:
        return
    root = _cache_root() / media_type / str(tmdb_id)
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{image_type}{Path(source_path).suffix or '.jpg'}"
    if not target.exists():
        response = requests.get(f"https://image.tmdb.org/t/p/w780{source_path}", timeout=30)
        response.raise_for_status()
        with tempfile.NamedTemporaryFile(dir=root, delete=False) as temp:
            temp.write(response.content)
            temp_path = Path(temp.name)
        temp_path.replace(target)
    checksum = hashlib.sha256(target.read_bytes()).hexdigest()
    with connect(db_file) as db:
        db.execute("""INSERT INTO tmdb_images (media_type, tmdb_id, season_number, episode_number, image_type,
          source_path, cache_path, mime_type, checksum, status, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?)
          ON CONFLICT(media_type, tmdb_id, season_number, episode_number, image_type) DO UPDATE SET cache_path=excluded.cache_path,
          checksum=excluded.checksum, status='ready', updated_at=excluded.updated_at""",
          (media_type, tmdb_id, season_number, episode_number, image_type, source_path, str(target),
           'image/png' if target.suffix.lower() == '.png' else 'image/jpeg', checksum, utcnow()))
        db.commit()


def sync_catalog(db_file: Path, payload: dict[str, Any], progress=None, cancelled=None) -> dict[str, Any]:
    token = get_setting("tmdb_api_key", db_file)
    tmdb = TMDBClient(token)
    media_types = payload.get("media_types") or ["movie", "tv"]
    filters = dict(payload.get("filters") or {})
    max_pages = max(1, min(int(payload.get("max_pages") or 1), 500))
    processed = errors = 0
    for media_type in media_types:
        for page in range(1, max_pages + 1):
            if cancelled and cancelled():
                break
            data = tmdb.discover(media_type, page=page, language=filters.get("language", "zh-CN"), region=filters.get("region", "CN"),
                                 include_adult=bool(filters.get("include_adult", False)), sort_by=filters.get("sort_by", "popularity.desc"),
                                 primary_release_year=filters.get("year") if media_type == "movie" else None,
                                 first_air_date_year=filters.get("year") if media_type == "tv" else None)
            results = data.get("results") or []
            with connect(db_file) as db:
                for item in results:
                    try:
                        _upsert_item(db, media_type, int(item["id"]), item)
                        processed += 1
                    except Exception:
                        errors += 1
                db.commit()
            for item in results:
                try:
                    details = enrich_item(db_file, media_type, int(item["id"]))
                    _cache_image(db_file, media_type, int(item["id"]), "poster", details.get("poster_path") or item.get("poster_path") or "")
                    _cache_image(db_file, media_type, int(item["id"]), "backdrop", details.get("backdrop_path") or item.get("backdrop_path") or "")
                except Exception:
                    errors += 1
            if progress:
                progress({"stage": f"tmdb_{media_type}", "page": page, "processed_delta": len(results), "total": max_pages * len(media_types)})
            if page >= int(data.get("total_pages") or page):
                break
    return {"processed": processed, "errors": errors}


def enrich_item(db_file: Path, media_type: str, tmdb_id: int, parent_id: int | None = None) -> dict[str, Any]:
    tmdb = TMDBClient(get_setting("tmdb_api_key", db_file))
    details = tmdb.details(media_type, tmdb_id)
    with connect(db_file) as db:
        _upsert_item(db, media_type, tmdb_id, details, parent_id=parent_id)
        if media_type == "tv":
            for season in details.get("seasons") or []:
                sn = int(season.get("season_number") or 0)
                season_details = tmdb.season_details(tmdb_id, sn)
                _upsert_item(db, "season", tmdb_id, season_details, parent_id=tmdb_id, season_number=sn)
                for episode in season_details.get("episodes") or []:
                    en = int(episode.get("episode_number") or 0)
                    _upsert_item(db, "episode", tmdb_id, episode, parent_id=tmdb_id, season_number=sn, episode_number=en)
                    db.execute("INSERT OR IGNORE INTO tmdb_relations VALUES (?, ?, 'season', 'episode', ?, ?)", (tmdb_id, tmdb_id, sn, en))
                db.execute("INSERT OR IGNORE INTO tmdb_relations VALUES (?, ?, 'tv', 'season', ?, NULL)", (tmdb_id, tmdb_id, sn))
        db.commit()
    return details


def scan_plex_inventory(db_file: Path, server_id: int, library_ids: list[int] | None = None, progress=None) -> int:
    with connect(db_file) as db:
        server = db.execute("SELECT * FROM servers WHERE id = ? AND enabled = 1", (server_id,)).fetchone()
    if server is None:
        raise ValueError("服务器不存在或已停用")
    plex = PlexServer(server_config_from_row(server, db_file), auto_login=False)
    libraries = plex.list_library()
    wanted = set(library_ids or [int(item[0]) for item in libraries])
    count = 0
    for library in libraries:
        if int(library[0]) not in wanted or int(library[1]) not in {1, 2}:
            continue
        for key in plex.list_media_keys(library, print_counts=False):
            metadata = plex.get_metadata(str(key))
            ids = extract_external_ids(metadata)
            with connect(db_file) as db:
                db.execute("""
                  INSERT INTO plex_inventory_items (server_id, library_id, rating_key, plex_type, title, year,
                    parent_rating_key, season_number, episode_number, tmdb_id, imdb_id, tvdb_id, raw_json, scanned_at)
                  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                  ON CONFLICT(server_id, library_id, rating_key) DO UPDATE SET title=excluded.title, year=excluded.year,
                    parent_rating_key=excluded.parent_rating_key, season_number=excluded.season_number,
                    episode_number=excluded.episode_number, tmdb_id=excluded.tmdb_id, imdb_id=excluded.imdb_id,
                    tvdb_id=excluded.tvdb_id, raw_json=excluded.raw_json, scanned_at=excluded.scanned_at
                """, (server_id, int(library[0]), str(key), metadata.get("type") or "", metadata.get("title") or "",
                      metadata.get("year"), str(metadata.get("parentRatingKey") or metadata.get("grandparentRatingKey") or ""),
                      metadata.get("parentIndex"), metadata.get("index"), int(ids["tmdb"]) if ids.get("tmdb") else None,
                      ids.get("imdb") or "", ids.get("tvdb") or "", json.dumps(metadata, ensure_ascii=False), utcnow()))
                db.commit()
            count += 1
            if progress:
                progress({"processed_delta": 1, "library": library[2]})
            if metadata.get("type") == "show":
                for episode in plex.list_show_episodes(str(key)):
                    eids = extract_external_ids(episode)
                    with connect(db_file) as db:
                        db.execute("""INSERT OR REPLACE INTO plex_inventory_items
                          (server_id, library_id, rating_key, plex_type, title, year, parent_rating_key, season_number,
                           episode_number, tmdb_id, imdb_id, tvdb_id, raw_json, scanned_at)
                          VALUES (?, ?, ?, 'episode', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                          (server_id, int(library[0]), str(episode.get("ratingKey") or ""), episode.get("title") or "",
                           episode.get("year"), str(key), episode.get("parentIndex"), episode.get("index"),
                           int(eids["tmdb"]) if eids.get("tmdb") else None, eids.get("imdb") or "", eids.get("tvdb") or "",
                           json.dumps(episode, ensure_ascii=False), utcnow()))
                        db.commit()
                    count += 1
    return count


def reconcile(db_file: Path, server_id: int) -> int:
    with connect(db_file) as db:
        db.execute("DELETE FROM media_match_results WHERE server_id = ?", (server_id,))
        items = db.execute("SELECT * FROM tmdb_items WHERE media_type IN ('movie','tv','season','episode')").fetchall()
        inventory = db.execute("SELECT * FROM plex_inventory_items WHERE server_id = ?", (server_id,)).fetchall()
        by_tmdb = {(r["tmdb_id"], r["plex_type"]): r for r in inventory if r["tmdb_id"]}
        by_title = {(str(r["title"]).casefold(), r["year"]): r for r in inventory}
        for item in items:
            plex_type = "movie" if item["media_type"] == "movie" else ("show" if item["media_type"] == "tv" else "episode")
            match = by_tmdb.get((item["tmdb_id"], plex_type))
            if not match:
                match = by_title.get((str(item["title"]).casefold(), int(item["release_date"][:4]) if item["release_date"][:4].isdigit() else None))
            status = "present" if match else "missing"
            source = "tmdb_id" if match and match["tmdb_id"] else ("title_year" if match else "")
            db.execute("""INSERT INTO media_match_results
              (media_type, tmdb_id, season_number, episode_number, server_id, library_id, status, match_source,
               confidence, plex_rating_key, details, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
              (item["media_type"], item["tmdb_id"], item["season_number"], item["episode_number"], server_id,
               match["library_id"] if match else None, status, source, 1.0 if source == "tmdb_id" else 0.7 if match else 0,
               match["rating_key"] if match else "", "{}", utcnow()))
        db.commit()
        return len(items)
