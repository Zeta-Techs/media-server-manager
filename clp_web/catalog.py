from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import requests

from clp.core import PlexServer, TaskCancelled, extract_external_ids

from .db import connect, decode_payload, get_setting, server_config_from_row, utcnow
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


def _plex_date(metadata: dict[str, Any]) -> str:
    return str(metadata.get("originallyAvailableAt") or metadata.get("originally_available_at") or "")[:10]


def _plex_genres(metadata: dict[str, Any]) -> str:
    values = []
    for field in ("Genre", "Label"):
        for item in metadata.get(field) or []:
            value = str(item.get("tag") or "").strip()
            if value and value not in values:
                values.append(value)
    return ", ".join(values)


def _upsert_media_library_item(db: Any, server_id: int, library_id: int, metadata: dict[str, Any],
                               plex_type: str | None = None, parent_rating_key: str = "",
                               season_number: int | None = None, episode_number: int | None = None) -> None:
    rating_key = str(metadata.get("ratingKey") or "")
    previous = db.execute(
        "SELECT genre, release_date, raw_json FROM media_library_items WHERE server_id = ? AND library_id = ? AND rating_key = ?",
        (server_id, library_id, rating_key),
    ).fetchone()
    previous_raw = decode_payload(previous["raw_json"] if previous else "{}")
    raw_payload = {**previous_raw, **metadata}
    ids = extract_external_ids(metadata)
    raw_payload["external_ids"] = {
        **(previous_raw.get("external_ids") or {}),
        **(metadata.get("external_ids") or {}),
        **ids,
    }
    raw_type = str(metadata.get("type") or plex_type or "")
    if raw_type == "season":
        raw_type = "season"
    elif raw_type == "episode":
        raw_type = "episode"
    elif raw_type == "show":
        raw_type = "show"
    elif raw_type == "movie":
        raw_type = "movie"
    else:
        raw_type = plex_type or raw_type
    duration = int(metadata.get("duration") or 0) if str(metadata.get("duration") or "0").isdigit() else 0
    # Genres/labels in the media-library view are authoritative Plex metadata.
    # Do not carry forward old TMDB genres: a Plex refresh must also clean up
    # values written by older versions of the synchronizer.
    genre = _plex_genres(metadata)
    release_date = _plex_date(metadata) or (str(previous["release_date"] or "") if previous else "")
    db.execute("""
        INSERT INTO media_library_items
          (server_id, library_id, rating_key, plex_type, parent_rating_key, season_number, episode_number,
           title, original_title, year, added_at, release_date, rating, audience_rating, duration,
           content_rating, genre, thumb, art, raw_json, scanned_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(server_id, library_id, rating_key) DO UPDATE SET
          plex_type=excluded.plex_type, parent_rating_key=excluded.parent_rating_key,
          season_number=excluded.season_number, episode_number=excluded.episode_number,
          title=excluded.title, original_title=excluded.original_title, year=excluded.year,
          added_at=excluded.added_at, release_date=excluded.release_date, rating=excluded.rating,
          audience_rating=excluded.audience_rating, duration=excluded.duration,
          content_rating=excluded.content_rating, genre=excluded.genre, thumb=excluded.thumb,
          art=excluded.art, raw_json=excluded.raw_json, scanned_at=excluded.scanned_at
    """, (server_id, library_id, rating_key, raw_type, parent_rating_key,
          season_number if season_number is not None else metadata.get("parentIndex"),
          episode_number if episode_number is not None else metadata.get("index"),
          str(metadata.get("title") or ""), str(metadata.get("originalTitle") or ""), metadata.get("year"),
          metadata.get("addedAt"), release_date, metadata.get("rating"), metadata.get("audienceRating"),
          duration, str(metadata.get("contentRating") or ""), genre,
          str(metadata.get("thumb") or metadata.get("parentThumb") or ""), str(metadata.get("art") or ""),
          json.dumps(raw_payload, ensure_ascii=False), utcnow()))


def _remove_expected_episode_placeholder(db: Any, server_id: int, library_id: int,
                                         parent_rating_key: str, season_number: int,
                                         episode_number: int) -> None:
    """Remove a TMDB expected-episode row once Plex has the real episode."""
    db.execute(
        """DELETE FROM media_library_items
           WHERE server_id = ? AND library_id = ? AND parent_rating_key = ?
             AND plex_type = 'episode' AND season_number = ? AND episode_number = ?
             AND rating_key LIKE 'tmdb:%'""",
        (server_id, library_id, parent_rating_key, season_number, episode_number),
    )


def _library_is_animation(title: str, plex_type: int) -> bool:
    if int(plex_type) != 2:
        return False
    lowered = str(title or "").casefold()
    return any(keyword in lowered for keyword in ("anime", "动画", "番"))


def sync_media_library(db_file: Path, server_id: int, library_id: int, progress=None, cancelled=None,
                       rating_keys: set[str] | None = None) -> int:
    with connect(db_file) as db:
        server = db.execute("SELECT * FROM servers WHERE id = ? AND enabled = 1", (server_id,)).fetchone()
    if server is None:
        raise ValueError("服务器不存在或已停用")
    plex = PlexServer(server_config_from_row(server, db_file), auto_login=False)
    library = next((row for row in plex.list_library() if int(row[0]) == int(library_id)), None)
    if library is None:
        raise ValueError("媒体库不存在")
    plex_type, title = int(library[1]), str(library[2])
    now = utcnow()
    with connect(db_file) as db:
        db.execute("""
          INSERT INTO media_libraries (server_id, library_id, title, plex_type, auto_animation, sync_status, updated_at)
          VALUES (?, ?, ?, ?, ?, 'running', ?)
          ON CONFLICT(server_id, library_id) DO UPDATE SET title=excluded.title, plex_type=excluded.plex_type,
            auto_animation=excluded.auto_animation, sync_status='running', sync_error='', updated_at=excluded.updated_at
        """, (server_id, library_id, title, plex_type, 1 if _library_is_animation(title, plex_type) else 0, now))
        db.commit()
    if rating_keys:
        items = [plex.get_metadata(key) for key in sorted(rating_keys)]
        if any(str(item.get("type") or "") != "show" for item in items):
            raise ValueError("目标不是电视剧")
    else:
        items = plex.list_library_collections(library_id) if plex_type == 3 else plex.list_library_items(library_id, plex_type)
    count = 0
    seen_keys: set[str] = set()
    if rating_keys:
        items = [item for item in items if str(item.get("ratingKey") or "") in rating_keys]
    for item in items:
        if cancelled and cancelled():
            raise TaskCancelled("任务已取消")
        with connect(db_file) as db:
            item_type = str(item.get("type") or ("collection" if plex_type == 3 else "show" if plex_type == 2 else "movie"))
            if item.get("ratingKey"):
                seen_keys.add(str(item["ratingKey"]))
            if item_type == "show" and not rating_keys:
                try:
                    item = plex.get_metadata(str(item.get("ratingKey") or ""))
                except Exception:
                    pass
            _upsert_media_library_item(db, server_id, library_id, item, item_type)
            if item_type == "show":
                show_key = str(item.get("ratingKey") or "")
                for season in plex.get_children(show_key):
                    if season.get("ratingKey"):
                        seen_keys.add(str(season["ratingKey"]))
                    season_index = season.get("index")
                    if season_index is None:
                        season_index = season.get("parentIndex")
                    season_no = int(season_index or 0)
                    _upsert_media_library_item(db, server_id, library_id, season, "season", show_key, season_no, None)
                for episode in plex.list_show_episodes(show_key):
                    if episode.get("ratingKey"):
                        seen_keys.add(str(episode["ratingKey"]))
                    season_no = int(episode.get("parentIndex") or 0)
                    episode_no = int(episode.get("index") or 0)
                    _upsert_media_library_item(db, server_id, library_id, episode, "episode", show_key,
                                               season_no, episode_no)
                    _remove_expected_episode_placeholder(db, server_id, library_id, show_key, season_no, episode_no)
            db.commit()
        count += 1
        if progress:
            progress({"processed_delta": 1, "total": len(items), "stage": "plex_media_library", "library": title})
    with connect(db_file) as db:
        existing = [str(row["rating_key"]) for row in db.execute("SELECT rating_key FROM media_library_items WHERE server_id = ? AND library_id = ?", (server_id, library_id)).fetchall()]
        # Keep TMDB season and episode placeholders; the Plex pass only owns
        # rows returned by Plex and must not erase the comparison baseline.
        if not rating_keys:
            stale = [key for key in existing if key not in seen_keys and not key.startswith("tmdb")]
            if stale:
                db.executemany("DELETE FROM media_library_items WHERE server_id = ? AND library_id = ? AND rating_key = ?", [(server_id, library_id, key) for key in stale])
        db.execute("UPDATE media_libraries SET plex_synced_at = ?, sync_status = 'idle', updated_at = ? WHERE server_id = ? AND library_id = ?",
                   (utcnow(), utcnow(), server_id, library_id))
        db.commit()
    return count


def sync_media_library_full(db_file: Path, server_id: int, library_id: int, progress=None, cancelled=None,
                            rating_keys: set[str] | None = None) -> dict[str, int]:
    """Refresh Plex metadata, then TMDB metadata, preserving both sources."""
    plex_count = sync_media_library(db_file, server_id, library_id, progress=progress, cancelled=cancelled, rating_keys=rating_keys)
    sync_media_library_tmdb(db_file, server_id, library_id, progress=progress, cancelled=cancelled, rating_keys=rating_keys)
    return {"plex": plex_count}


def sync_media_library_tmdb(db_file: Path, server_id: int, library_id: int, progress=None, cancelled=None,
                            rating_keys: set[str] | None = None, logger=None, strict_errors: bool = False) -> int:
    token = get_setting("tmdb_api_key", db_file)
    if not token:
        raise ValueError("请先保存 TMDB API Key")
    with connect(db_file) as db:
        if rating_keys:
            placeholders = ",".join("?" for _ in rating_keys)
            rows = db.execute(
                f"SELECT * FROM media_library_items WHERE server_id = ? AND library_id = ? AND plex_type IN ('show','movie') AND rating_key IN ({placeholders}) ORDER BY id",
                (server_id, library_id, *sorted(rating_keys)),
            ).fetchall()
        else:
            rows = db.execute("SELECT * FROM media_library_items WHERE server_id = ? AND library_id = ? AND plex_type IN ('show','movie') ORDER BY id", (server_id, library_id)).fetchall()
    tmdb = TMDBClient(token)
    processed = 0
    failures = []
    def report_failure(message: str) -> None:
        failures.append(message)
        if logger:
            logger(message)
    if strict_errors and not rows:
        raise ValueError("未找到需要重新检查的电视剧缓存")
    for row in rows:
        if cancelled and cancelled():
            raise TaskCancelled("任务已取消")
        payload = json.loads(row["raw_json"] or "{}")
        ids = payload.get("external_ids") or extract_external_ids(payload)
        tmdb_id = ids.get("tmdb")
        if not tmdb_id and row["plex_type"] == "show":
            try:
                year = int(row["year"]) if row["year"] else None
                matches = tmdb.search_tv(str(row["title"] or ""), year)
                if matches:
                    tmdb_id = int(matches[0].get("id") or 0) or None
                    if tmdb_id:
                        payload["external_ids"] = {**ids, "tmdb": tmdb_id}
            except Exception as exc:
                report_failure(f"TMDB 搜索失败：{row['title']}：{exc}")
                tmdb_id = None
        if not tmdb_id:
            report_failure(f"未找到 TMDB 匹配：{row['title']}")
        if tmdb_id:
            details = None
            try:
                details = tmdb.details("tv" if row["plex_type"] == "show" else "movie", int(tmdb_id))
            except Exception as exc:
                cached_details = payload.get("tmdb")
                if isinstance(cached_details, dict) and cached_details:
                    details = cached_details
                else:
                    report_failure(f"TMDB 详情获取失败：{row['title']}（{tmdb_id}）：{exc}")
            if details:
              try:
                with connect(db_file) as db:
                    tmdb_release = str(details.get("first_air_date") or details.get("release_date") or "")
                    # TMDB enriches raw data and dates only.  The genre column
                    # remains the Plex Genre/Label projection above.
                    db.execute("UPDATE media_library_items SET raw_json = ?, release_date = COALESCE(NULLIF(?, ''), release_date), scanned_at = ? WHERE server_id = ? AND library_id = ? AND rating_key = ?",
                               (json.dumps({**payload, "tmdb": details}, ensure_ascii=False), tmdb_release, utcnow(), server_id, library_id, row["rating_key"]))
                    if row["plex_type"] == "show":
                        for season_info in details.get("seasons") or []:
                            season_number = int(season_info.get("season_number") or 0)
                            try:
                                season_details = tmdb.season_details(int(tmdb_id), season_number)
                            except Exception as exc:
                                season_details = None
                                cached_season = db.execute("SELECT raw_json FROM media_library_items WHERE server_id = ? AND library_id = ? AND parent_rating_key = ? AND plex_type = 'season' AND season_number = ? ORDER BY id LIMIT 1", (server_id, library_id, row["rating_key"], season_number)).fetchone()
                                if cached_season:
                                    cached = decode_payload(cached_season["raw_json"])
                                    season_details = cached.get("tmdb") if isinstance(cached.get("tmdb"), dict) else None
                                if not season_details:
                                    report_failure(f"TMDB 季详情获取失败：{row['title']} S{season_number:02d}（{tmdb_id}）：{exc}")
                                if not season_details:
                                    continue
                            season_key = f"tmdb-season:{tmdb_id}:{season_number}"
                            season_present = db.execute("SELECT 1 FROM media_library_items WHERE server_id = ? AND library_id = ? AND parent_rating_key = ? AND plex_type = 'season' AND season_number = ? LIMIT 1", (server_id, library_id, row["rating_key"], season_number)).fetchone()
                            if not season_present:
                                first_air = str((season_details.get("episodes") or [{}])[0].get("air_date") or "")[:10]
                                db.execute("INSERT OR IGNORE INTO media_library_items (server_id, library_id, rating_key, plex_type, parent_rating_key, season_number, title, release_date, raw_json, scanned_at) VALUES (?, ?, ?, 'season', ?, ?, ?, ?, ?, ?)", (server_id, library_id, season_key, row["rating_key"], season_number, str(season_details.get("name") or f"第 {season_number} 季"), first_air, json.dumps({"tmdb": season_details, "expected": True}, ensure_ascii=False), utcnow()))
                            for expected in season_details.get("episodes") or []:
                                episode_number = int(expected.get("episode_number") or 0)
                                # A TMDB expected row is the comparison
                                # baseline, not proof that Plex has the
                                # episode.  Exclude placeholders here or a
                                # repeated recheck will delete every missing
                                # episode and the next recheck will recreate it.
                                present = db.execute("SELECT 1 FROM media_library_items WHERE server_id = ? AND library_id = ? AND parent_rating_key = ? AND plex_type = 'episode' AND season_number = ? AND episode_number = ? AND rating_key NOT LIKE 'tmdb:%' LIMIT 1", (server_id, library_id, row["rating_key"], season_number, episode_number)).fetchone()
                                if present:
                                    db.execute("DELETE FROM media_library_items WHERE server_id = ? AND library_id = ? AND rating_key = ?", (server_id, library_id, f"tmdb:{tmdb_id}:{season_number}:{episode_number}"))
                                    continue
                                expected_key = f"tmdb:{tmdb_id}:{season_number}:{episode_number}"
                                db.execute("""
                                  INSERT OR IGNORE INTO media_library_items
                                    (server_id, library_id, rating_key, plex_type, parent_rating_key, season_number, episode_number,
                                     title, original_title, year, release_date, duration, raw_json, scanned_at)
                                  VALUES (?, ?, ?, 'episode', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """, (server_id, library_id, expected_key, row["rating_key"], season_number, episode_number,
                                      str(expected.get("name") or f"E{episode_number:02d}"), str(expected.get("original_name") or ""),
                                      int(str(expected.get("air_date") or "")[:4]) if str(expected.get("air_date") or "")[:4].isdigit() else None,
                                      str(expected.get("air_date") or "")[:10], int(expected.get("runtime") or 0),
                                      json.dumps({"tmdb": expected, "expected": True}, ensure_ascii=False), utcnow()))
                    db.commit()
              except Exception as exc:
                report_failure(f"TMDB 缺集写入失败：{row['title']}（{tmdb_id}）：{exc}")
        processed += 1
        if progress:
            progress({"processed_delta": 1, "total": len(rows), "stage": "tmdb_media_library"})
    if strict_errors and failures:
        raise RuntimeError("；".join(failures))
    with connect(db_file) as db:
        db.execute("UPDATE media_libraries SET tmdb_synced_at = ?, sync_status = 'idle', updated_at = ? WHERE server_id = ? AND library_id = ?", (utcnow(), utcnow(), server_id, library_id))
        db.commit()
    return processed


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
