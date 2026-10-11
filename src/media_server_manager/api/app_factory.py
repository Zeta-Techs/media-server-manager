# ruff: noqa: F401

from __future__ import annotations

import hmac
import json
import posixpath
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from urllib.parse import urlparse

from flask import Blueprint, Flask, Response, jsonify, redirect, request, session, url_for
from sqlalchemy import select
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge

from media_server_manager.api.dependencies import get_database, get_database_path
from media_server_manager.api.routes.auth import create_auth_blueprint
from media_server_manager.api.routes.automation import create_automation_blueprint
from media_server_manager.api.routes.catalog import register_routes as register_catalog_routes
from media_server_manager.api.routes.diagnostics import create_diagnostics_blueprint, overview_payload
from media_server_manager.api.routes.jobs import create_jobs_blueprint
from media_server_manager.api.routes.media_libraries import register_routes as register_media_library_routes
from media_server_manager.api.routes.servers import create_servers_blueprint
from media_server_manager.api.routes.webhooks import register_routes as register_webhook_routes
from media_server_manager.application.auth.service import AuthService
from media_server_manager.application.catalog_sync.operations import (
    resolve_plex_rating_key,
    sync_media_library_full,
)
from media_server_manager.application.job_orchestration.queue import JobQueue
from media_server_manager.application.media_operations.bulk_media import (
    _summary,
    _title_key,
    decode_json,
    parse_line,
)
from media_server_manager.application.server_management.service import ServerManagementService
from media_server_manager.config import settings
from media_server_manager.core import extract_external_ids
from media_server_manager.infrastructure.db.repositories.auth import AuthRepository
from media_server_manager.infrastructure.db.repositories.automation import AutomationRepository
from media_server_manager.infrastructure.db.repositories.catalog import CatalogRepository
from media_server_manager.infrastructure.db.repositories.media_library import MediaLibraryRepository
from media_server_manager.infrastructure.db.repositories.rechecks import RecheckRepository
from media_server_manager.infrastructure.db.runtime import (
    DB_FILE,
    connect,  # noqa: F401 - legacy extension hook; routes use SQLAlchemy sessions
    decode_payload,
    get_setting,
    init_db,  # noqa: F401 - retained as a public test/extension hook
    load_tags,
    server_config_from_row,
    utcnow,
)
from media_server_manager.infrastructure.db.session import Database
from media_server_manager.infrastructure.integrations.plex.client import PlexServer
from media_server_manager.infrastructure.integrations.tmdb import TMDBClient
from media_server_manager.infrastructure.security.passwords import (
    load_session_secret,
    verify_csrf,
)

# Compatibility hook for extensions and the legacy test suite.  Production
# configuration comes from Settings; only an explicitly monkeypatched legacy
# value overrides it for one process.
_DEFAULT_DATABASE_PATH = settings.database_path


def _database_path() -> Path:
    return settings.database_path if DB_FILE == _DEFAULT_DATABASE_PATH else DB_FILE


def _air_date(metadata: dict[str, Any]) -> str:
    return str(metadata.get("originallyAvailableAt") or metadata.get("originally_available_at") or "")[:10]


def _season_bucket(date_value: str) -> str:
    if not date_value or len(date_value) < 7:
        return "未定档"
    try:
        year, month = int(date_value[:4]), int(date_value[5:7])
    except (TypeError, ValueError):
        return "未定档"
    quarter = ((month - 1) // 3) + 1
    label = {1: "1月番", 2: "4月番", 3: "7月番", 4: "10月番"}[quarter]
    return f"{year} Q{quarter} / {label}"


def _media_image_path(metadata: dict[str, Any]) -> str:
    return str(metadata.get("thumb") or metadata.get("parentThumb") or "")


def _media_fields(metadata: dict[str, Any]) -> dict[str, Any]:
    try:
        duration = int(metadata.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    return {
        "rating_key": str(metadata.get("ratingKey") or ""),
        "title": str(metadata.get("title") or ""),
        "original_title": str(metadata.get("originalTitle") or ""),
        "year": metadata.get("year"),
        "added_at": metadata.get("addedAt"),
        "release_date": _air_date(metadata),
        "rating": metadata.get("rating"),
        "audience_rating": metadata.get("audienceRating"),
        "duration": duration,
        "content_rating": str(metadata.get("contentRating") or ""),
        "genre": ", ".join(str(g.get("tag")) for g in (metadata.get("Genre") or []) if g.get("tag")),
        "summary": str(metadata.get("summary") or ""),
        "thumb": _media_image_path(metadata),
        "art": str(metadata.get("art") or ""),
        "type": str(metadata.get("type") or ""),
        "external_ids": extract_external_ids(metadata),
    }


def _episode_payload(episode: dict[str, Any]) -> dict[str, Any]:
    try:
        season = int(episode.get("parentIndex") or 0)
    except (TypeError, ValueError):
        season = 0
    try:
        number = int(episode.get("index") or 0)
    except (TypeError, ValueError):
        number = 0
    try:
        duration = int(episode.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    return {
        "rating_key": str(episode.get("ratingKey") or ""),
        "season": season,
        "episode": number,
        "title": str(episode.get("title") or ""),
        "air_date": _air_date(episode),
        "duration": duration,
        "thumb": _media_image_path(episode),
    }


def _show_payload(plex: PlexServer, show: dict[str, Any], library_id: int) -> dict[str, Any]:
    show_key = str(show.get("ratingKey") or "")
    seasons: list[dict[str, Any]] = []
    episodes_by_season: dict[int, list[dict[str, Any]]] = {}
    try:
        for episode in plex.list_show_episodes(show_key):
            payload = _episode_payload(episode)
            episodes_by_season.setdefault(payload["season"], []).append(payload)
    except Exception:
        episodes_by_season = {}
    try:
        season_rows = plex.get_children(show_key)
    except Exception:
        season_rows = []

    def season_number_for(row: dict[str, Any]) -> int:
        try:
            value = row.get("index")
            if value is None:
                value = row.get("parentIndex")
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    known_seasons = {season_number_for(row) for row in season_rows}
    for season_number in sorted(set(known_seasons) | set(episodes_by_season)):
        matching_seasons = [row for row in season_rows if season_number_for(row) == season_number]
        season = matching_seasons[0] if matching_seasons else {}
        for candidate in matching_seasons[1:]:
            if (
                str(season.get("title") or "").strip().casefold() == "specials"
                and str(candidate.get("title") or "").strip().casefold() != "specials"
                and season_number > 0
            ):
                season = candidate
        episodes = sorted(episodes_by_season.get(season_number, []), key=lambda item: item["episode"])
        dates = [item["air_date"] for item in episodes if item["air_date"]]
        release_date = min(dates) if dates else _air_date(season)
        seasons.append(
            {
                "season": season_number,
                "title": str(season.get("title") or f"第 {season_number} 季"),
                "release_date": release_date,
                "bucket": "特别篇" if season_number == 0 else _season_bucket(release_date),
                "episodes": episodes,
            }
        )
    seasons.sort(key=lambda item: item["season"])
    return {
        **_media_fields(show),
        "library_id": int(library_id),
        "show_kind": "动画"
        if any(
            any(marker in str(item.get("tag") or "").lower() for marker in ("动画", "animation", "anime"))
            for item in (show.get("Genre") or [])
        )
        else "真人",
        "seasons": seasons,
        "season_count": len(seasons),
        "episode_count": sum(len(season["episodes"]) for season in seasons),
    }


def _show_summary_payload(show: dict[str, Any], library_id: int) -> dict[str, Any]:
    """Return the inexpensive part of a show card before its episodes load."""
    return {
        **_media_fields(show),
        "library_id": int(library_id),
        "show_kind": "动画"
        if any(
            any(marker in str(item.get("tag") or "").lower() for marker in ("动画", "animation", "anime"))
            for item in (show.get("Genre") or [])
        )
        else "真人",
        "seasons": [],
        "season_count": int(show.get("childCount") or 0),
        "episode_count": int(show.get("leafCount") or 0),
    }


def _sort_value(item: dict[str, Any], field: str) -> tuple[int, float | str]:
    aliases = {
        "name": "title",
        "original_title": "original_title",
        "year": "year",
        "added_at": "added_at",
        "release_date": "release_date",
        "rating": "rating",
        "audience_rating": "audience_rating",
        "duration": "duration",
        "season_count": "season_count",
        "episode_count": "episode_count",
        "content_rating": "content_rating",
        "genre": "genre",
    }
    value = item.get(aliases.get(field, "title"))
    if value is None or value == "":
        return (1, "")
    if field in {
        "year",
        "added_at",
        "rating",
        "audience_rating",
        "duration",
        "season_count",
        "episode_count",
    }:
        try:
            return (0, float(value))
        except (TypeError, ValueError):
            return (1, 0)
    return (0, str(value).casefold())


def _sort_items(items: list[dict[str, Any]], field: str, direction: str) -> list[dict[str, Any]]:
    reverse = direction == "desc"
    return sorted(
        items, key=lambda item: (_sort_value(item, field), str(item.get("rating_key") or "")), reverse=reverse
    )


def _library_name_is_animation(title: str, plex_type: int) -> bool:
    return int(plex_type) == 2 and any(
        keyword in str(title or "").casefold() for keyword in ("anime", "动画", "番")
    )


def _library_is_animation(mode: str, title: str, plex_type: int) -> bool:
    if int(plex_type) != 2:
        return False
    if mode == "animation":
        return True
    if mode == "normal":
        return False
    return _library_name_is_animation(title, plex_type)


def _cached_show_kind(row: Any, library: Any) -> str:
    """Use TMDB classification for system rows and Plex labels for real rows."""
    if row is not None and int(row["system_managed"] or 0) and row["animation_kind"]:
        return str(row["animation_kind"])
    genre = str(row["genre"] or "").casefold() if row is not None else ""
    if genre:
        return "动画" if any(marker in genre for marker in ("动画", "animation", "anime")) else "真人"
    return (
        "动画"
        if _library_is_animation(library["animation_mode"], library["title"], library["plex_type"])
        else "真人"
    )


def _quarter_entries(show: dict[str, Any], library_id: int, plex: PlexServer) -> list[dict[str, Any]]:
    full = _show_payload(plex, show, library_id)
    entries = []
    for season in full["seasons"]:
        dates = [episode["air_date"] for episode in season["episodes"] if episode.get("air_date")]
        first_date = min(dates) if dates else season.get("release_date") or ""
        entries.append(
            {
                **_show_summary_payload(show, library_id),
                "season": season["season"],
                "bucket": season["bucket"],
                "release_date": season.get("release_date") or "",
                "first_episode_date": first_date,
                "episode_count": len(season["episodes"]),
                "is_special": season["season"] == 0,
                "is_undated": not bool(first_date),
            }
        )
    return entries


def _cached_show_payload(
    repository: MediaLibraryRepository, server_id: int, library_id: int, show_row: Any, library: Any
) -> dict[str, Any]:
    show = dict(show_row)
    seasons: list[dict[str, Any]] = []
    season_rows = repository.children_for_parent(server_id, library_id, show["rating_key"], "season")

    # A Plex library can expose an anomalous ``Specials`` row with index 0,
    # while its cached season number has been normalised to 1.  If we render
    # rows independently, that row duplicates the real Season 1 card.  Group
    # by the normalised season number and choose the most useful source row:
    # real Plex seasons beat expected TMDB placeholders, and a named season
    # beats an anomalous ``Specials`` label for season numbers greater than 0.
    seasons_by_number: dict[int, dict[str, Any]] = {}
    for season_row in season_rows:
        season = dict(season_row)
        season_number = int(season.get("season_number") or 0)
        existing = seasons_by_number.get(season_number)
        if existing is None:
            seasons_by_number[season_number] = season
            continue
        seasons_by_number[season_number] = _prefer_cached_child(existing, season)

    for season_number in sorted(seasons_by_number):
        season = seasons_by_number[season_number]
        episodes_by_number: dict[int, dict[str, Any]] = {}
        episode_rows = [
            row for row in repository.children_for_parent(server_id, library_id, show["rating_key"], "episode")
            if int(row.get("season_number") or 0) == int(season["season_number"] or 0)
        ]
        for episode_row in episode_rows:
            episode = dict(episode_row)
            expected = _cached_child_is_expected(episode)
            payload = {
                "rating_key": episode["rating_key"],
                "season": int(episode["season_number"] or 0),
                "episode": int(episode["episode_number"] or 0),
                "title": episode["title"],
                "air_date": episode["release_date"],
                "duration": episode["duration"] or 0,
                "thumb": episode["thumb"],
                "added_at": episode["added_at"],
                "expected": expected,
                "missing": expected,
            }
            episode_number = payload["episode"]
            existing = episodes_by_number.get(episode_number)
            # A real Plex row supersedes a TMDB expected placeholder with the
            # same season/episode number. Keep the placeholder only while the
            # episode is genuinely absent from Plex.
            if existing is None or (existing.get("missing") and not expected):
                episodes_by_number[episode_number] = payload
        episodes = [episodes_by_number[number] for number in sorted(episodes_by_number)]
        dates = [item["air_date"] for item in episodes if item.get("air_date")]
        release_date = min(dates) if dates else season["release_date"] or ""
        seasons.append(
            {
                "season": season_number,
                "title": season["title"] or f"第 {season_number} 季",
                "release_date": release_date,
                "bucket": "特别篇" if season_number == 0 else _season_bucket(release_date),
                "episodes": episodes,
            }
        )
    raw = decode_payload(show.get("raw_json"))
    item = {
        key: show.get(key)
        for key in (
            "rating_key",
            "title",
            "original_title",
            "year",
            "added_at",
            "release_date",
            "rating",
            "audience_rating",
            "duration",
            "content_rating",
            "genre",
            "thumb",
            "art",
        )
    }
    external_ids = raw.get("external_ids") or extract_external_ids(raw)
    external_ids = {
        **external_ids,
        "tmdb": str(show.get("tmdb_id") or external_ids.get("tmdb") or ""),
        "imdb": str(show.get("imdb_id") or external_ids.get("imdb") or ""),
        "tvdb": str(show.get("tvdb_id") or external_ids.get("tvdb") or ""),
    }
    item.update(
        {
            "library_id": library_id,
            "type": "show",
            "summary": raw.get("summary") or "",
            "external_ids": external_ids,
            "tmdb_id": int(show["tmdb_id"]) if show.get("tmdb_id") else None,
            "imdb_id": show.get("imdb_id") or external_ids.get("imdb") or "",
            "tvdb_id": show.get("tvdb_id") or external_ids.get("tvdb") or "",
            "show_kind": _cached_show_kind(show, library),
            "seasons": seasons,
            "season_count": len(seasons),
            "episode_count": sum(len(item["episodes"]) for item in seasons),
            "poster_url": raw.get("poster_url") or "",
        }
    )
    return item


def _cached_show_summary(
    show_row: Any, library_id: int, library: Any, season_count: int = 0, episode_count: int = 0
) -> dict[str, Any]:
    """Build a show card without loading its season and episode payloads."""
    show = dict(show_row)
    raw = decode_payload(show.get("raw_json"))
    external_ids = raw.get("external_ids") or extract_external_ids(raw)
    external_ids = {
        **external_ids,
        "tmdb": str(show.get("tmdb_id") or external_ids.get("tmdb") or ""),
        "imdb": str(show.get("imdb_id") or external_ids.get("imdb") or ""),
        "tvdb": str(show.get("tvdb_id") or external_ids.get("tvdb") or ""),
    }
    return {
        "rating_key": str(show.get("rating_key") or ""),
        "title": str(show.get("title") or ""),
        "original_title": str(show.get("original_title") or ""),
        "year": show.get("year"),
        "added_at": show.get("added_at"),
        "release_date": show.get("release_date") or "",
        "rating": show.get("rating"),
        "audience_rating": show.get("audience_rating"),
        "duration": show.get("duration") or 0,
        "content_rating": str(show.get("content_rating") or ""),
        "genre": str(show.get("genre") or ""),
        "thumb": show.get("thumb") or "",
        "art": show.get("art") or "",
        "type": "show",
        "library_id": int(library_id),
        "show_kind": _cached_show_kind(show, library),
        "seasons": [],
        "season_count": int(season_count),
        "episode_count": int(episode_count),
        "external_ids": external_ids,
        "tmdb_id": int(show["tmdb_id"]) if show.get("tmdb_id") else None,
        "imdb_id": show.get("imdb_id") or external_ids.get("imdb") or "",
        "tvdb_id": show.get("tvdb_id") or external_ids.get("tvdb") or "",
        "summary": raw.get("summary") or "",
        "poster_url": raw.get("poster_url") or "",
    }


def _cached_child_is_expected(row: Any) -> bool:
    """Return whether a cached season or episode is a synthetic expectation."""
    return (
        str(row["rating_key"] or "").startswith("tmdb")
        or decode_payload(row["raw_json"]).get("expected") is True
    )


def _prefer_cached_child(existing: Any, candidate: Any) -> Any:
    """Prefer a real Plex child over a synthetic TMDB placeholder."""
    if existing is None:
        return candidate
    existing_expected = _cached_child_is_expected(existing)
    candidate_expected = _cached_child_is_expected(candidate)
    existing_specials = str(existing["title"] or "").strip().casefold() == "specials"
    candidate_specials = str(candidate["title"] or "").strip().casefold() == "specials"
    if candidate_expected != existing_expected:
        return existing if candidate_expected else candidate
    if existing_specials and not candidate_specials and int(candidate["season_number"] or 0) > 0:
        return candidate
    return existing


def create_app() -> Flask:
    # Fail fast in production before opening the legacy compatibility queue.
    settings.ensure_directories()
    app = Flask(__name__, template_folder="templates", static_folder="static")
    auth_bp = create_auth_blueprint()
    servers_bp = create_servers_blueprint()
    jobs_bp = create_jobs_blueprint()
    automation_bp = create_automation_blueprint()
    diagnostics_bp = create_diagnostics_blueprint()
    catalog_bp = Blueprint("catalog", __name__)
    webhooks_bp = Blueprint("webhooks", __name__)
    app.config.update(
        SECRET_KEY=load_session_secret(_database_path()),
        MAX_CONTENT_LENGTH=5 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=settings.cookie_secure,
    )
    database_path = _database_path()
    database = Database.for_path(database_path)
    manager = JobQueue(database_path, database=database)
    app.extensions["database"] = database
    app.extensions["task_manager"] = manager
    app.extensions["db_file"] = database_path

    @app.teardown_appcontext
    def _teardown(_: Any) -> None:
        pass

    def has_user() -> bool:
        with get_database().session() as db:
            return AuthService(db).has_users()

    def current_user_exists() -> bool:
        user_id = session.get("user_id")
        if not user_id:
            return False
        with get_database().session() as db:
            return AuthService(db).current_user_exists(int(user_id))

    def api_error(message: str, code: str, status: int):
        return jsonify({"error": message, "code": code}), status

    def client_ip() -> str:
        return request.remote_addr or "unknown"

    def validate_address(value: str) -> str:
        address = (value or "").strip().rstrip("/")
        parsed = urlparse(address)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("服务器地址必须是有效的 http 或 https URL")
        return address

    def validate_media_path(value: str) -> str:
        path = (value or "").strip()
        parsed = urlparse(path)
        normalized = posixpath.normpath(parsed.path)
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.params
            or parsed.query
            or parsed.fragment
            or not normalized.startswith("/library/metadata/")
            or normalized != parsed.path
            or any(part in {".", ".."} for part in normalized.split("/"))
        ):
            raise ValueError("媒体图片路径无效")
        return normalized

    def serialize_server(row: Any) -> dict[str, Any]:
        data = dict(row)
        data.pop("token", None)
        secret = data.pop("webhook_secret", "")
        data["token_configured"] = True
        data["webhook_url"] = url_for(
            "webhooks.webhook", server_id=data["id"], secret=secret, _external=True
        )
        return data

    def serialize_server_model(server: Any) -> dict[str, Any]:
        return serialize_server(
            {
                "id": server.id,
                "name": server.name,
                "address": server.address,
                "token": server.token,
                "skip_libraries": server.skip_libraries,
                "pinyin_mode": server.pinyin_mode,
                "auth_source": server.auth_source,
                "webhook_secret": server.webhook_secret,
                "enabled": server.enabled,
                "created_at": server.created_at,
                "updated_at": server.updated_at,
            }
        )

    def server_model_row(server_id: int, enabled_only: bool = False) -> dict[str, Any] | None:
        with get_database().session() as db:
            server = ServerManagementService(db).get_server(server_id)
            if server is None or (enabled_only and not server.enabled):
                return None
            return {
                "id": server.id,
                "name": server.name,
                "address": server.address,
                "token": server.token,
                "skip_libraries": server.skip_libraries,
                "pinyin_mode": server.pinyin_mode,
                "auth_source": server.auth_source,
                "webhook_secret": server.webhook_secret,
                "enabled": server.enabled,
            }

    @app.before_request
    def enforce_csrf():
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return None
        if not request.path.startswith("/api/"):
            return None
        expected = session.get("csrf_token", "")
        supplied = request.headers.get("X-CSRF-Token", "")
        if not verify_csrf(expected, supplied):
            return api_error("CSRF 校验失败，请刷新页面后重试", "csrf_failed", 403)
        return None

    @app.errorhandler(BadRequest)
    def handle_bad_request(_: BadRequest):
        return api_error("请求格式无效", "bad_request", 400)

    @app.errorhandler(RequestEntityTooLarge)
    def handle_too_large(_: RequestEntityTooLarge):
        return api_error("请求内容超过 5 MiB 限制", "request_too_large", 413)

    @app.errorhandler(ValueError)
    def handle_value_error(exc: ValueError):
        if request.path.startswith("/api/"):
            return api_error(str(exc), "invalid_request", 400)
        raise exc

    def login_required(view: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(view)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            if not has_user() and request.endpoint not in {"auth.index", "auth.api_setup", "static"}:
                if request.path.startswith("/api/"):
                    return api_error("应用尚未初始化", "not_initialized", 403)
                return redirect(url_for("auth.index"))
            if not current_user_exists():
                session.pop("user_id", None)
                if request.path.startswith("/api/"):
                    return api_error("请先登录", "unauthorized", 401)
                return redirect(url_for("auth.index"))
            return view(*args, **kwargs)

        return wrapped

    app.extensions["route_context"] = SimpleNamespace(
        login_required=login_required,
        validate_address=validate_address,
        has_user=has_user,
        current_user_exists=lambda: current_user_exists(),
        server_service=ServerManagementService,
        server_model_row=server_model_row,
        server_config_from_row=server_config_from_row,
        load_tags=load_tags,
        manager=lambda: manager,
    )

    register_media_library_routes(servers_bp, locals())
    register_catalog_routes(catalog_bp, locals())
    register_webhook_routes(webhooks_bp, locals())

    @app.get("/healthz")
    def healthz():
        try:
            with get_database().session() as db:
                db.scalar(select(1))
            return jsonify({"ok": True, "worker": manager.worker_status()})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc), "code": "database_unavailable"}), 503

    @app.get("/api/overview")
    @login_required
    def api_overview():
        with get_database().session() as db:
            payload = overview_payload(db, get_database_path())
        payload["worker"] = manager.worker_status()
        return jsonify(payload)

    @app.get("/api/overview/media")
    @login_required
    def api_overview_media():
        try:
            requested_limit = int(request.args.get("limit", "12"))
        except ValueError:
            requested_limit = 12
        limit = max(1, min(requested_limit, 24))
        with get_database().session() as db:
            rows = [
                {
                    "id": server.id,
                    "name": server.name,
                    "address": server.address,
                    "token": server.token,
                    "skip_libraries": server.skip_libraries,
                    "pinyin_mode": server.pinyin_mode,
                    "auth_source": server.auth_source,
                    "webhook_secret": server.webhook_secret,
                    "enabled": server.enabled,
                }
                for server in ServerManagementService(db).enabled_servers()
            ]
        items: list[dict[str, Any]] = []
        warnings: list[str] = []
        per_server = max(4, min(limit, 12))
        for row in rows:
            try:
                plex = PlexServer(server_config_from_row(row), tags=load_tags(), auto_login=False)
                for item in plex.list_recent_media(per_server):
                    item["server_id"] = int(row["id"])
                    item["server_name"] = row["name"]
                    item["image_url"] = url_for(
                        "servers.media_image",
                        server_id=int(row["id"]),
                        path=item.pop("thumb"),
                    )
                    items.append(item)
            except Exception as exc:
                warnings.append(f"{row['name']}：{exc}")
        items.sort(key=lambda item: int(item.get("added_at") or 0), reverse=True)
        return jsonify({"items": items[:limit], "warnings": warnings})

    app.register_blueprint(auth_bp)
    app.register_blueprint(servers_bp)
    app.register_blueprint(jobs_bp)
    app.register_blueprint(automation_bp)
    app.register_blueprint(diagnostics_bp)
    app.register_blueprint(catalog_bp)
    app.register_blueprint(webhooks_bp)
    return app
