# mypy: ignore-errors
# ruff: noqa: F821
from __future__ import annotations

from typing import Any

from flask import Blueprint

from ..dependencies import get_database, get_database_path


def register_routes(blueprint: Blueprint, context: dict[str, Any]) -> None:
    """Media-library and extended server routes."""
    # Route functions are registered here, while the application factory
    # supplies its request/session dependencies through this explicit context.
    from media_server_manager.api import app_factory as _factory

    globals().update(vars(_factory))
    globals().update(context)
    servers_bp = blueprint
    @servers_bp.post("/api/servers/<int:server_id>/test")
    @login_required
    def test_server(server_id: int):
        try:
            server = server_model_row(server_id)
            if server is None:
                return api_error("服务器不存在", "server_not_found", 404)
            plex = _factory.PlexServer(server_config_from_row(server), tags=load_tags(), auto_login=False)
            name = plex.login()
            return jsonify({"ok": True, "friendly_name": name})
        except Exception as exc:
            return api_error(str(exc), "plex_connection_failed", 400)

    @servers_bp.get("/api/servers/<int:server_id>/libraries")
    @login_required
    def server_libraries(server_id: int):
        try:
            server = server_model_row(server_id)
            if server is None:
                return api_error("服务器不存在", "server_not_found", 404)
            plex = _factory.PlexServer(server_config_from_row(server), tags=load_tags(), auto_login=True)
            upstream = [{"key": item[0], "type": item[1], "title": item[2]} for item in plex.list_library()]
            with get_database().transaction() as uow:
                repository = MediaLibraryRepository(uow.session)
                now = utcnow()
                repository.bootstrap(
                    server_id,
                    [(int(item["key"]), int(item["type"]), item["title"]) for item in upstream],
                    now,
                    _library_name_is_animation,
                )
                settings = {int(row["library_id"]): row for row in repository.all_for_server(server_id)}
            libraries = [
                {
                    **item,
                    "animation_mode": settings.get(int(item["key"]), {}).get("animation_mode", "auto"),
                    "auto_animation": bool(
                        settings.get(int(item["key"]), {}).get(
                            "auto_animation", _library_name_is_animation(item["title"], item["type"])
                        )
                    ),
                    "is_animation": _library_is_animation(
                        settings.get(int(item["key"]), {}).get("animation_mode", "auto"),
                        item["title"],
                        item["type"],
                    ),
                }
                for item in upstream
            ]
            return jsonify(libraries)
        except Exception as exc:
            return api_error(str(exc), "plex_library_failed", 400)

    @servers_bp.put("/api/servers/<int:server_id>/libraries/<int:library_id>/settings")
    @login_required
    def update_library_settings(server_id: int, library_id: int):
        data = request.get_json(force=True) or {}
        mode = str(data.get("animation_mode") or "auto")
        sort_key = data.get("sort_key")
        sort_direction = data.get("sort_direction")
        auto_recheck = data.get("auto_recheck_new_episodes")
        if "auto_recheck_new_episodes" in data and not isinstance(auto_recheck, bool):
            return api_error("自动重检开关必须是布尔值", "invalid_auto_recheck", 400)
        if mode not in {"auto", "animation", "normal"}:
            return api_error("动画媒体库模式无效", "invalid_animation_mode", 400)
        allowed_sort_keys = {
            "name",
            "original_title",
            "year",
            "added_at",
            "release_date",
            "first_episode_date",
            "latest_added_at",
            "rating",
            "audience_rating",
            "duration",
            "season_count",
            "episode_count",
            "content_rating",
            "genre",
        }
        if sort_key is not None and str(sort_key) not in allowed_sort_keys:
            return api_error("媒体库排序字段无效", "invalid_media_library_sort", 400)
        if sort_direction is not None and str(sort_direction) not in {"asc", "desc"}:
            return api_error("媒体库排序方向无效", "invalid_media_library_sort_direction", 400)
        with get_database().transaction() as uow:
            repository = MediaLibraryRepository(uow.session)
            library = repository.get(server_id, library_id)
            if library is None:
                return api_error("媒体库尚未同步，请先从媒体服务器重新拉取", "library_cache_missing", 404)
            if "animation_mode" not in data:
                mode = library["animation_mode"]
            values: dict[str, Any] = {"animation_mode": mode, "updated_at": utcnow()}
            if sort_key is not None:
                values["sort_key"] = str(sort_key)
            if sort_direction is not None:
                values["sort_direction"] = str(sort_direction)
            if auto_recheck is not None:
                if int(library["plex_type"]) != 2:
                    return api_error(
                        "只有电视剧媒体库支持新增剧集自动重检", "invalid_auto_recheck_library", 400
                    )
                values["auto_recheck_new_episodes"] = 1 if bool(auto_recheck) else 0
            repository.update_settings(server_id, library_id, values)
        return jsonify(
            {
                "ok": True,
                "animation_mode": mode,
                "sort_key": sort_key,
                "sort_direction": sort_direction,
                "auto_recheck_new_episodes": bool(auto_recheck) if auto_recheck is not None else None,
                "is_animation": _library_is_animation(mode, library["title"], library["plex_type"]),
            }
        )

    @servers_bp.put("/api/servers/<int:server_id>/media-library/order")
    @login_required
    def update_media_library_order(server_id: int):
        data = request.get_json(force=True) or {}
        order = data.get("order") or []
        if not isinstance(order, list) or any(str(item).strip() == "" for item in order):
            return api_error("媒体库顺序无效", "invalid_media_library_order", 400)
        with get_database().transaction() as uow:
            repository = MediaLibraryRepository(uow.session)
            existing = repository.library_ids(server_id)
            try:
                requested = [int(item) for item in order]
            except (TypeError, ValueError):
                return api_error("媒体库顺序无效", "invalid_media_library_order", 400)
            if set(requested) != existing or len(requested) != len(existing):
                return api_error("媒体库顺序必须包含全部媒体库", "invalid_media_library_order", 400)
            repository.reorder(server_id, requested, utcnow())
        return jsonify({"ok": True, "order": requested})

    @servers_bp.get("/api/servers/<int:server_id>/media-image")
    @login_required
    def media_image(server_id: int):
        server = server_model_row(server_id, enabled_only=True)
        if server is None:
            return api_error("服务器不存在或已停用", "server_not_found", 404)
        try:
            path = validate_media_path(request.args.get("path", ""))
            plex = _factory.PlexServer(server_config_from_row(server), tags=load_tags(), auto_login=False)
            upstream = plex.fetch_media_image(path)
            content_type = upstream.headers.get("Content-Type", "").split(";", 1)[0].lower()
            if not content_type.startswith("image/"):
                return api_error("Plex 返回的资源不是图片", "invalid_media_image", 502)
            content = upstream.content
            if len(content) > 8 * 1024 * 1024:
                return api_error("媒体图片超过大小限制", "media_image_too_large", 502)
            response = Response(content, status=200, content_type=content_type)
            response.headers["Cache-Control"] = "private, max-age=3600"
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response
        except ValueError as exc:
            return api_error(str(exc), "invalid_media_path", 400)
        except Exception as exc:
            return api_error(str(exc), "media_image_failed", 502)

    @servers_bp.get("/api/servers/<int:server_id>/media-library")
    @login_required
    def media_library(server_id: int):
        if server_model_row(server_id, enabled_only=True) is None:
            return api_error("服务器不存在或已停用", "server_not_found", 404)
        with get_database().session() as db:
            repository = MediaLibraryRepository(db)
            libraries = repository.list_with_jobs(server_id)
        if not libraries:
            try:
                server_row = server_model_row(server_id, enabled_only=True)
                plex = _factory.PlexServer(server_config_from_row(server_row), tags=load_tags(), auto_login=True)
                now = utcnow()
                with get_database().transaction() as uow:
                    repository = MediaLibraryRepository(uow.session)
                    repository.bootstrap(
                        server_id,
                        [(int(key), int(plex_type), str(title)) for key, plex_type, title in plex.list_library()],
                        now,
                        _library_name_is_animation,
                    )
                with get_database().session() as db:
                    libraries = MediaLibraryRepository(db).list_with_jobs(server_id)
            except Exception as exc:
                return api_error(str(exc), "media_library_bootstrap_failed", 400)
        with get_database().session() as db:
            repository = MediaLibraryRepository(db)
            output = []
            for library in libraries:
                count, episode_count = repository.item_counts(server_id, int(library["library_id"]))
                output.append(
                    {
                        "id": int(library["library_id"]),
                        "title": library["title"],
                        "plex_type": int(library["plex_type"]),
                        "kind": "movie"
                        if int(library["plex_type"]) == 1
                        else "show"
                        if int(library["plex_type"]) == 2
                        else "collection",
                        "items": [],
                        "item_count": int(count),
                        "episode_count": int(episode_count),
                        "animation_mode": library["animation_mode"],
                        "sort_key": library["sort_key"],
                        "sort_direction": library["sort_direction"],
                        "auto_recheck_new_episodes": bool(library["auto_recheck_new_episodes"]),
                        "display_order": int(library["display_order"] or 0),
                        "auto_animation": bool(library["auto_animation"]),
                        "is_animation": _library_is_animation(
                            library["animation_mode"], library["title"], library["plex_type"]
                        ),
                        "plex_synced_at": library["plex_synced_at"],
                        "tmdb_synced_at": library["tmdb_synced_at"],
                        "sync_status": library["job_status"] or library["sync_status"],
                        "sync_job_id": library["sync_job_id"],
                        "sync_error": library["job_error"] or library["sync_error"],
                        "sync_stage": library["job_stage"] or "",
                        "sync_current_library": library["job_current_library"] or "",
                        "sync_processed": int(library["job_processed"] or 0),
                        "sync_total": int(library["job_total"] or 0),
                    }
                )
        return jsonify({"server_id": server_id, "libraries": output, "generated_at": utcnow()})

    @servers_bp.get("/api/servers/<int:server_id>/media-library/<int:library_id>/items")
    @login_required
    def media_library_items(server_id: int, library_id: int):
        full_mode = str(request.args.get("mode") or "").lower() in {"full", "all", "summary"}
        try:
            offset = max(0, int(request.args.get("offset", 0)))
            limit = min(200, max(1, int(request.args.get("limit", 40))))
        except (TypeError, ValueError):
            return api_error("分页参数无效", "invalid_media_library_pagination", 400)
        sort = str(request.args.get("sort") or "name")
        direction = str(request.args.get("direction") or "asc").lower()
        allowed_sort = {
            "name",
            "original_title",
            "year",
            "added_at",
            "release_date",
            "rating",
            "audience_rating",
            "duration",
            "season_count",
            "episode_count",
            "content_rating",
            "genre",
        }
        if sort not in allowed_sort:
            return api_error("媒体库排序字段无效", "invalid_media_library_sort", 400)
        if direction not in {"asc", "desc"}:
            return api_error("媒体库排序方向无效", "invalid_media_library_sort_direction", 400)
        with get_database().session() as db:
            repository = MediaLibraryRepository(db)
            library = repository.get(server_id, library_id)
            if library is None:
                return api_error("媒体库缓存不存在，请先同步", "library_cache_missing", 404)
            rows = repository.parent_items(server_id, library_id)
            counts_by_parent = repository.child_counts(server_id, library_id)
            items = []
            seen_tmdb: set[tuple[str, int]] = set()
            real_signatures = {
                (
                    "movie" if row["plex_type"] == "movie" else "tv",
                    int(row["year"] or 0),
                    _title_key(row["title"]),
                )
                for row in rows
                if not row["system_managed"]
                and row["plex_type"] in {"movie", "show"}
                and _title_key(row["title"])
            }
            for row in rows:
                item = dict(row)
                tmdb_key = (
                    str(item.get("tmdb_media_type") or item.get("plex_type") or ""),
                    int(item.get("tmdb_id") or 0),
                )
                if (
                    item.get("system_managed")
                    and tmdb_key[1]
                    and repository.has_real_tmdb(server_id, library_id, tmdb_key[0], tmdb_key[1])
                ):
                    continue
                signature = (
                    "movie" if row["plex_type"] == "movie" else "tv",
                    int(row["year"] or 0),
                    _title_key(row["title"]),
                )
                if item.get("system_managed") and signature in real_signatures:
                    continue
                if tmdb_key[1] and tmdb_key in seen_tmdb:
                    continue
                if tmdb_key[1]:
                    seen_tmdb.add(tmdb_key)
                raw = decode_payload(item.pop("raw_json", "{}"))
                external_ids = raw.get("external_ids") or extract_external_ids(raw)
                poster_url = str(raw.get("poster_url") or "")
                show_kind = item.get("animation_kind") or (
                    "动画"
                    if library["plex_type"] == 2
                    and _library_is_animation(
                        library["animation_mode"], library["title"], library["plex_type"]
                    )
                    else "真人"
                )
                aggregate = counts_by_parent.get(str(row["rating_key"]), {})
                items.append(
                    {
                        "rating_key": str(item.get("rating_key") or ""),
                        "title": str(item.get("title") or ""),
                        "original_title": str(item.get("original_title") or ""),
                        "year": item.get("year"),
                        "plex_type": str(item.get("plex_type") or ""),
                        "genre": str(item.get("genre") or ""),
                        "system_genres": decode_json(item.get("system_genres"), []),
                        "content_rating": str(item.get("content_rating") or ""),
                        "summary": str(item.get("summary") or ""),
                        "duration": item.get("duration") or 0,
                        "resolution": str(item.get("resolution") or ""),
                        "rating": item.get("rating"),
                        "audience_rating": item.get("audience_rating"),
                        "added_at": item.get("added_at") or "",
                        "release_date": item.get("release_date") or "",
                        "thumb": str(item.get("thumb") or ""),
                        "art": str(item.get("art") or ""),
                        "poster_url": poster_url,
                        "tmdb_id": item.get("tmdb_id"),
                        "tmdb_media_type": str(item.get("tmdb_media_type") or ""),
                        "imdb_id": str(item.get("imdb_id") or ""),
                        "tvdb_id": str(item.get("tvdb_id") or ""),
                        "external_ids": external_ids,
                        "library_id": library_id,
                        "show_kind": show_kind,
                        "source": str(item.get("source") or "plex"),
                        "system_managed": bool(item.get("system_managed")),
                        "season_count": int(aggregate.get("season_count") or 0),
                        "episode_count": int(aggregate.get("episode_count") or 0),
                        "missing_count": int(aggregate.get("missing_count") or 0),
                    }
                )
            items = _sort_items(items, sort, direction)
            total = len(items)
            page = items if full_mode else items[offset : offset + limit]
            response_offset = 0 if full_mode else offset
            response_limit = total if full_mode else limit
            response_has_more = False if full_mode else offset + len(page) < total
        return jsonify(
            {
                "server_id": server_id,
                "library_id": library_id,
                "items": page,
                "total": total,
                "offset": response_offset,
                "limit": response_limit,
                "has_more": response_has_more,
                "mode": "full" if full_mode else "paged",
            }
        )

    @servers_bp.get("/api/servers/<int:server_id>/media-library/search")
    @login_required
    def search_media_library(server_id: int):
        query = str(request.args.get("q") or "").strip()
        media_type = str(request.args.get("media_type") or "all").strip().lower()
        year_text = str(request.args.get("year") or "").strip()
        include_tmdb = str(request.args.get("include_tmdb", "1")).lower() not in {"0", "false", "no"}
        try:
            year = int(year_text) if year_text else None
            if year is not None and not 1800 <= year <= 2200:
                raise ValueError
        except ValueError:
            return api_error("年份无效", "invalid_search_year", 400)
        if media_type not in {"all", "movie", "tv"}:
            return api_error("媒体类型无效", "invalid_search_media_type", 400)
        if not query and year is None and media_type == "all":
            return api_error("请输入关键词、类型或年份", "empty_media_search", 400)
        try:
            limit = min(max(int(request.args.get("limit") or 20), 1), 50)
        except ValueError:
            limit = 20
        wanted_types = [media_type] if media_type in {"movie", "tv"} else ["movie", "tv"]
        if server_model_row(server_id, enabled_only=True) is None:
            return api_error("服务器不存在或已停用", "server_not_found", 404)
        with get_database().session() as db:
            media_repository = MediaLibraryRepository(db)
            libraries = {int(row["library_id"]): row for row in media_repository.all_for_server(server_id)}
            rows = media_repository.searchable_items(server_id)
        local: dict[tuple[str, int | str], dict[str, Any]] = {}
        # Keep an index of every local TMDB-backed item even when its cached
        # title does not match the text query. System-maintained rows can have
        # stale/garbled localized title columns; the stable TMDB identity must
        # still allow them to merge with the TMDB search result.
        local_tmdb_index: dict[tuple[str, int], dict[str, Any]] = {}
        needle = query.casefold()
        for row in rows:
            kind = str(row["tmdb_media_type"] or ("movie" if row["plex_type"] == "movie" else "tv"))
            if kind not in wanted_types:
                continue
            if year is not None and int(row["year"] or 0) != year:
                continue
            raw = decode_payload(row["raw_json"])
            tmdb_id = int(row["tmdb_id"] or (raw.get("external_ids") or {}).get("tmdb") or 0)
            external_ids = raw.get("external_ids") or extract_external_ids(raw)
            imdb_id = str(row["imdb_id"] or external_ids.get("imdb") or "")
            tvdb_id = str(row["tvdb_id"] or external_ids.get("tvdb") or "")
            # System-maintained rows are seeded from TMDB and may have a
            # damaged/empty localized title in the legacy title columns.
            # Prefer the cached TMDB payload for display and matching so the
            # item remains searchable even when the remote TMDB request is
            # unavailable or no longer returns the same search hit.
            tmdb_payload = raw.get("tmdb")
            cached_tmdb = tmdb_payload if isinstance(tmdb_payload, dict) else raw
            cached_title = str(cached_tmdb.get("title") or cached_tmdb.get("name") or "")
            cached_original = str(cached_tmdb.get("original_title") or cached_tmdb.get("original_name") or "")
            title = str(row["title"] or "")
            original_title = str(row["original_title"] or "")
            if cached_title and (not title or "\ufffd" in title):
                title = cached_title
            if cached_original and (not original_title or "\ufffd" in original_title):
                original_title = cached_original
            if tmdb_id and (year is None or int(row["year"] or 0) == year):
                local_key = (kind, tmdb_id)
                indexed = local_tmdb_index.get(local_key)
                if indexed is None or (indexed.get("system_managed") and not int(row["system_managed"] or 0)):
                    indexed = {
                        "source": "local",
                        "media_type": kind,
                        "tmdb_id": tmdb_id or None,
                        "imdb_id": imdb_id,
                        "tvdb_id": tvdb_id,
                        "title": title,
                        "original_title": original_title,
                        "year": row["year"],
                        "thumb": row["thumb"] or "",
                        "poster_url": raw.get("poster_url") or "",
                        "genres": decode_json(row["system_genres"], [])
                        or [x.strip() for x in str(row["genre"] or "").split(",") if x.strip()],
                        "library_ids": [],
                        "library_titles": [],
                        "locations": [],
                        "source_labels": [],
                        "library_id": int(row["library_id"]),
                        "rating_key": str(row["rating_key"]),
                        "system_managed": bool(row["system_managed"]),
                        "plex_present": not bool(row["system_managed"]),
                        "resolution": row["resolution"] or "",
                        "can_add": False,
                        "duplicate_reason": "已存在于媒体库",
                        "_real": not bool(row["system_managed"]),
                    }
                    local_tmdb_index[local_key] = indexed
                indexed["library_ids"].append(int(row["library_id"]))
                indexed["locations"].append(
                    {
                        "library_id": int(row["library_id"]),
                        "rating_key": str(row["rating_key"]),
                        "media_type": kind,
                    }
                )
                library_row = libraries.get(int(row["library_id"]))
                indexed["library_titles"].append(
                    str(library_row["title"] if library_row else row["library_id"])
                )
                indexed["source_labels"].append(
                    "本系统" if bool(row["system_managed"]) else f"媒体服务器：{title}"
                )
            haystack = " ".join(
                [title, original_title, cached_title, cached_original, str(tmdb_id), imdb_id, tvdb_id]
            ).casefold()
            if needle and needle not in haystack:
                continue
            key = (kind, tmdb_id) if tmdb_id else (kind, f"local:{row['rating_key']}")
            item = local.get(key)
            if item is None or (item.get("system_managed") and not int(row["system_managed"] or 0)):
                item = {
                    "source": "local",
                    "media_type": kind,
                    "tmdb_id": tmdb_id or None,
                    "imdb_id": imdb_id,
                    "tvdb_id": tvdb_id,
                    "title": title,
                    "original_title": original_title,
                    "year": row["year"],
                    "thumb": row["thumb"] or "",
                    "poster_url": raw.get("poster_url") or "",
                    "genres": decode_json(row["system_genres"], [])
                    or [x.strip() for x in str(row["genre"] or "").split(",") if x.strip()],
                    "library_ids": [],
                    "library_titles": [],
                    "locations": [],
                    "source_labels": [],
                    "library_id": int(row["library_id"]),
                    "rating_key": str(row["rating_key"]),
                    "system_managed": bool(row["system_managed"]),
                    "plex_present": not bool(row["system_managed"]),
                    "resolution": row["resolution"] or "",
                    "can_add": False,
                    "duplicate_reason": "已存在于媒体库",
                    "_real": not bool(row["system_managed"]),
                }
                local[key] = item
            item["library_ids"].append(int(row["library_id"]))
            item["locations"].append(
                {
                    "library_id": int(row["library_id"]),
                    "rating_key": str(row["rating_key"]),
                    "media_type": kind,
                }
            )
            library_row = libraries.get(int(row["library_id"]))
            item["library_titles"].append(str(library_row["title"] if library_row else row["library_id"]))
            item["source_labels"].append("本系统" if bool(row["system_managed"]) else f"媒体服务器：{title}")
        # Keep local TMDB-backed entries visible even when the remote search
        # does not return them. This is essential for system-maintained items:
        # adding them must be observable from the local database alone.
        for key, indexed in local_tmdb_index.items():
            haystack = " ".join(
                [
                    str(indexed.get("title") or ""),
                    str(indexed.get("original_title") or ""),
                    str(indexed.get("tmdb_id") or ""),
                ]
            ).casefold()
            if (not needle or needle in haystack) and key not in local:
                local[key] = indexed
        for item in local.values():
            item["library_ids"] = list(dict.fromkeys(item.get("library_ids") or []))
            item["library_titles"] = list(dict.fromkeys(item.get("library_titles") or []))
            item["source_labels"] = list(dict.fromkeys(item.get("source_labels") or []))
            seen_locations: set[tuple[Any, Any]] = set()
            unique_locations = []
            for loc in item.get("locations") or []:
                loc_key = (loc.get("library_id"), loc.get("rating_key"))
                if loc_key not in seen_locations:
                    seen_locations.add(loc_key)
                    unique_locations.append(loc)
            item["locations"] = unique_locations
        tmdb_items = {}
        # The local TMDB catalog is a separate source from both Plex and live
        # TMDB requests. Include it in the same identity merge.
        with get_database().session() as db:
            catalog_rows = CatalogRepository(db).movie_and_tv_items()
        for row in catalog_rows:
            release = str(row["release_date"] or "")
            catalog_year = int(release[:4]) if release[:4].isdigit() else None
            if media_type != "all" and row["media_type"] != media_type:
                continue
            if year is not None and catalog_year != year:
                continue
            catalog_raw = decode_payload(row["raw_json"])
            catalog_ids = catalog_raw.get("external_ids") or {}
            catalog_imdb = str(row["imdb_id"] or catalog_ids.get("imdb") or catalog_ids.get("imdb_id") or "")
            catalog_tvdb = str(row["tvdb_id"] or catalog_ids.get("tvdb") or catalog_ids.get("tvdb_id") or "")
            haystack = " ".join(
                [
                    str(row["title"] or ""),
                    str(row["original_title"] or ""),
                    str(row["tmdb_id"]),
                    catalog_imdb,
                    catalog_tvdb,
                ]
            ).casefold()
            if needle and needle not in haystack:
                continue
            key = (str(row["media_type"]), int(row["tmdb_id"]))
            genres = decode_json(row["genres"], [])
            if genres and isinstance(genres[0], dict):
                genres = [str(g.get("name") or g.get("id") or "") for g in genres]
            tmdb_items[key] = {
                "source": "catalog",
                "media_type": str(row["media_type"]),
                "tmdb_id": int(row["tmdb_id"]),
                "imdb_id": catalog_imdb,
                "tvdb_id": catalog_tvdb,
                "title": row["title"],
                "original_title": row["original_title"],
                "year": catalog_year,
                "poster_url": f"https://image.tmdb.org/t/p/w342{row['poster_path']}"
                if row["poster_path"]
                else "",
                "genres": genres,
                "library_ids": [],
                "library_titles": [],
                "source_labels": ["本系统"],
                "system_managed": False,
                "plex_present": False,
                "resolution": "",
                "can_add": False,
                "duplicate_reason": "本系统资料库",
            }
        for key, item in list(tmdb_items.items()):
            local_item = local.get(key) or local_tmdb_index.get(key)
            if local_item is None:
                local_item = next(
                    (
                        candidate
                        for candidate in local.values()
                        if candidate.get("media_type") == item.get("media_type")
                        and int(candidate.get("year") or 0) == int(item.get("year") or 0)
                        and (
                            _title_key(candidate.get("title")) == _title_key(item.get("title"))
                            or _title_key(candidate.get("original_title"))
                            == _title_key(item.get("original_title"))
                        )
                    ),
                    None,
                )
            if local_item:
                local[key] = local_item
                if not local_item.get("_real"):
                    local_item.update(
                        {
                            field: item[field]
                            for field in (
                                "title",
                                "original_title",
                                "year",
                                "poster_url",
                                "genres",
                                "imdb_id",
                                "tvdb_id",
                            )
                            if field in item and item[field]
                        }
                    )
                local_item["source_labels"] = list(
                    dict.fromkeys((local_item.get("source_labels") or []) + ["本系统"])
                )
                local_item["source"] = "merged"
                local_item["can_add"] = False
                tmdb_items.pop(key, None)
        warning = ""
        if include_tmdb:
            try:
                with get_database().session() as db:
                    token = AuthRepository(db).setting("tmdb_api_key")
                if token:
                    tmdb = TMDBClient(token)
                    parsed = None
                    if query:
                        try:
                            parsed = parse_line(query)
                        except ValueError:
                            parsed = None
                    for kind in wanted_types:
                        results: list[dict[str, Any]] = []
                        if query.lower().startswith("tt") or query.lower().startswith("tvdb:"):
                            try:
                                external_value = query.split(":", 1)[1] if ":" in query else query
                                external_data = tmdb.find_by_external_id(external_value)
                                results = (
                                    (external_data.get("movie_results") or [])
                                    if kind == "movie"
                                    else (external_data.get("tv_results") or [])
                                )
                            except Exception:
                                results = []
                        elif parsed and parsed.get("kind") in {"id", "url"}:
                            if parsed.get("media_type") in {"", kind}:
                                try:
                                    results = [tmdb.details(kind, int(parsed["id"]))]
                                except Exception:
                                    results = []
                        elif query:
                            results = (
                                tmdb.search_movie(query, year)
                                if kind == "movie"
                                else tmdb.search_tv(query, year)
                            )
                        else:
                            data = tmdb.discover(kind, year=year, sort_by="popularity.desc")
                            results = data.get("results") or []
                        for payload in results[:limit]:
                            item = _summary(kind, payload)
                            if not item.get("genres") and payload.get("id"):
                                try:
                                    item = _summary(
                                        kind, {**payload, **tmdb.details(kind, int(payload["id"]))}
                                    )
                                except Exception:
                                    pass
                            if year is not None and item.get("year") != year:
                                continue
                            key = (kind, int(item["tmdb_id"]))
                            previous = tmdb_items.get(key)
                            tmdb_items[key] = {
                                "source": "tmdb",
                                "media_type": kind,
                                "tmdb_id": int(item["tmdb_id"]),
                                "imdb_id": item.get("imdb_id") or "",
                                "tvdb_id": item.get("tvdb_id") or "",
                                "title": item["title"],
                                "original_title": item["original_title"],
                                "year": item["year"],
                                "poster_url": f"https://image.tmdb.org/t/p/w342{item['poster_path']}"
                                if item.get("poster_path")
                                else "",
                                "genres": item.get("genres") or [],
                                "library_ids": [],
                                "library_titles": [],
                                "source_labels": list(
                                    dict.fromkeys((previous or {}).get("source_labels", []) + ["TMDB"])
                                ),
                                "system_managed": False,
                                "plex_present": False,
                                "resolution": "",
                                "can_add": True,
                                "duplicate_reason": "",
                            }
                for key, item in tmdb_items.items():
                    local_item = local.get(key) or local_tmdb_index.get(key)
                    if local_item:
                        local[key] = local_item
                        # Real Plex metadata remains authoritative. For a
                        # system-maintained row, keep the TMDB result's title,
                        # year, poster and genres while attaching local
                        # library/resource state.
                        if not local_item.get("_real"):
                            local_item.update(
                                {
                                    field: item[field]
                                    for field in ("title", "original_title", "year", "poster_url", "genres")
                                    if field in item
                                }
                            )
                        local_item["source_labels"] = list(
                            dict.fromkeys(
                                (local_item.get("source_labels") or [])
                                + ["TMDB" if item.get("source") == "tmdb" else "本系统"]
                            )
                        )
                        local_item["source"] = "merged"
                        local_item["can_add"] = False
                        local_item["duplicate_reason"] = "已存在于媒体库"
                        local_item["source_labels"] = list(
                            dict.fromkeys(local_item.get("source_labels") or [])
                        )
                for key, item in tmdb_items.items():
                    if key not in local:
                        item["can_add"] = item.get("source") == "tmdb"
            except Exception as exc:
                warning = str(exc)
        merged = list(local.values()) + [item for key, item in tmdb_items.items() if key not in local]
        return jsonify(
            {
                "query": {"q": query, "media_type": media_type, "year": year},
                "local_count": len(local),
                "tmdb_count": len(tmdb_items),
                "results": merged[: limit * len(wanted_types)],
                "warning": warning,
            }
        )

    @servers_bp.post("/api/servers/<int:server_id>/media-library/search/add")
    @login_required
    def add_media_search_result(server_id: int):
        data = request.get_json(force=True) or {}
        try:
            media_type, tmdb_id, library_id = (
                str(data.get("media_type") or ""),
                int(data.get("tmdb_id") or 0),
                int(data.get("library_id") or 0),
            )
        except (TypeError, ValueError):
            return api_error("添加参数无效", "invalid_media_search_add", 400)
        if media_type not in {"movie", "tv"} or tmdb_id <= 0 or library_id <= 0:
            return api_error("添加参数无效", "invalid_media_search_add", 400)
        with get_database().session() as db:
            repository = MediaLibraryRepository(db)
            if repository.enabled_server(server_id) is None:
                return api_error("服务器不存在或已停用", "server_not_found", 404)
            lib = repository.get(server_id, library_id)
            if lib is None or int(lib["plex_type"]) != (1 if media_type == "movie" else 2):
                return api_error("目标媒体库类型不匹配", "invalid_target_library", 400)
        # Keep the server id in the payload as well as on the job row. The
        # worker uses the payload when it writes the system-maintained item;
        # omitting it made every search-add task fail before doing any work.
        job_id = manager.create_job(
            "media_library_search_add",
            server_id,
            {"server_id": server_id, "media_type": media_type, "tmdb_id": tmdb_id, "library_id": library_id},
        )
        return jsonify({"job_id": job_id})

    @servers_bp.post("/api/servers/<int:server_id>/media-library/<int:library_id>/refresh")
    @login_required
    def refresh_media_library(server_id: int, library_id: int):
        try:
            job_id = manager.create_job(
                "media_library_full_refresh", server_id, {"server_id": server_id, "library_id": library_id}
            )
            with get_database().transaction() as uow:
                MediaLibraryRepository(uow.session).update_sync_job(server_id, library_id, job_id, utcnow())
            return jsonify({"id": job_id, "workflow": "plex_then_tmdb"})
        except (TypeError, ValueError) as exc:
            return api_error(str(exc), "invalid_media_library_refresh", 400)

    @servers_bp.post("/api/servers/<int:server_id>/media-library/<int:library_id>/tmdb-refresh")
    @login_required
    def refresh_media_library_tmdb(server_id: int, library_id: int):
        try:
            # Keep the legacy endpoint compatible with older clients while
            # applying the same Plex-first, TMDB-second workflow.
            job_id = manager.create_job(
                "media_library_full_refresh", server_id, {"server_id": server_id, "library_id": library_id}
            )
            with get_database().transaction() as uow:
                MediaLibraryRepository(uow.session).update_sync_job(server_id, library_id, job_id, utcnow())
            return jsonify({"id": job_id, "workflow": "plex_then_tmdb"})
        except (TypeError, ValueError) as exc:
            return api_error(str(exc), "invalid_media_library_tmdb_refresh", 400)

    @servers_bp.post("/api/servers/<int:server_id>/media-library/recheck")
    @login_required
    def media_library_recheck(server_id: int):
        data = request.get_json(force=True) or {}
        library_id = int(data.get("library_id") or 0)
        rating_key = str(data.get("rating_key") or "").strip()
        if not library_id or not rating_key:
            return api_error("缺少媒体库或剧集标识", "invalid_media_recheck", 400)
        try:
            server = server_model_row(server_id, enabled_only=True)
            if server is None:
                return api_error("服务器不存在或已停用", "server_not_found", 404)
            resolved_rating_key = resolve_plex_rating_key(get_database_path(), server_id, library_id, rating_key)
            plex = _factory.PlexServer(server_config_from_row(server), tags=load_tags(), auto_login=True)
            metadata = plex.get_metadata(resolved_rating_key)
            if str(metadata.get("type") or "") != "show":
                return api_error("只支持重新检查剧集", "invalid_media_recheck_type", 400)
            sync_media_library_full(
                db_file=get_database_path(), server_id=server_id, library_id=library_id, rating_keys={resolved_rating_key}
            )
            with get_database().session() as db:
                repository = MediaLibraryRepository(db)
                library = repository.get(server_id, library_id)
                row = repository.item(server_id, library_id, resolved_rating_key)
                if row is None:
                    return api_error("重新检查后未找到真实剧集缓存", "media_item_not_found", 404)
                return jsonify(_cached_show_payload(repository, server_id, library_id, row, library))
        except Exception as exc:
            return api_error(str(exc), "media_recheck_failed", 400)

    @servers_bp.get("/api/servers/<int:server_id>/media-library/<int:library_id>/quarter-index")
    @login_required
    def media_library_quarter_index(server_id: int, library_id: int):
        with get_database().session() as db:
            repository = MediaLibraryRepository(db)
            library = repository.get(server_id, library_id)
            if library is None or int(library["plex_type"]) != 2:
                return api_error("电视剧库缓存不存在", "library_not_found", 404)
            shows = repository.items_by_type(server_id, library_id, "show")
            season_rows = repository.items_by_type(server_id, library_id, "season")
            episode_rows = repository.items_by_type(server_id, library_id, "episode")
            seasons_by_show: dict[str, dict[int, Any]] = {}
            episodes_by_season: dict[tuple[str, int], dict[int, Any]] = {}
            for row in season_rows:
                parent_key = str(row["parent_rating_key"])
                season_number = int(row["season_number"] or 0)
                seasons = seasons_by_show.setdefault(parent_key, {})
                seasons[season_number] = _prefer_cached_child(seasons.get(season_number), row)
            for row in episode_rows:
                key = (str(row["parent_rating_key"]), int(row["season_number"] or 0))
                indexed_episodes = episodes_by_season.setdefault(key, {})
                episode_number = int(row["episode_number"] or 0)
                indexed_episodes[episode_number] = _prefer_cached_child(indexed_episodes.get(episode_number), row)
            entries, live_items = [], []
            is_animation_library = _library_is_animation(
                library["animation_mode"], library["title"], library["plex_type"]
            )
            for show in shows:
                show_seasons = list(seasons_by_show.get(str(show["rating_key"]), {}).values())
                season_count = len({int(row["season_number"] or 0) for row in show_seasons})
                episode_count = sum(
                    len(episodes_by_season.get((str(show["rating_key"]), int(row["season_number"] or 0)), {}))
                    for row in show_seasons
                )
                summary = _cached_show_summary(show, library_id, library, season_count, episode_count)
                if is_animation_library:
                    for season_row in show_seasons:
                        season_number = int(season_row["season_number"] or 0)
                        episodes = list(
                            episodes_by_season.get((str(show["rating_key"]), season_number), {}).values()
                        )
                        dates = [
                            str(episode["release_date"] or "")[:10]
                            for episode in episodes
                            if episode["release_date"]
                        ]
                        first_date = min(dates) if dates else str(season_row["release_date"] or "")[:10]
                        added = [episode["added_at"] for episode in episodes if episode["added_at"]]
                        missing_count = sum(1 for episode in episodes if _cached_child_is_expected(episode))
                        entries.append(
                            {
                                **summary,
                                "season": season_number,
                                "bucket": "特别篇" if season_number == 0 else _season_bucket(first_date),
                                "release_date": first_date,
                                "first_episode_date": first_date,
                                "latest_added_at": max(added) if added else "",
                                "episode_count": len(episodes),
                                "missing_count": missing_count,
                                "is_special": season_number == 0,
                                "is_undated": not bool(first_date),
                            }
                        )
                else:
                    live_items.append(summary)
            groups: dict[str, list[dict[str, Any]]] = {}
            for entry in entries:
                groups.setdefault(entry["bucket"], []).append(entry)

            def group_key(label: str):
                # Normal quarters are always newest-first.  Special episodes
                # and undated seasons are kept as explicit trailing groups.
                if label == "特别篇":
                    return (1, 0, 0)
                if label == "未定档":
                    return (2, 0, 0)
                try:
                    return (0, -int(label[:4]), -int(label.split("Q", 1)[1].split()[0]))
                except (TypeError, ValueError, IndexError):
                    return (2, 0, 0)

            for group in groups.values():
                group.sort(
                    key=lambda item: (
                        item.get("first_episode_date") or "9999-99-99",
                        str(item.get("title") or "").casefold(),
                    )
                )
            result = [{"label": label, "items": groups[label]} for label in sorted(groups, key=group_key)]
        return jsonify(
            {"server_id": server_id, "library_id": library_id, "groups": result, "live_items": live_items}
        )

    @servers_bp.get("/api/servers/<int:server_id>/media-library/item")
    @login_required
    def media_library_item(server_id: int):
        try:
            library_id = int(request.args.get("library_id") or 0)
            rating_key = str(request.args.get("rating_key") or "").strip()
        except (TypeError, ValueError):
            return api_error("媒体参数无效", "invalid_media_item", 400)
        if not library_id or not rating_key:
            return api_error("缺少媒体库或条目标识", "invalid_media_item", 400)
        with get_database().session() as db:
            repository = MediaLibraryRepository(db)
            library = repository.get(server_id, library_id)
            row = repository.item(server_id, library_id, rating_key)
            if library is None or row is None:
                return api_error("媒体条目缓存不存在，请先同步媒体库", "media_item_not_found", 404)
            if row["plex_type"] == "show":
                return jsonify(_cached_show_payload(repository, server_id, library_id, row, library))
            raw = decode_payload(row["raw_json"])
            item = dict(row)
            item.pop("raw_json", None)
            item.update(
                {
                    "library_id": library_id,
                    "summary": raw.get("summary") or "",
                    "external_ids": raw.get("external_ids") or extract_external_ids(raw),
                    "collections": [],
                }
            )
            item["tmdb_id"] = (
                int(row["tmdb_id"])
                if row["tmdb_id"]
                else (int(item["external_ids"]["tmdb"]) if item["external_ids"].get("tmdb") else None)
            )
            item["imdb_id"] = row["imdb_id"] or item["external_ids"].get("imdb") or ""
            item["tvdb_id"] = row["tvdb_id"] or item["external_ids"].get("tvdb") or ""
            item["poster_url"] = raw.get("poster_url") or ""
        return jsonify(item)

    @servers_bp.get("/api/servers/<int:server_id>/media-library/item-by-tmdb")
    @login_required
    def media_library_item_by_tmdb(server_id: int):
        """Return one cached media item without reloading the whole library.

        Search-add jobs write only to the local cache.  The browser uses this
        endpoint after the job succeeds to patch the current result and the
        affected library in place.  Prefer a real Plex row when both a real
        and a system-maintained row exist, matching the normal merge rules.
        """
        try:
            library_id = int(request.args.get("library_id") or 0)
            tmdb_id = int(request.args.get("tmdb_id") or 0)
        except (TypeError, ValueError):
            return api_error("媒体参数无效", "invalid_media_item_by_tmdb", 400)
        media_type = str(request.args.get("media_type") or "").strip().lower()
        if not library_id or not tmdb_id or media_type not in {"movie", "tv"}:
            return api_error("缺少有效的媒体库、媒体类型或 TMDB ID", "invalid_media_item_by_tmdb", 400)
        with get_database().session() as db:
            repository = MediaLibraryRepository(db)
            library = repository.get(server_id, library_id)
            if library is None:
                return api_error("媒体库不存在", "library_not_found", 404)
            expected_type = 1 if media_type == "movie" else 2
            if int(library["plex_type"] or 0) != expected_type:
                return api_error("媒体库类型与作品类型不匹配", "invalid_target_library", 400)
            row = repository.item_by_tmdb(server_id, library_id, media_type, tmdb_id)
            if row is None:
                return api_error("媒体条目尚未写入缓存", "media_item_not_found", 404)
            if media_type == "tv":
                item = _cached_show_payload(repository, server_id, library_id, row, library)
                quarter_entries = []
                for season in item.get("seasons") or []:
                    dates = [
                        episode.get("air_date")
                        for episode in season.get("episodes") or []
                        if episode.get("air_date")
                    ]
                    first_date = min(dates) if dates else season.get("release_date") or ""
                    added = [
                        episode.get("added_at")
                        for episode in season.get("episodes") or []
                        if episode.get("added_at")
                    ]
                    quarter_entries.append(
                        {
                            "label": season.get("bucket") or "未定档",
                            "season": int(season.get("season") or 0),
                            "first_episode_date": first_date,
                            "latest_added_at": max(added) if added else "",
                            "episode_count": len(season.get("episodes") or []),
                            "missing_count": sum(
                                1 for episode in season.get("episodes") or [] if episode.get("missing")
                            ),
                            "is_special": int(season.get("season") or 0) == 0,
                            "is_undated": not bool(first_date),
                        }
                    )
                item["quarter_entries"] = quarter_entries
            else:
                raw = decode_payload(row["raw_json"])
                item = dict(row)
                item.pop("raw_json", None)
                item.update(
                    {
                        "library_id": library_id,
                        "summary": raw.get("summary") or "",
                        "external_ids": raw.get("external_ids") or extract_external_ids(raw),
                        "collections": [],
                    }
                )
                item["tmdb_id"] = int(row["tmdb_id"]) if row["tmdb_id"] else tmdb_id
                item["imdb_id"] = row["imdb_id"] or item["external_ids"].get("imdb") or ""
                item["tvdb_id"] = row["tvdb_id"] or item["external_ids"].get("tvdb") or ""
                item["poster_url"] = raw.get("poster_url") or ""
            item["media_type"] = media_type
            item["system_managed"] = bool(row["system_managed"])
            item["plex_present"] = not bool(row["system_managed"])
            item["library_title"] = str(library["title"] or "")
            item["library_item_count"] = repository.item_counts(server_id, library_id)[0]
        return jsonify(item)

