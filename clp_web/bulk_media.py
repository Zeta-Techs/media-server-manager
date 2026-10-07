from __future__ import annotations

import json
import re
import unicodedata
from typing import Any
from urllib.parse import urlparse

from .catalog import sync_media_library_tmdb
from .db import connect, decode_payload, get_setting, utcnow
from .tmdb import TMDBClient

MAX_BULK_ROWS = 100
_TITLE_YEAR = re.compile(r"^(.+?)\s*[（(]?((?:19|20)\d{2})[）)]?$")
_TMDB_URL = re.compile(r"/(movie|tv)/(\d+)(?:[-/]|$)", re.I)


def decode_json(value: Any, default: Any = None) -> Any:
    """Decode batch JSON fields, preserving both object and array payloads."""
    if default is None:
        default = {}
    try:
        parsed = json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default
    return parsed


def parse_line(value: str) -> dict[str, Any]:
    text = str(value or "").strip()
    if not text:
        raise ValueError("输入为空")
    if text.isdigit():
        return {"kind": "id", "id": int(text), "media_type": ""}
    match = _TMDB_URL.search(urlparse(text).path)
    host = (urlparse(text).netloc or "").lower().split(":", 1)[0]
    if match and host in {"themoviedb.org", "www.themoviedb.org"}:
        return {"kind": "url", "id": int(match.group(2)), "media_type": "movie" if match.group(1).lower() == "movie" else "tv"}
    match = _TITLE_YEAR.match(text)
    if match:
        return {"kind": "title_year", "title": match.group(1).strip(), "year": int(match.group(2))}
    raise ValueError("请输入 TMDB ID、TMDB URL 或作品名+年份")


def _kind_from_payload(payload: dict[str, Any]) -> str:
    genres = {str(item.get("name") or "").casefold() for item in payload.get("genres") or []}
    keywords = {str(item.get("name") or "").casefold() for item in (payload.get("keywords") or {}).get("keywords", [])}
    return "动画" if "animation" in genres or "anime" in keywords else "真人"


def _summary(media_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    title = payload.get("title") or payload.get("name") or ""
    original = payload.get("original_title") or payload.get("original_name") or ""
    release = payload.get("release_date") or payload.get("first_air_date") or ""
    genres = [str(item.get("name") or "") for item in payload.get("genres") or [] if item.get("name")]
    external = payload.get("external_ids") or {}
    return {"media_type": media_type, "tmdb_id": int(payload.get("id") or 0), "imdb_id": str(payload.get("imdb_id") or external.get("imdb_id") or ""), "tvdb_id": str(payload.get("tvdb_id") or external.get("tvdb_id") or ""), "title": title,
            "original_title": original, "year": int(release[:4]) if release[:4].isdigit() else None,
            "poster_path": payload.get("poster_path") or "", "genres": genres,
            "animation_kind": _kind_from_payload(payload), "raw": payload}


def resolve_row(tmdb: TMDBClient, parsed: dict[str, Any]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    if parsed["kind"] == "title_year":
        for media_type, results in (("movie", tmdb.search_movie(parsed["title"], parsed["year"])), ("tv", tmdb.search_tv(parsed["title"], parsed["year"]))):
            candidates.extend([_summary(media_type, item) for item in results[:10]])
        if len(candidates) == 1:
            details = tmdb.details(candidates[0]["media_type"], candidates[0]["tmdb_id"])
            return {**_summary(candidates[0]["media_type"], details), "candidates": candidates, "selected": True, "status": "resolved"}
        return {"candidates": candidates, "selected": False, "status": "needs_selection" if candidates else "error", "message": "请选择 TMDB 候选" if candidates else "未找到匹配作品"}
    types = [parsed["media_type"]] if parsed.get("media_type") else ["movie", "tv"]
    direct_matches: list[dict[str, Any]] = []
    for media_type in types:
        try:
            details = tmdb.details(media_type, parsed["id"])
            direct_matches.append(_summary(media_type, details))
        except Exception:
            continue
    if len(direct_matches) == 1:
        return {**direct_matches[0], "candidates": [], "selected": True, "status": "resolved"}
    if len(direct_matches) > 1:
        return {"candidates": direct_matches, "selected": False, "status": "needs_selection", "message": "请选择电影或剧集类型"}
    return {"status": "error", "message": "TMDB 未找到对应作品", "candidates": []}


def row_dict(row: Any) -> dict[str, Any]:
    item = dict(row)
    custom = decode_json(item.get("custom_genres"), [])
    item["genres"] = custom if custom else decode_json(item.get("genres"), [])
    item["candidates"] = decode_json(item.get("candidates"), [])
    item["raw"] = decode_json(item.get("raw_json"), {})
    item.pop("raw_json", None)
    return item


def resolve_batch(db_file, batch_id: int, progress=None, cancelled=None) -> dict[str, int]:
    token = get_setting("tmdb_api_key", db_file)
    tmdb = TMDBClient(token)
    with connect(db_file) as db:
        rows = db.execute("SELECT * FROM media_library_bulk_rows WHERE batch_id=? ORDER BY line_number", (batch_id,)).fetchall()
        db.execute("UPDATE media_library_bulk_batches SET status='resolving', updated_at=? WHERE id=?", (utcnow(), batch_id))
        db.commit()
    errors = 0
    for index, row in enumerate(rows, 1):
        try:
            parsed = parse_line(row["input_text"])
            result = resolve_row(tmdb, parsed)
            result["input_kind"] = parsed["kind"]
            status = result.get("status", "error")
        except Exception as exc:
            result, status, errors = {"message": str(exc), "candidates": []}, "error", errors + 1
        with connect(db_file) as db:
            summary = result
            db.execute("""UPDATE media_library_bulk_rows SET input_kind=?, status=?, media_type=?, tmdb_id=?, title=?, original_title=?, year=?, poster_path=?, genres=?, animation_kind=?, candidates=?, selected=?, message=?, raw_json=?, updated_at=? WHERE id=?""",
                       (summary.get("input_kind", ""), status, summary.get("media_type", ""), summary.get("tmdb_id"), summary.get("title", ""), summary.get("original_title", ""), summary.get("year"), summary.get("poster_path", ""), json.dumps(summary.get("genres", []), ensure_ascii=False), summary.get("animation_kind", "真人"), json.dumps(summary.get("candidates", []), ensure_ascii=False), 1 if summary.get("selected") else 0, summary.get("message", ""), json.dumps(summary.get("raw", {}), ensure_ascii=False), utcnow(), row["id"]))
            db.commit()
        if progress: progress({"processed_delta": 1, "total": len(rows), "stage": "tmdb_bulk_resolve"})
        if cancelled and cancelled():
            raise RuntimeError("任务已取消")
    with connect(db_file) as db:
        status = "ready" if not errors else "ready"
        db.execute("UPDATE media_library_bulk_batches SET status=?, updated_at=? WHERE id=?", (status, utcnow(), batch_id))
        db.commit()
    return {"processed": len(rows), "errors": errors}


def _resolution(metadata: dict[str, Any]) -> str:
    values = []
    for media in metadata.get("Media") or []:
        for part in media.get("Part") or []:
            for stream in part.get("Stream") or []:
                if str(stream.get("streamType") or stream.get("stream_type") or "") == "1":
                    values.append(int(stream.get("height") or 0))
        values.append(int(media.get("height") or 0))
    height = max(values or [0])
    if height >= 1800: return "4K"
    if height >= 1000: return "1080P"
    if height >= 650: return "720P"
    return str(height) + "P" if height else ""


def _title_key(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", text)


def dedupe_system_media_items(db_file) -> int:
    """Remove stale system rows once a matching Plex row is present.

    Older batch imports could create a synthetic show before Plex metadata had
    a TMDB id.  A later Plex sync then left both rows behind.  TMDB identity is
    preferred; title/year is a conservative fallback for legacy rows.
    """
    removed = 0
    with connect(db_file) as db:
        managed = db.execute("SELECT * FROM media_library_items WHERE system_managed=1 AND parent_rating_key='' AND plex_type IN ('movie','show')").fetchall()
        for item in managed:
            kind = str(item["tmdb_media_type"] or ("movie" if item["plex_type"] == "movie" else "tv"))
            real_rows = db.execute("SELECT * FROM media_library_items WHERE server_id=? AND library_id=? AND system_managed=0 AND parent_rating_key='' AND plex_type=? AND rating_key<>?", (item["server_id"], item["library_id"], item["plex_type"], item["rating_key"])).fetchall()
            match = None
            for real in real_rows:
                if item["tmdb_id"] and real["tmdb_id"] and str(real["tmdb_media_type"] or kind) == kind and int(real["tmdb_id"]) == int(item["tmdb_id"]):
                    match = real; break
                item_raw = decode_payload(item["raw_json"])
                item_title = item["title"] or (item_raw.get("title") or item_raw.get("name"))
                item_original = item["original_title"] or (item_raw.get("original_title") or item_raw.get("original_name"))
                same_title = (_title_key(real["title"]) and _title_key(real["title"]) == _title_key(item_title)) or (_title_key(real["original_title"]) and _title_key(real["original_title"]) == _title_key(item_original))
                if int(real["year"] or 0) == int(item["year"] or 0) and same_title:
                    match = real; break
            if not match:
                continue
            if item["tmdb_id"] and not match["tmdb_id"]:
                db.execute("UPDATE media_library_items SET tmdb_media_type=?, tmdb_id=? WHERE server_id=? AND library_id=? AND rating_key=?", (kind, int(item["tmdb_id"]), match["server_id"], match["library_id"], match["rating_key"]))
            db.execute("DELETE FROM media_library_items WHERE server_id=? AND library_id=? AND parent_rating_key=?", (item["server_id"], item["library_id"], item["rating_key"]))
            db.execute("DELETE FROM media_library_items WHERE server_id=? AND library_id=? AND rating_key=?", (item["server_id"], item["library_id"], item["rating_key"]))
            removed += 1
        # Also collapse duplicate synthetic rows left by repeated batch
        # confirmation when Plex has not supplied a real row yet.
        duplicate_rows = db.execute("SELECT * FROM media_library_items WHERE system_managed=1 AND parent_rating_key='' AND plex_type IN ('movie','show') ORDER BY id").fetchall()
        kept = {}
        for item in duplicate_rows:
            identity = (item["server_id"], item["library_id"], item["tmdb_media_type"], item["tmdb_id"] or 0, int(item["year"] or 0), _title_key(item["title"]))
            if identity[-1] and identity in kept:
                old = kept[identity]
                db.execute("DELETE FROM media_library_items WHERE server_id=? AND library_id=? AND parent_rating_key=?", (item["server_id"], item["library_id"], item["rating_key"]))
                db.execute("DELETE FROM media_library_items WHERE server_id=? AND library_id=? AND rating_key=?", (item["server_id"], item["library_id"], item["rating_key"]))
                removed += 1
            else:
                kept[identity] = item
        if removed:
            db.commit()
    return removed


def confirm_batch(db_file, batch_id: int, payload: dict[str, Any], progress=None, cancelled=None) -> dict[str, int]:
    movie_library = int(payload.get("movie_library_id") or 0)
    show_library = int(payload.get("show_library_id") or 0)
    selections = payload.get("selections") or []
    added = skipped = errors = 0
    tmdb = TMDBClient(get_setting("tmdb_api_key", db_file))
    with connect(db_file) as db:
        batch = db.execute("SELECT * FROM media_library_bulk_batches WHERE id=?", (batch_id,)).fetchone()
        if batch is None: raise ValueError("批次不存在")
        rows = db.execute("SELECT * FROM media_library_bulk_rows WHERE batch_id=? ORDER BY line_number", (batch_id,)).fetchall()
        allowed = {int(item.get("row_id")): item for item in selections if item.get("row_id")}
        for row in rows:
            choice = allowed.get(int(row["id"]))
            if not choice or row["status"] not in {"resolved", "needs_selection"}: continue
            media_type = choice.get("media_type") or row["media_type"]
            tmdb_id = int(choice.get("tmdb_id") or row["tmdb_id"] or 0)
            stored_candidates = decode_json(row["candidates"], [])
            if row["status"] == "needs_selection":
                if not any(str(candidate.get("media_type")) == str(media_type) and int(candidate.get("tmdb_id") or 0) == tmdb_id for candidate in stored_candidates):
                    errors += 1
                    continue
            elif row["media_type"] and str(row["media_type"]) != str(media_type):
                errors += 1
                continue
            target = movie_library if media_type == "movie" else show_library
            if not tmdb_id or not target: errors += 1; continue
            library = db.execute("SELECT * FROM media_libraries WHERE server_id=? AND library_id=?", (batch["server_id"], target)).fetchone()
            expected = 1 if media_type == "movie" else 2
            if library is None or int(library["plex_type"]) != expected:
                errors += 1; continue
            existing = db.execute("SELECT * FROM media_library_items WHERE server_id=? AND library_id=? AND tmdb_media_type=? AND tmdb_id=? LIMIT 1", (batch["server_id"], target, media_type, tmdb_id)).fetchone()
            if existing is None:
                existing = db.execute("SELECT * FROM media_library_items WHERE server_id=? AND library_id=? AND system_managed=0 AND parent_rating_key='' AND plex_type=? AND year=?", (batch["server_id"], target, "movie" if media_type == "movie" else "show", row["year"] or 0)).fetchone()
                if existing is not None:
                    details_title = str(row["title"] or "")
                    if details_title and _title_key(existing["title"]) != _title_key(details_title):
                        existing = None
            if existing:
                skipped += 1; db.execute("UPDATE media_library_bulk_rows SET status='duplicate', message=?, updated_at=? WHERE id=?", ("目标媒体库已存在", utcnow(), row["id"])); continue
            raw = decode_payload(row["raw_json"])
            try:
                details = tmdb.details(media_type, tmdb_id)
                raw = details
            except Exception:
                details = raw
            if not raw:
                errors += 1
                continue
            title = str(raw.get("title") or raw.get("name") or row["title"] or "")
            original_title = str(raw.get("original_title") or raw.get("original_name") or row["original_title"] or "")
            release_date = str(raw.get("release_date") or raw.get("first_air_date") or "")
            year = int(release_date[:4]) if release_date[:4].isdigit() else row["year"]
            poster_path = str(raw.get("poster_path") or row["poster_path"] or "")
            key = f"tmdb:{media_type}:{tmdb_id}"
            custom_genres = choice.get("genres") or decode_json(row["custom_genres"], []) or decode_json(row["genres"], [])
            animation = choice.get("animation_kind") or row["custom_animation_kind"] or row["animation_kind"] or "真人"
            release = raw.get("release_date") or raw.get("first_air_date") or ""
            external = raw.get("external_ids") or {}
            db.execute("""INSERT INTO media_library_items (server_id,library_id,rating_key,plex_type,title,original_title,year,release_date,genre,thumb,raw_json,scanned_at,tmdb_media_type,tmdb_id,imdb_id,tvdb_id,source,system_managed,resolution,animation_kind,system_genres) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (batch["server_id"], target, key, "movie" if media_type == "movie" else "show", title, original_title, year, release, ", ".join(custom_genres), "", json.dumps({**raw, "poster_url": f"https://image.tmdb.org/t/p/w342{poster_path}" if poster_path else "", "external_ids": {"tmdb": tmdb_id, "imdb": external.get("imdb_id") or "", "tvdb": external.get("tvdb_id") or ""}, "tmdb": raw}, ensure_ascii=False), utcnow(), media_type, tmdb_id, str(external.get("imdb_id") or ""), str(external.get("tvdb_id") or ""), "system", 1, "", animation, json.dumps(custom_genres, ensure_ascii=False)))
            db.execute("UPDATE media_library_bulk_rows SET status='added', target_library_id=?, message='', updated_at=? WHERE id=?", (target, utcnow(), row["id"])); added += 1
        db.execute("UPDATE media_library_bulk_batches SET status='completed', updated_at=? WHERE id=?", (utcnow(), batch_id)); db.commit()
    for row in rows:
        if int(row["id"]) in {int(item.get("row_id")) for item in selections}:
            choice = next((item for item in selections if int(item.get("row_id")) == int(row["id"])), {})
            if choice.get("media_type") == "tv" and choice.get("tmdb_id"):
                # The TMDB pass accepts the synthetic top-level row and creates
                # its expected season/episode placeholders without contacting
                # Plex.  A later Plex sync will replace matching placeholders.
                try:
                    sync_media_library_tmdb(db_file, int(batch["server_id"]), int(choice.get("library_id") or show_library), rating_keys={f"tmdb:tv:{int(choice['tmdb_id'])}"})
                except Exception:
                    pass
    return {"added": added, "skipped": skipped, "errors": errors}


def add_system_media_item(db_file, server_id: int, library_id: int, media_type: str, tmdb_id: int) -> dict[str, Any]:
    """Add one TMDB item to the local media-library cache only."""
    if media_type not in {"movie", "tv"}:
        raise ValueError("仅支持电影或剧集")
    tmdb = TMDBClient(get_setting("tmdb_api_key", db_file))
    details = tmdb.details(media_type, int(tmdb_id))
    title = str(details.get("title") or details.get("name") or "")
    original_title = str(details.get("original_title") or details.get("original_name") or "")
    release = str(details.get("release_date") or details.get("first_air_date") or "")
    year = int(release[:4]) if release[:4].isdigit() else None
    poster_path = str(details.get("poster_path") or "")
    genres = [str(item.get("name") or "") for item in details.get("genres") or [] if item.get("name")]
    key = f"tmdb:{media_type}:{int(tmdb_id)}"
    with connect(db_file) as db:
        library = db.execute("SELECT * FROM media_libraries WHERE server_id=? AND library_id=?", (server_id, library_id)).fetchone()
        expected = 1 if media_type == "movie" else 2
        if library is None or int(library["plex_type"]) != expected:
            raise ValueError("目标媒体库类型与作品类型不匹配")
        existing = db.execute("SELECT * FROM media_library_items WHERE server_id=? AND library_id=? AND tmdb_media_type=? AND tmdb_id=? LIMIT 1", (server_id, library_id, media_type, int(tmdb_id))).fetchone()
        if existing is None:
            # Legacy Plex rows may not have a TMDB id yet. Avoid creating a
            # second synthetic row when title/year already identifies it.
            title_key, original_key = _title_key(title), _title_key(original_title)
            candidates = db.execute("SELECT * FROM media_library_items WHERE server_id=? AND library_id=? AND system_managed=0 AND parent_rating_key='' AND plex_type=?", (server_id, library_id, "movie" if media_type == "movie" else "show")).fetchall()
            existing = next((row for row in candidates if int(row["year"] or 0) == int(year or 0) and ((title_key and _title_key(row["title"]) == title_key) or (original_key and _title_key(row["original_title"]) == original_key))), None)
        if existing:
            db.execute("UPDATE media_library_items SET tmdb_media_type=?, tmdb_id=? WHERE server_id=? AND library_id=? AND rating_key=? AND (tmdb_id IS NULL OR tmdb_id=0)", (media_type, int(tmdb_id), server_id, library_id, existing["rating_key"]))
            db.commit()
            return {"status": "exists", "rating_key": existing["rating_key"], "title": existing["title"]}
        db.execute("""INSERT INTO media_library_items
          (server_id,library_id,rating_key,plex_type,title,original_title,year,release_date,genre,thumb,raw_json,scanned_at,
           tmdb_media_type,tmdb_id,imdb_id,tvdb_id,source,system_managed,resolution,animation_kind,system_genres)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (server_id, library_id, key, "movie" if media_type == "movie" else "show", title, original_title, year, release,
           ", ".join(genres), "", json.dumps({**details, "poster_url": f"https://image.tmdb.org/t/p/w342{poster_path}" if poster_path else "", "external_ids": {"tmdb": int(tmdb_id), "imdb": str((details.get("external_ids") or {}).get("imdb_id") or ""), "tvdb": str((details.get("external_ids") or {}).get("tvdb_id") or "")}}, ensure_ascii=False), utcnow(),
           media_type, int(tmdb_id), str((details.get("external_ids") or {}).get("imdb_id") or ""), str((details.get("external_ids") or {}).get("tvdb_id") or ""), "system", 1, "", "动画" if any(g.casefold() in {"animation", "anime", "动画"} for g in genres) else "真人", json.dumps(genres, ensure_ascii=False)))
        db.commit()
    if media_type == "tv":
        sync_media_library_tmdb(db_file, server_id, library_id, rating_keys={key})
    return {"status": "added", "rating_key": key, "title": title}
