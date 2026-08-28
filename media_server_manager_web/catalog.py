from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any

import requests

from media_server_manager.core import PlexServer, extract_external_ids

from .db import connect, get_setting, server_config_from_row, utcnow
from .tmdb import TMDBClient


def quarter_for_date(value: str | date | None) -> tuple[int, str] | None:
    if not value:
        return None
    try:
        parsed = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None
    quarter = (parsed.month - 1) // 3 + 1
    return parsed.year, f"Q{quarter}"


def quarter_bounds(year: int, quarter: str) -> tuple[str, str]:
    q = int(str(quarter).upper().replace("Q", ""))
    if q not in (1, 2, 3, 4):
        raise ValueError("季度必须是 Q1、Q2、Q3 或 Q4")
    start_month = (q - 1) * 3 + 1
    start = date(year, start_month, 1)
    end = date(year + 1, 1, 1) - timedelta(days=1) if q == 4 else date(year, start_month + 3, 1) - timedelta(days=1)
    return start.isoformat(), end.isoformat()


def ensure_anime_quarters(db_file: Path, start_year: int = 2000, end_year: int | None = None) -> int:
    end_year = end_year or date.today().year
    now = utcnow()
    count = 0
    with connect(db_file) as db:
        for year in range(start_year, end_year + 1):
            for q in range(1, 5):
                start, end = quarter_bounds(year, f"Q{q}")
                status = "current" if date.today().isoformat() >= start and date.today().isoformat() <= end else "historical"
                policy = "daily" if status == "current" else "weekly"
                db.execute("""INSERT INTO anime_seasons (year, quarter, start_date, end_date, status, sync_policy, created_at, updated_at)
                  VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(year, quarter) DO UPDATE SET start_date=excluded.start_date,
                  end_date=excluded.end_date, status=excluded.status, sync_policy=excluded.sync_policy, updated_at=excluded.updated_at""",
                  (year, f"Q{q}", start, end, status, policy, now, now))
                count += 1
        db.commit()
    return count


def _schedule_time(air_date: str | None) -> str | None:
    if not air_date:
        return None
    try:
        day = date.fromisoformat(str(air_date)[:10])
    except ValueError:
        return None
    return datetime.combine(day, dt_time(4, 0)).isoformat(timespec="seconds")


def update_show_schedule(db_file: Path, tmdb_id: int, details: dict[str, Any], now: date | None = None) -> dict[str, Any]:
    today = now or date.today()
    last = details.get("last_episode_to_air") or {}
    nxt = details.get("next_episode_to_air") or {}
    last_date = last.get("air_date") or None
    next_date = nxt.get("air_date") or None
    if next_date:
        # Schedule at the announced air date. If TMDB still reports a date
        # that has already passed, schedule one next-day compensation refresh
        # to catch delayed airing/metadata updates.
        if str(next_date)[:10] <= today.isoformat():
            next_sync = _schedule_time((today + timedelta(days=1)).isoformat())
        else:
            next_sync = _schedule_time(next_date)
        mode = "scheduled"
        state = "scheduled"
        active_until = next_date
    elif last_date:
        try:
            last_day = date.fromisoformat(str(last_date)[:10])
        except ValueError:
            last_day = today
        active_until = (last_day + timedelta(days=30)).isoformat()
        active = last_day >= today - timedelta(days=30)
        next_sync = _schedule_time((today + timedelta(days=1)).isoformat()) if active else None
        mode = "fallback_daily" if active else "history_weekly"
        state = "active" if active else "historical"
    else:
        active_until = None
        next_sync = None
        mode = "history_weekly"
        state = "unknown"
    with connect(db_file) as db:
        previous = db.execute("SELECT next_air_date FROM anime_show_schedule WHERE tmdb_id=?", (tmdb_id,)).fetchone()
        db.execute("""INSERT INTO anime_show_schedule (tmdb_id, last_aired_episode_date, last_aired_season,
          last_aired_episode, next_air_date, next_air_season, next_air_episode, schedule_source,
          last_schedule_checked_at, next_sync_at, active_until, sync_mode, schedule_status, updated_at)
          VALUES (?, ?, ?, ?, ?, ?, ?, 'tmdb', ?, ?, ?, ?, ?, ?) ON CONFLICT(tmdb_id) DO UPDATE SET
          last_aired_episode_date=excluded.last_aired_episode_date, last_aired_season=excluded.last_aired_season,
          last_aired_episode=excluded.last_aired_episode, next_air_date=excluded.next_air_date,
          next_air_season=excluded.next_air_season, next_air_episode=excluded.next_air_episode,
          last_schedule_checked_at=excluded.last_schedule_checked_at, next_sync_at=excluded.next_sync_at,
          active_until=excluded.active_until, sync_mode=excluded.sync_mode, schedule_status=excluded.schedule_status,
          updated_at=excluded.updated_at""", (tmdb_id, last_date, last.get("season_number"), last.get("episode_number"),
          next_date, nxt.get("season_number"), nxt.get("episode_number"), utcnow(), next_sync, active_until, mode, state, utcnow()))
        event_type = "schedule_discovered"
        if next_date and (not previous or previous["next_air_date"] != next_date):
            event_type = "episode_air_date"
        if next_date:
            exists = db.execute("SELECT 1 FROM anime_sync_events WHERE tmdb_id=? AND event_type=? AND scheduled_at=? LIMIT 1", (tmdb_id, event_type, next_sync)).fetchone()
            if not exists:
                db.execute("INSERT INTO anime_sync_events (tmdb_id,event_type,scheduled_at,status) VALUES (?,?,?,'scheduled')", (tmdb_id, event_type, next_sync))
        db.commit()
    return {"tmdb_id": tmdb_id, "last_air_date": last_date, "next_air_date": next_date, "next_sync_at": next_sync, "sync_mode": mode, "schedule_status": state}


def due_anime_ids(db_file: Path, now: str | None = None) -> list[int]:
    with connect(db_file) as db:
        rows = db.execute("SELECT tmdb_id FROM anime_show_schedule WHERE next_sync_at IS NOT NULL AND next_sync_at <= ? ORDER BY next_sync_at", (now or datetime.now().isoformat(timespec="seconds"),)).fetchall()
    return [int(row["tmdb_id"]) for row in rows]


def _cache_root() -> Path:
    root = Path(os.environ.get("MSM_CONFIG_DIR", "config")) / "media_cache" / "tmdb"
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
    if media_type == "season" and season_number is not None:
        root = _cache_root() / "season" / str(tmdb_id) / str(season_number)
    elif media_type == "episode" and season_number is not None and episode_number is not None:
        root = _cache_root() / "episode" / str(tmdb_id) / str(season_number) / str(episode_number)
    else:
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
                    if media_type == "tv":
                        update_show_schedule(db_file, int(item["id"]), details)
                    _cache_image(db_file, media_type, int(item["id"]), "poster", details.get("poster_path") or item.get("poster_path") or "")
                    _cache_image(db_file, media_type, int(item["id"]), "backdrop", details.get("backdrop_path") or item.get("backdrop_path") or "")
                except Exception:
                    errors += 1
            if progress:
                progress({"stage": f"tmdb_{media_type}", "page": page, "processed_delta": len(results), "total": max_pages * len(media_types)})
            if page >= int(data.get("total_pages") or page):
                break
    return {"processed": processed, "errors": errors}


def sync_anime_quarter(db_file: Path, year: int, quarter: str, progress=None, cancelled=None, max_pages: int = 20) -> dict[str, Any]:
    ensure_anime_quarters(db_file, min(2000, year), max(date.today().year, year))
    start, end = quarter_bounds(year, quarter)
    payload = {"media_types": ["tv"], "filters": {"language": "zh-CN", "region": "JP", "include_adult": False,
               "sort_by": "first_air_date.asc", "first_air_date.gte": start, "first_air_date.lte": end,
               "with_origin_country": "JP", "with_genres": "16"}, "max_pages": max(1, min(int(max_pages or 20), 500))}
    token = get_setting("tmdb_api_key", db_file)
    tmdb = TMDBClient(token)
    found: list[int] = []
    for page in range(1, payload["max_pages"] + 1):
        if cancelled and cancelled():
            break
        params = {k: v for k, v in payload["filters"].items() if v not in (None, "")}
        data = tmdb.discover("tv", page=page, **params)
        results = data.get("results") or []
        page_ids: list[int] = []
        with connect(db_file) as db:
            season_row = db.execute("SELECT id FROM anime_seasons WHERE year=? AND quarter=?", (year, quarter)).fetchone()
            season_id = int(season_row["id"])
            for rank, item in enumerate(results, start=1):
                tmdb_id = int(item["id"]); found.append(tmdb_id); page_ids.append(tmdb_id)
                _upsert_item(db, "tv", tmdb_id, item)
                db.execute("""INSERT INTO anime_season_items (season_id, tmdb_id, discover_rank, popularity, vote_average, first_discovered_at, updated_at)
                  VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(season_id, tmdb_id) DO UPDATE SET discover_rank=excluded.discover_rank,
                  popularity=excluded.popularity, vote_average=excluded.vote_average, updated_at=excluded.updated_at""",
                  (season_id, tmdb_id, rank, item.get("popularity") or 0, item.get("vote_average") or 0, utcnow(), utcnow()))
            db.commit()
        for tmdb_id in page_ids:
            if cancelled and cancelled():
                break
            details = enrich_item(db_file, "tv", tmdb_id)
            update_show_schedule(db_file, tmdb_id, details)
        if progress:
            progress({"stage": f"anime_{year}_{quarter}", "page": page, "processed_delta": len(results), "total": int(data.get("total_results") or 0)})
        if page >= int(data.get("total_pages") or page):
            break
    with connect(db_file) as db:
        db.execute("UPDATE anime_seasons SET last_synced_at=?, updated_at=? WHERE year=? AND quarter=?", (utcnow(), utcnow(), year, quarter)); db.commit()
    return {"year": year, "quarter": quarter, "processed": len(found), "tmdb_ids": found}


def discover_anime_schedules(db_file: Path, progress=None, cancelled=None) -> int:
    with connect(db_file) as db:
        ids = [int(row["tmdb_id"]) for row in db.execute("""SELECT DISTINCT t.tmdb_id FROM tmdb_items t
                    JOIN anime_season_items asi ON asi.tmdb_id=t.tmdb_id
                    WHERE t.media_type='tv' ORDER BY t.tmdb_id""").fetchall()]
    tmdb = TMDBClient(get_setting("tmdb_api_key", db_file)); count = 0
    for tmdb_id in ids:
        if cancelled and cancelled():
            break
        try:
            details = tmdb.tv_schedule(tmdb_id)
            update_show_schedule(db_file, tmdb_id, details)
            count += 1
        except Exception:
            # A single removed or rate-limited title must not prevent the
            # weekly discovery pass from reaching the rest of the catalog.
            continue
        if progress:
            progress({"processed_delta": 1, "total": len(ids), "stage": "anime_schedule_discovery"})
    return count


def enrich_item(db_file: Path, media_type: str, tmdb_id: int, parent_id: int | None = None) -> dict[str, Any]:
    tmdb = TMDBClient(get_setting("tmdb_api_key", db_file))
    details = tmdb.details(media_type, tmdb_id)
    images: list[tuple[str, int, str, str, int | None, int | None]] = []
    with connect(db_file) as db:
        _upsert_item(db, media_type, tmdb_id, details, parent_id=parent_id)
        if media_type == "tv":
            for season in details.get("seasons") or []:
                sn = int(season.get("season_number") or 0)
                season_details = tmdb.season_details(tmdb_id, sn)
                _upsert_item(db, "season", tmdb_id, season_details, parent_id=tmdb_id, season_number=sn)
                images.append(("season", tmdb_id, "season_poster", season_details.get("poster_path") or "", sn, None))
                for episode in season_details.get("episodes") or []:
                    en = int(episode.get("episode_number") or 0)
                    _upsert_item(db, "episode", tmdb_id, episode, parent_id=tmdb_id, season_number=sn, episode_number=en)
                    images.append(("episode", tmdb_id, "episode_still", episode.get("still_path") or "", sn, en))
                    db.execute("INSERT OR IGNORE INTO tmdb_relations VALUES (?, ?, 'season', 'episode', ?, ?)", (tmdb_id, tmdb_id, sn, en))
                db.execute("INSERT OR IGNORE INTO tmdb_relations VALUES (?, ?, 'tv', 'season', ?, NULL)", (tmdb_id, tmdb_id, sn))
        db.commit()
    for image_media_type, image_tmdb_id, image_type, source_path, season_number, episode_number in images:
        _cache_image(db_file, image_media_type, image_tmdb_id, image_type, source_path, season_number, episode_number)
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
                seen_seasons: set[int] = set()
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
                    try:
                        season_no = int(episode.get("parentIndex"))
                    except (TypeError, ValueError):
                        season_no = None
                    if season_no is not None and season_no not in seen_seasons:
                        seen_seasons.add(season_no)
                        with connect(db_file) as db:
                            db.execute("""INSERT OR REPLACE INTO plex_inventory_items
                              (server_id, library_id, rating_key, plex_type, title, year, parent_rating_key,
                               season_number, episode_number, tmdb_id, imdb_id, tvdb_id, raw_json, scanned_at)
                              VALUES (?, ?, ?, 'season', ?, ?, ?, ?, NULL, ?, '', '', ?, ?)""",
                              (server_id, int(library[0]), f"{key}:season:{season_no}", f"Season {season_no}", metadata.get("year"), str(key), season_no,
                               int(ids["tmdb"]) if ids.get("tmdb") else None, json.dumps({"show": metadata, "season_number": season_no}, ensure_ascii=False), utcnow()))
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


def reconcile_anime(db_file: Path, server_id: int) -> int:
    with connect(db_file) as db:
        db.execute("DELETE FROM anime_match_results WHERE server_id = ?", (server_id,))
        items = db.execute("SELECT * FROM tmdb_items WHERE media_type IN ('tv','season','episode')").fetchall()
        inventory = db.execute("SELECT * FROM plex_inventory_items WHERE server_id = ?", (server_id,)).fetchall()
        tv_by_id = {int(r["tmdb_id"]): r for r in inventory if r["tmdb_id"] and r["plex_type"] == "show"}
        seasons = {(int(r["tmdb_id"]), int(r["season_number"] or 0)): r for r in inventory if r["tmdb_id"] and r["plex_type"] == "season"}
        episodes = {(int(r["tmdb_id"]), int(r["season_number"] or 0), int(r["episode_number"] or 0)): r for r in inventory if r["tmdb_id"] and r["plex_type"] == "episode"}
        for item in items:
            media_type = item["media_type"]
            match = None
            if media_type == "tv":
                match = tv_by_id.get(int(item["tmdb_id"]))
            elif media_type == "season":
                match = seasons.get((int(item["tmdb_id"]), int(item["season_number"] or 0)))
            else:
                match = episodes.get((int(item["tmdb_id"]), int(item["season_number"] or 0), int(item["episode_number"] or 0)))
            release = str(item["release_date"] or "")[:10]
            is_future = bool(release and release > date.today().isoformat())
            if is_future:
                status = "future"
            elif media_type == "season":
                expected = db.execute("SELECT COUNT(*) AS n FROM tmdb_items WHERE media_type='episode' AND tmdb_id=? AND season_number=? AND COALESCE(release_date,'') <= ?", (item["tmdb_id"], item["season_number"], date.today().isoformat())).fetchone()["n"]
                present = db.execute("SELECT COUNT(*) AS n FROM plex_inventory_items WHERE server_id=? AND tmdb_id=? AND plex_type='episode' AND season_number=?", (server_id, item["tmdb_id"], item["season_number"])).fetchone()["n"]
                status = "present" if match and (not expected or present >= expected) else ("partial" if present else "missing")
            elif media_type == "tv":
                expected = db.execute("SELECT COUNT(*) AS n FROM tmdb_items WHERE media_type='episode' AND tmdb_id=? AND COALESCE(release_date,'') <= ?", (item["tmdb_id"], date.today().isoformat())).fetchone()["n"]
                present = db.execute("SELECT COUNT(*) AS n FROM plex_inventory_items WHERE server_id=? AND tmdb_id=? AND plex_type='episode'", (server_id, item["tmdb_id"])).fetchone()["n"]
                status = "present" if match and (not expected or present >= expected) else ("partial" if present else ("present" if match else "missing"))
            else:
                status = "present" if match else "missing"
            source = "tmdb_id" if match else ""
            db.execute("""INSERT INTO anime_match_results
              (tmdb_id, media_type, season_number, episode_number, server_id, library_id, status, match_source,
               confidence, plex_rating_key, details, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
              (item["tmdb_id"], media_type, item["season_number"], item["episode_number"], server_id,
               match["library_id"] if match else None, status, source, 1.0 if match else 0,
               match["rating_key"] if match else "", "{}", utcnow()))
        db.commit()
        return len(items)

