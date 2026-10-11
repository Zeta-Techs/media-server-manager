"""Application workflows for Plex media-library synchronization."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Callable

from ...core import ServerConfig, TaskCancelled, extract_external_ids
from ...infrastructure.db.repositories.auth import AuthRepository
from ...infrastructure.db.repositories.media_library_sync import MediaLibrarySyncRepository
from ...infrastructure.db.repositories.servers import ServerRepository
from ...infrastructure.db.runtime import server_config_from_row
from ...infrastructure.db.session import Database
from ...infrastructure.integrations.plex.client import PlexServer
from ...infrastructure.integrations.tmdb import TMDBClient


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _match_title_key(value: Any) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").casefold())


def _server_config(database: Database, server_id: int) -> ServerConfig:
    with database.session() as session:
        server = ServerRepository(session).get(server_id)
        if server is None or not server.enabled:
            raise ValueError("服务器不存在或已停用")
        client_identifier = AuthRepository(session).setting("plex_client_identifier") or "media-server-manager"
        return ServerConfig(
            name=server.name,
            address=server.address,
            token=server.token,
            skip_libraries=[value for value in server.skip_libraries.replace("；", ";").split(";") if value],
            pinyin_mode=server.pinyin_mode,
            client_identifier=client_identifier,
        )


def _plex_date(metadata: dict[str, Any]) -> str:
    return str(metadata.get("originallyAvailableAt") or metadata.get("originally_available_at") or "")[:10]


def _plex_genres(metadata: dict[str, Any]) -> str:
    values: list[str] = []
    for field in ("Genre", "Label"):
        for item in metadata.get(field) or []:
            value = str(item.get("tag") or "").strip()
            if value and value not in values:
                values.append(value)
    return ", ".join(values)


def _plex_resolution(metadata: dict[str, Any]) -> str:
    heights: list[int] = []
    for media in metadata.get("Media") or []:
        try:
            heights.append(int(media.get("height") or 0))
        except (TypeError, ValueError):
            pass
        for part in media.get("Part") or []:
            for stream in part.get("Stream") or []:
                if str(stream.get("streamType") or "") != "1":
                    continue
                try:
                    heights.append(int(stream.get("height") or 0))
                except (TypeError, ValueError):
                    pass
    height = max(heights or [0])
    if height >= 1800:
        return "4K"
    if height >= 1000:
        return "1080P"
    if height >= 650:
        return "720P"
    return f"{height}P" if height else ""


def _is_animation(title: str, plex_type: int) -> bool:
    return int(plex_type) == 2 and any(value in str(title or "").casefold() for value in ("anime", "动画", "动漫"))


def _plex_values(metadata: dict[str, Any], raw_type: str) -> dict[str, Any]:
    ids = extract_external_ids(metadata)
    raw_duration = metadata.get("duration") or 0
    duration = int(raw_duration) if str(raw_duration).isdigit() else 0
    return {
        "title": str(metadata.get("title") or ""),
        "original_title": str(metadata.get("originalTitle") or ""),
        "year": metadata.get("year"),
        "added_at": metadata.get("addedAt"),
        "release_date": _plex_date(metadata),
        "rating": metadata.get("rating"),
        "audience_rating": metadata.get("audienceRating"),
        "duration": duration,
        "content_rating": str(metadata.get("contentRating") or ""),
        "genre": _plex_genres(metadata),
        "thumb": str(metadata.get("thumb") or metadata.get("parentThumb") or ""),
        "art": str(metadata.get("art") or ""),
        "scanned_at": _now(),
        "tmdb_media_type": "movie" if raw_type == "movie" else "tv" if raw_type == "show" else "",
        "tmdb_id": int(ids["tmdb"]) if ids.get("tmdb") else None,
        "imdb_id": ids.get("imdb") or "",
        "tvdb_id": ids.get("tvdb") or "",
        "source": "plex",
        "system_managed": 0,
        "resolution": _plex_resolution(metadata),
        "animation_kind": "",
        "system_genres": "[]",
        "external_ids": ids,
    }


def sync_media_library(
    database: Database,
    server_id: int,
    library_id: int,
    *,
    progress: Callable[[dict[str, Any]], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    rating_keys: set[str] | None = None,
) -> int:
    plex = PlexServer(_server_config(database, server_id), auto_login=False)
    library = next((row for row in plex.list_library() if int(row[0]) == library_id), None)
    if library is None:
        raise ValueError("媒体库不存在")
    plex_type, title = int(library[1]), str(library[2])
    with database.transaction() as uow:
        MediaLibrarySyncRepository(uow.session).upsert_library(
            server_id, library_id, title, plex_type, _is_animation(title, plex_type), _now()
        )
    if rating_keys:
        items = [plex.get_metadata(key) for key in sorted(rating_keys)]
        if any(str(item.get("type") or "") != "show" for item in items):
            raise ValueError("目标不是电视剧")
    else:
        items = plex.list_library_collections(library_id) if plex_type == 3 else plex.list_library_items(library_id, plex_type)
    seen_keys: set[str] = set()
    count = 0
    for item in items:
        if cancelled and cancelled():
            raise TaskCancelled("任务已取消")
        item_type = str(item.get("type") or ("collection" if plex_type == 3 else "show" if plex_type == 2 else "movie"))
        if item_type == "show" and not rating_keys:
            try:
                item = plex.get_metadata(str(item.get("ratingKey") or ""))
            except Exception:
                pass
        children: list[tuple[dict[str, Any], str, str, int | None, int | None]] = []
        if item_type == "show":
            show_key = str(item.get("ratingKey") or "")
            for season in plex.get_children(show_key):
                season_number = int(season.get("index") or season.get("parentIndex") or 0)
                children.append((season, "season", show_key, season_number, None))
            for episode in plex.list_show_episodes(show_key):
                children.append((episode, "episode", show_key, int(episode.get("parentIndex") or 0), int(episode.get("index") or 0)))
        entries: list[tuple[dict[str, Any], str, str, int | None, int | None]] = [
            (item, item_type, "", None, None),
            *children,
        ]
        with database.transaction() as uow:
            repository = MediaLibrarySyncRepository(uow.session)
            for metadata, child_type, parent, child_season_number, child_episode_number in entries:
                rating_key = str(metadata.get("ratingKey") or "")
                if rating_key:
                    seen_keys.add(rating_key)
                repository.upsert_plex_item(
                    server_id, library_id, metadata, plex_type=child_type,
                    parent_rating_key=parent, season_number=child_season_number, episode_number=child_episode_number,
                    values=_plex_values(metadata, child_type),
                )
                if child_type == "episode" and parent:
                    repository.remove_expected_episode(server_id, library_id, parent, int(child_season_number or 0), int(child_episode_number or 0))
        count += 1
        if progress:
            progress({"processed_delta": 1, "total": len(items), "stage": "plex_media_library", "library": title})
    with database.transaction() as uow:
        MediaLibrarySyncRepository(uow.session).finish_plex(server_id, library_id, seen_keys, bool(rating_keys), _now())
    return count


def sync_media_library_tmdb(
    database: Database,
    server_id: int,
    library_id: int,
    *,
    progress: Callable[[dict[str, Any]], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    rating_keys: set[str] | None = None,
    logger: Callable[[str], None] | None = None,
    strict_errors: bool = False,
) -> int:
    with database.session() as session:
        token = AuthRepository(session).setting("tmdb_api_key")
        rows = MediaLibrarySyncRepository(session).sync_candidates(server_id, library_id, rating_keys)
    if not token:
        raise ValueError("请先保存 TMDB API Key")
    if strict_errors and not rows:
        raise ValueError("未找到需要重新检查的电视剧缓存")
    tmdb = TMDBClient(token)
    failures: list[str] = []

    def report(message: str) -> None:
        failures.append(message)
        if logger:
            logger(message)

    for item in rows:
        if cancelled and cancelled():
            raise TaskCancelled("任务已取消")
        payload = _decode_json(item.get("raw_json"))
        ids = payload.get("external_ids") or extract_external_ids(payload)
        tmdb_id = ids.get("tmdb")
        if not tmdb_id and item["plex_type"] == "show":
            try:
                matches = tmdb.search_tv(str(item.get("title") or ""), int(item["year"]) if item.get("year") else None)
                if matches:
                    tmdb_id = int(matches[0].get("id") or 0) or None
                    if tmdb_id:
                        payload["external_ids"] = {**ids, "tmdb": tmdb_id}
            except Exception as exc:
                report(f"TMDB 搜索失败：{item['title']}：{exc}")
        if not tmdb_id:
            report(f"未找到 TMDB 匹配：{item['title']}")
        details: dict[str, Any] | None = None
        if tmdb_id:
            try:
                details = tmdb.details("tv" if item["plex_type"] == "show" else "movie", int(tmdb_id))
            except Exception as exc:
                cached = payload.get("tmdb")
                if isinstance(cached, dict) and cached:
                    details = cached
                else:
                    report(f"TMDB 详情获取失败：{item['title']}（{tmdb_id}）：{exc}")
        if details and tmdb_id:
            try:
                with database.transaction() as uow:
                    repository = MediaLibrarySyncRepository(uow.session)
                    repository.update_tmdb_projection(item, payload, details, _now())
                    if item["plex_type"] == "show":
                        for season_info in details.get("seasons") or []:
                            season_number = int(season_info.get("season_number") or 0)
                            season_details: dict[str, Any] | None = None
                            try:
                                season_details = tmdb.season_details(int(tmdb_id), season_number)
                            except Exception as exc:
                                cached_season = repository.cached_season(item, season_number)
                                cached_payload = _decode_json(cached_season.get("raw_json") if cached_season else "{}").get("tmdb")
                                season_details = cached_payload if isinstance(cached_payload, dict) else None
                                if season_details is None:
                                    report(f"TMDB 季详情获取失败：{item['title']} S{season_number:02d}（{tmdb_id}）：{exc}")
                            if not season_details:
                                continue
                            repository.season_placeholder(item, int(tmdb_id), season_number, season_details, _now())
                            for expected in season_details.get("episodes") or []:
                                episode_number = int(expected.get("episode_number") or 0)
                                if repository.real_episode_exists(item, season_number, episode_number):
                                    repository.remove_expected_episode(server_id, library_id, item["rating_key"], season_number, episode_number)
                                else:
                                    repository.episode_placeholder(item, int(tmdb_id), season_number, episode_number, expected, _now())
            except Exception as exc:
                report(f"TMDB 缺集写入失败：{item['title']}（{tmdb_id}）：{exc}")
        if progress:
            progress({"processed_delta": 1, "total": len(rows), "stage": "tmdb_media_library"})
    if strict_errors and failures:
        raise RuntimeError("；".join(failures))
    with database.transaction() as uow:
        MediaLibrarySyncRepository(uow.session).finish_tmdb(server_id, library_id, _now())
    return len(rows)


def reconcile(database: Database, server_id: int) -> int:
    with database.transaction() as uow:
        return MediaLibrarySyncRepository(uow.session).reconcile(server_id, _now())


def resolve_plex_rating_key(database: Database, server_id: int, library_id: int, rating_key: str) -> str:
    key = str(rating_key or "").strip()
    if not key.startswith("tmdb:"):
        return key
    with database.session() as session:
        repository = MediaLibrarySyncRepository(session)
        row = repository.item(server_id, library_id, key)
        server = repository.enabled_server(server_id)
    if row is None or server is None:
        raise ValueError("系统维护剧集缓存不存在，请先刷新媒体库")
    raw = _decode_json(row.get("raw_json"))
    ids = raw.get("external_ids") or extract_external_ids(raw)
    tmdb_id = int(row.get("tmdb_id") or ids.get("tmdb") or 0)
    if not tmdb_id:
        match = re.search(r"tmdb:tv:(\d+)$", key)
        tmdb_id = int(match.group(1)) if match else 0
    plex = PlexServer(server_config_from_row(server), auto_login=False)
    library = next((entry for entry in plex.list_library() if int(entry[0]) == library_id), None)
    if library is None or int(library[1]) != 2:
        raise ValueError("目标媒体库不是电视剧库")
    title_key, original_key = _match_title_key(row.get("title")), _match_title_key(row.get("original_title"))
    year = int(row.get("year") or 0)
    fallback = None
    for item in plex.list_library_items(library_id, 2):
        candidate_key = str(item.get("ratingKey") or "")
        if not candidate_key:
            continue
        candidate_ids = extract_external_ids(item)
        if tmdb_id and str(candidate_ids.get("tmdb") or "") == str(tmdb_id):
            return candidate_key
        candidate_year = int(item.get("year") or 0)
        if year and candidate_year and candidate_year != year:
            continue
        if title_key == _match_title_key(item.get("title")) or original_key == _match_title_key(item.get("originalTitle")):
            fallback = candidate_key
    if fallback:
        return fallback
    raise ValueError("Plex 媒体库中尚未找到对应的真实剧集，请确认媒体已完成入库并刷新 Plex 元数据")


def _decode_json(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


__all__ = ["reconcile", "resolve_plex_rating_key", "sync_media_library", "sync_media_library_tmdb"]
