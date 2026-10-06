from __future__ import annotations

import hmac
import json
import os
import posixpath
import secrets
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Callable
from urllib.parse import urlparse

from flask import Blueprint, Flask, Response, jsonify, redirect, render_template, request, session, url_for
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge

from clp.core import PlexServer, extract_external_ids, join_skip_libraries

from .db import (
    DB_FILE,
    DEFAULT_USERNAME,
    connect,
    decode_payload,
    get_setting,
    init_db,
    load_tags,
    new_secret,
    rows_to_dicts,
    save_tags,
    server_config_from_row,
    set_setting,
    utcnow,
)
from .plex_auth import check_pin, create_pin
from .scheduling import describe_schedule, next_run_at, validate_schedule
from .security import (
    hash_password,
    load_session_secret,
    new_csrf_token,
    verify_csrf,
    verify_password,
)
from .services import TERMINAL_JOB_STATUSES, JobQueue
from .catalog import _remove_expected_episode_placeholder, _upsert_media_library_item


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
    seasons = []
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
        seasons.append({
            "season": season_number,
            "title": str(season.get("title") or f"第 {season_number} 季"),
            "release_date": release_date,
            "bucket": "特别篇" if season_number == 0 else _season_bucket(release_date),
            "episodes": episodes,
        })
    seasons.sort(key=lambda item: item["season"])
    return {
        **_media_fields(show),
        "library_id": int(library_id),
        "show_kind": "动画" if any(any(marker in str(item.get("tag") or "").lower() for marker in ("动画", "animation", "anime")) for item in (show.get("Genre") or [])) else "真人",
        "seasons": seasons,
        "season_count": len(seasons),
        "episode_count": sum(len(season["episodes"]) for season in seasons),
    }


def _show_summary_payload(show: dict[str, Any], library_id: int) -> dict[str, Any]:
    """Return the inexpensive part of a show card before its episodes load."""
    return {
        **_media_fields(show),
        "library_id": int(library_id),
        "show_kind": "动画" if any(any(marker in str(item.get("tag") or "").lower() for marker in ("动画", "animation", "anime")) for item in (show.get("Genre") or [])) else "真人",
        "seasons": [],
        "season_count": int(show.get("childCount") or 0),
        "episode_count": int(show.get("leafCount") or 0),
    }


def _sort_value(item: dict[str, Any], field: str) -> Any:
    aliases = {
        "name": "title", "original_title": "original_title", "year": "year", "added_at": "added_at",
        "release_date": "release_date", "rating": "rating", "audience_rating": "audience_rating",
        "duration": "duration", "season_count": "season_count", "episode_count": "episode_count",
        "content_rating": "content_rating", "genre": "genre",
    }
    value = item.get(aliases.get(field, "title"))
    if value in (None, ""):
        return (1, "")
    if field in {"year", "added_at", "rating", "audience_rating", "duration", "season_count", "episode_count"}:
        try:
            return (0, float(value))
        except (TypeError, ValueError):
            return (1, 0)
    return (0, str(value).casefold())


def _sort_items(items: list[dict[str, Any]], field: str, direction: str) -> list[dict[str, Any]]:
    reverse = direction == "desc"
    return sorted(items, key=lambda item: (_sort_value(item, field), str(item.get("rating_key") or "")), reverse=reverse)


def _library_name_is_animation(title: str, plex_type: int) -> bool:
    return int(plex_type) == 2 and any(keyword in str(title or "").casefold() for keyword in ("anime", "动画", "番"))


def _library_is_animation(mode: str, title: str, plex_type: int) -> bool:
    if int(plex_type) != 2:
        return False
    if mode == "animation":
        return True
    if mode == "normal":
        return False
    return _library_name_is_animation(title, plex_type)


def _quarter_entries(show: dict[str, Any], library_id: int, plex: PlexServer) -> list[dict[str, Any]]:
    full = _show_payload(plex, show, library_id)
    entries = []
    for season in full["seasons"]:
        dates = [episode["air_date"] for episode in season["episodes"] if episode.get("air_date")]
        first_date = min(dates) if dates else season.get("release_date") or ""
        entries.append({
            **_show_summary_payload(show, library_id),
            "season": season["season"],
            "bucket": season["bucket"],
            "release_date": season.get("release_date") or "",
            "first_episode_date": first_date,
            "episode_count": len(season["episodes"]),
            "is_special": season["season"] == 0,
            "is_undated": not bool(first_date),
        })
    return entries


def _cached_show_payload(db: Any, server_id: int, library_id: int, show_row: Any, library: Any) -> dict[str, Any]:
    show = dict(show_row)
    seasons = []
    season_rows = db.execute("SELECT * FROM media_library_items WHERE server_id = ? AND library_id = ? AND parent_rating_key = ? AND plex_type = 'season' ORDER BY season_number, id", (server_id, library_id, show["rating_key"])).fetchall()

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
        existing_raw = decode_payload(existing.get("raw_json"))
        season_raw = decode_payload(season.get("raw_json"))
        existing_expected = existing_raw.get("expected") is True or str(existing.get("rating_key") or "").startswith("tmdb")
        season_expected = season_raw.get("expected") is True or str(season.get("rating_key") or "").startswith("tmdb")
        existing_specials = str(existing.get("title") or "").strip().casefold() == "specials"
        season_specials = str(season.get("title") or "").strip().casefold() == "specials"
        if (
            (existing_expected and not season_expected)
            or (existing_specials and not season_specials and season_number > 0)
        ):
            seasons_by_number[season_number] = season

    for season_number in sorted(seasons_by_number):
        season = seasons_by_number[season_number]
        episodes_by_number = {}
        episode_rows = db.execute("SELECT * FROM media_library_items WHERE server_id = ? AND library_id = ? AND parent_rating_key = ? AND plex_type = 'episode' AND season_number = ? ORDER BY episode_number", (server_id, library_id, show["rating_key"], season["season_number"])).fetchall()
        for episode_row in episode_rows:
            episode = dict(episode_row)
            expected = decode_payload(episode.get("raw_json")).get("expected") is True or str(episode.get("rating_key") or "").startswith("tmdb:")
            payload = {"rating_key": episode["rating_key"], "season": int(episode["season_number"] or 0), "episode": int(episode["episode_number"] or 0),
                       "title": episode["title"], "air_date": episode["release_date"], "duration": episode["duration"] or 0,
                       "thumb": episode["thumb"], "added_at": episode["added_at"], "expected": expected, "missing": expected}
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
        seasons.append({"season": season_number, "title": season["title"] or f"第 {season_number} 季",
                        "release_date": release_date, "bucket": "特别篇" if season_number == 0 else _season_bucket(release_date),
                        "episodes": episodes})
    raw = decode_payload(show.get("raw_json"))
    item = {key: show.get(key) for key in ("rating_key", "title", "original_title", "year", "added_at", "release_date", "rating", "audience_rating", "duration", "content_rating", "genre", "thumb", "art")}
    item.update({"library_id": library_id, "type": "show", "summary": raw.get("summary") or "", "external_ids": raw.get("external_ids") or extract_external_ids(raw),
                 "show_kind": "动画" if _library_is_animation(library["animation_mode"], library["title"], library["plex_type"]) else "真人", "seasons": seasons,
                 "season_count": len(seasons), "episode_count": sum(len(item["episodes"]) for item in seasons)})
    return item


def create_app() -> Flask:
    app = Flask(__name__)
    auth_bp = Blueprint("auth", __name__)
    servers_bp = Blueprint("servers", __name__)
    jobs_bp = Blueprint("jobs", __name__)
    automation_bp = Blueprint("automation", __name__)
    tools_bp = Blueprint("tools", __name__)
    app.config.update(
        SECRET_KEY=load_session_secret(DB_FILE.parent / "session_secret"),
        MAX_CONTENT_LENGTH=5 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("CLP_COOKIE_SECURE", "0") == "1",
    )
    init_db()
    manager = JobQueue(DB_FILE)
    app.extensions["task_manager"] = manager

    @app.teardown_appcontext
    def _teardown(_: Any) -> None:
        pass

    def has_user() -> bool:
        with connect() as db:
            return db.execute("SELECT COUNT(*) AS count FROM users").fetchone()["count"] > 0

    def current_user_exists() -> bool:
        user_id = session.get("user_id")
        if not user_id:
            return False
        with connect() as db:
            return db.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is not None

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
            "automation.webhook", server_id=data["id"], secret=secret, _external=True
        )
        return data

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

    @auth_bp.route("/")
    def index():
        return render_template("index.html", initialized=has_user())

    @auth_bp.route("/media-library/detail")
    def media_library_detail_page():
        return render_template("index.html", initialized=has_user())

    @auth_bp.post("/api/setup")
    def api_setup():
        data = request.get_json(force=True)
        username = (data.get("username") or "admin").strip()
        password = data.get("password") or ""
        if not username or len(username) > 64:
            return api_error("用户名不能为空且不能超过 64 个字符", "invalid_username", 400)
        if len(password) < 10 or len(password) > 128:
            return api_error("密码长度需要在 10 到 128 位之间", "invalid_password", 400)
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT COUNT(*) AS count FROM users").fetchone()["count"]:
                db.rollback()
                return api_error("应用已经初始化", "already_initialized", 409)
            now = utcnow()
            cur = db.execute(
                "INSERT INTO users (username, password_hash, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (username, hash_password(password), now, now),
            )
            db.commit()
        session.clear()
        session["user_id"] = int(cur.lastrowid or 0)
        session["csrf_token"] = new_csrf_token()
        return jsonify({"ok": True, "csrf_token": session["csrf_token"]})

    @auth_bp.post("/api/auth/login")
    def api_login():
        data = request.get_json(force=True)
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        with connect() as db:
            cutoff = (
                datetime.now(timezone.utc) - timedelta(minutes=10)
            ).isoformat(timespec="seconds").replace("+00:00", "Z")
            db.execute("DELETE FROM auth_attempts WHERE created_at < ?", (cutoff,))
            failures = db.execute(
                "SELECT COUNT(*) AS count FROM auth_attempts WHERE username = ? AND ip_address = ?",
                (username, client_ip()),
            ).fetchone()["count"]
            if failures >= 5:
                db.commit()
                return api_error("登录失败次数过多，请 10 分钟后重试", "login_rate_limited", 429)
            user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            if user is None or not verify_password(password, user["password_hash"]):
                db.execute(
                    "INSERT INTO auth_attempts (username, ip_address, created_at) VALUES (?, ?, ?)",
                    (username, client_ip(), utcnow()),
                )
                db.commit()
                return api_error("用户名或密码错误", "invalid_credentials", 401)
            db.execute(
                "DELETE FROM auth_attempts WHERE username = ? AND ip_address = ?",
                (username, client_ip()),
            )
            db.commit()
        session.clear()
        session["user_id"] = int(user["id"])
        session["csrf_token"] = new_csrf_token()
        return jsonify({"ok": True, "csrf_token": session["csrf_token"]})

    @auth_bp.post("/api/auth/logout")
    def api_logout():
        session.clear()
        session["csrf_token"] = new_csrf_token()
        return jsonify({"ok": True, "csrf_token": session["csrf_token"]})

    @auth_bp.put("/api/auth/password")
    @login_required
    def api_change_password():
        data = request.get_json(force=True)
        current_password = data.get("current_password") or ""
        new_password = data.get("new_password") or ""
        if len(new_password) < 10 or len(new_password) > 128:
            return api_error("新密码长度需要在 10 到 128 位之间", "invalid_password", 400)
        with connect() as db:
            user = db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
            if user is None or not verify_password(current_password, user["password_hash"]):
                return api_error("当前密码错误", "invalid_credentials", 401)
            db.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
                (hash_password(new_password), utcnow(), user["id"]),
            )
            db.commit()
        return jsonify({"ok": True})

    @auth_bp.get("/api/session")
    def api_session():
        csrf_token = session.setdefault("csrf_token", new_csrf_token())
        return jsonify(
            {
                "initialized": has_user(),
                "authenticated": current_user_exists(),
                "default_username": DEFAULT_USERNAME,
                "csrf_token": csrf_token,
            }
        )

    @app.get("/healthz")
    def healthz():
        try:
            with connect() as db:
                db.execute("SELECT 1").fetchone()
            return jsonify({"ok": True, "worker": manager.worker_status()})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc), "code": "database_unavailable"}), 503

    @app.get("/api/overview")
    @login_required
    def api_overview():
        with connect() as db:
            server_count = db.execute("SELECT COUNT(*) AS count FROM servers").fetchone()["count"]
            enabled_server_count = db.execute("SELECT COUNT(*) AS count FROM servers WHERE enabled = 1").fetchone()["count"]
            running_jobs = db.execute("SELECT COUNT(*) AS count FROM jobs WHERE status = 'running'").fetchone()["count"]
            queued_jobs = db.execute("SELECT COUNT(*) AS count FROM jobs WHERE status = 'queued'").fetchone()["count"]
            failed_jobs = rows_to_dicts(
                db.execute(
                    """
                    SELECT jobs.*, servers.name AS server_name
                    FROM jobs JOIN servers ON servers.id = jobs.server_id
                    WHERE jobs.status = 'failed'
                    ORDER BY jobs.id DESC LIMIT 5
                    """
                ).fetchall()
            )
            recent_jobs = rows_to_dicts(
                db.execute(
                    """
                    SELECT jobs.*, servers.name AS server_name
                    FROM jobs JOIN servers ON servers.id = jobs.server_id
                    ORDER BY jobs.id DESC LIMIT 5
                    """
                ).fetchall()
            )
            webhook_events = db.execute("SELECT COUNT(*) AS count FROM webhook_events").fetchone()["count"]
            active_schedules = db.execute("SELECT COUNT(*) AS count FROM schedules WHERE enabled = 1").fetchone()["count"]
            notification_channels = db.execute("SELECT COUNT(*) AS count FROM notification_channels WHERE enabled = 1").fetchone()["count"]
            db_size = DB_FILE.stat().st_size if DB_FILE.exists() else 0
        for job in recent_jobs + failed_jobs:
            job["payload"] = decode_payload(job.get("payload", "{}"))
        return jsonify(
            {
                "servers": {"total": server_count, "enabled": enabled_server_count},
                "jobs": {"running": running_jobs, "queued": queued_jobs, "recent": recent_jobs, "failed": failed_jobs},
                "automation": {
                    "webhook_events": webhook_events,
                    "active_schedules": active_schedules,
                    "notification_channels": notification_channels,
                },
                "system": {"db_size": db_size},
                "worker": manager.worker_status(),
            }
        )

    @app.get("/api/overview/media")
    @login_required
    def api_overview_media():
        try:
            requested_limit = int(request.args.get("limit", "12"))
        except ValueError:
            requested_limit = 12
        limit = max(1, min(requested_limit, 24))
        with connect() as db:
            rows = db.execute("SELECT * FROM servers WHERE enabled = 1 ORDER BY id ASC").fetchall()
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

    @servers_bp.get("/api/servers")
    @login_required
    def list_servers():
        with connect() as db:
            rows = db.execute("SELECT * FROM servers ORDER BY id ASC").fetchall()
        return jsonify([serialize_server(row) for row in rows])

    @servers_bp.post("/api/servers")
    @login_required
    def create_server():
        data = request.get_json(force=True)
        name = (data.get("name") or "Plex Server").strip()
        try:
            address = validate_address(data.get("address") or "")
        except ValueError as exc:
            return api_error(str(exc), "invalid_server_address", 400)
        token = (data.get("token") or "").strip()
        pinyin_mode = data.get("pinyin_mode") or "first_letter"
        if not token:
            return api_error("Token 不能为空", "missing_token", 400)
        if pinyin_mode not in {"first_letter", "full_spell"}:
            return api_error("拼音模式无效", "invalid_pinyin_mode", 400)
        skip_libraries = data.get("skip_libraries") or []
        if isinstance(skip_libraries, str):
            skip_value = skip_libraries
        else:
            skip_value = join_skip_libraries(skip_libraries)
        now = utcnow()
        with connect() as db:
            cur = db.execute(
                """
                INSERT INTO servers (
                    name, address, token, skip_libraries, pinyin_mode, auth_source,
                    webhook_secret, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    address,
                    token,
                    skip_value,
                    pinyin_mode,
                    data.get("auth_source") or "manual",
                    new_secret(),
                    1 if data.get("enabled", True) else 0,
                    now,
                    now,
                ),
            )
            db.commit()
            server_id = int(cur.lastrowid or 0)
        return jsonify({"id": server_id})

    @servers_bp.put("/api/servers/<int:server_id>")
    @login_required
    def update_server(server_id: int):
        data = request.get_json(force=True)
        skip_libraries = data.get("skip_libraries") or []
        skip_value = skip_libraries if isinstance(skip_libraries, str) else join_skip_libraries(skip_libraries)
        with connect() as db:
            existing = db.execute("SELECT * FROM servers WHERE id = ?", (server_id,)).fetchone()
            if existing is None:
                return api_error("服务器不存在", "server_not_found", 404)
            try:
                address = validate_address(data.get("address") or existing["address"])
            except ValueError as exc:
                return api_error(str(exc), "invalid_server_address", 400)
            token = (data.get("token") or "").strip() or existing["token"]
            pinyin_mode = data.get("pinyin_mode") or "first_letter"
            if pinyin_mode not in {"first_letter", "full_spell"}:
                return api_error("拼音模式无效", "invalid_pinyin_mode", 400)
            db.execute(
                """
                UPDATE servers
                SET name = ?, address = ?, token = ?, skip_libraries = ?, pinyin_mode = ?, enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    (data.get("name") or "Plex Server").strip(),
                    address,
                    token,
                    skip_value,
                    pinyin_mode,
                    1 if data.get("enabled", True) else 0,
                    utcnow(),
                    server_id,
                ),
            )
            db.commit()
        return jsonify({"ok": True})

    @servers_bp.post("/api/plex/oauth/start")
    @login_required
    def plex_oauth_start():
        forward_url = url_for("servers.plex_oauth_callback", _external=True)
        result = create_pin(forward_url)
        flow_id = secrets.token_urlsafe(24)
        expires_at = (
            datetime.now(timezone.utc) + timedelta(minutes=10)
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
        with connect() as db:
            db.execute("DELETE FROM oauth_flows WHERE expires_at < ?", (utcnow(),))
            db.execute(
                """
                INSERT INTO oauth_flows (id, user_id, pin_id, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (flow_id, session["user_id"], int(result["pin_id"]), expires_at, utcnow()),
            )
            db.commit()
        return jsonify({"flow_id": flow_id, "code": result["code"], "auth_url": result["auth_url"]})

    @servers_bp.get("/plex/oauth/callback")
    def plex_oauth_callback():
        return render_template("plex_oauth_callback.html")

    @servers_bp.get("/api/plex/oauth/status/<flow_id>")
    @login_required
    def plex_oauth_status(flow_id: str):
        try:
            with connect() as db:
                flow = db.execute(
                    "SELECT * FROM oauth_flows WHERE id = ? AND user_id = ?",
                    (flow_id, session["user_id"]),
                ).fetchone()
            if flow is None or flow["expires_at"] < utcnow():
                return api_error("授权请求不存在或已过期", "oauth_flow_expired", 404)
            if flow["token"]:
                resources = json.loads(flow["resources"] or "[]")
            else:
                result = check_pin(flow["pin_id"])
                if not result.get("claimed"):
                    return jsonify({"claimed": False, "flow_id": flow_id})
                token = result["auth_token"]
                resources = []
                for index, resource in enumerate(result.get("servers") or []):
                    clean = {key: value for key, value in resource.items() if key != "token"}
                    clean["resource_index"] = index
                    resources.append(clean)
                with connect() as db:
                    db.execute(
                        "UPDATE oauth_flows SET token = ?, resources = ? WHERE id = ?",
                        (token, json.dumps(resources, ensure_ascii=False), flow_id),
                    )
                    db.commit()
            return jsonify({"claimed": True, "flow_id": flow_id, "servers": resources})
        except Exception as exc:
            return jsonify({"claimed": False, "error": str(exc), "code": "oauth_failed"}), 400

    @servers_bp.post("/api/plex/oauth/import-server")
    @login_required
    def plex_oauth_import_server():
        data = request.get_json(force=True)
        flow_id = data.get("flow_id") or ""
        resource_index = int(data.get("resource_index", -1))
        with connect() as db:
            flow = db.execute(
                "SELECT * FROM oauth_flows WHERE id = ? AND user_id = ? AND expires_at >= ?",
                (flow_id, session["user_id"], utcnow()),
            ).fetchone()
        if flow is None or not flow["token"]:
            return api_error("OAuth 授权结果不存在或已过期", "oauth_flow_expired", 404)
        resources = json.loads(flow["resources"] or "[]")
        if resource_index < 0 or resource_index >= len(resources):
            return api_error("OAuth 服务器选择无效", "invalid_oauth_resource", 400)
        resource = resources[resource_index]
        name = (resource.get("name") or "Plex Server").strip()
        allowed_connections = resource.get("connections") or []
        requested_uri = (data.get("connection_uri") or "").rstrip("/")
        connection = next(
            (item for item in allowed_connections if item.get("uri") == requested_uri),
            resource.get("best_connection") or (allowed_connections[0] if allowed_connections else {}),
        )
        try:
            address = validate_address(connection.get("uri") or "")
        except ValueError as exc:
            return api_error(str(exc), "invalid_server_address", 400)
        token = flow["token"]
        now = utcnow()
        with connect() as db:
            existing = db.execute("SELECT id FROM servers WHERE address = ?", (address,)).fetchone()
            if existing:
                db.execute(
                    """
                    UPDATE servers
                    SET name = ?, token = ?, auth_source = 'plex_oauth', enabled = 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (name, token, now, existing["id"]),
                )
                server_id = existing["id"]
            else:
                cur = db.execute(
                    """
                    INSERT INTO servers (
                        name, address, token, skip_libraries, pinyin_mode, auth_source,
                        webhook_secret, enabled, created_at, updated_at
                    ) VALUES (?, ?, ?, '', 'first_letter', 'plex_oauth', ?, 1, ?, ?)
                    """,
                    (name, address, token, new_secret(), now, now),
                )
                server_id = int(cur.lastrowid or 0)
            db.commit()
        return jsonify({"id": server_id})

    @servers_bp.delete("/api/servers/<int:server_id>")
    @login_required
    def delete_server(server_id: int):
        with connect() as db:
            db.execute("DELETE FROM servers WHERE id = ?", (server_id,))
            db.commit()
        return jsonify({"ok": True})

    @servers_bp.post("/api/servers/<int:server_id>/webhook-secret/rotate")
    @login_required
    def rotate_webhook_secret(server_id: int):
        secret = new_secret()
        with connect() as db:
            cur = db.execute(
                "UPDATE servers SET webhook_secret = ?, updated_at = ? WHERE id = ?",
                (secret, utcnow(), server_id),
            )
            db.commit()
        if not cur.rowcount:
            return api_error("服务器不存在", "server_not_found", 404)
        return jsonify(
            {
                "ok": True,
                "webhook_url": url_for(
                    "automation.webhook", server_id=server_id, secret=secret, _external=True
                ),
            }
        )

    @servers_bp.post("/api/servers/<int:server_id>/test")
    @login_required
    def test_server(server_id: int):
        try:
            with connect() as db:
                server = db.execute("SELECT * FROM servers WHERE id = ?", (server_id,)).fetchone()
            if server is None:
                return api_error("服务器不存在", "server_not_found", 404)
            plex = PlexServer(server_config_from_row(server), tags=load_tags(), auto_login=False)
            name = plex.login()
            return jsonify({"ok": True, "friendly_name": name})
        except Exception as exc:
            return api_error(str(exc), "plex_connection_failed", 400)

    @servers_bp.get("/api/servers/<int:server_id>/libraries")
    @login_required
    def server_libraries(server_id: int):
        try:
            with connect() as db:
                server = db.execute("SELECT * FROM servers WHERE id = ?", (server_id,)).fetchone()
            if server is None:
                return api_error("服务器不存在", "server_not_found", 404)
            plex = PlexServer(server_config_from_row(server), tags=load_tags(), auto_login=True)
            upstream = [{"key": item[0], "type": item[1], "title": item[2]} for item in plex.list_library()]
            with connect() as db:
                now = utcnow()
                for item in upstream:
                    db.execute("INSERT OR IGNORE INTO media_libraries (server_id, library_id, title, plex_type, auto_animation, updated_at) VALUES (?, ?, ?, ?, ?, ?)", (server_id, int(item["key"]), item["title"], int(item["type"]), 1 if _library_name_is_animation(item["title"], item["type"]) else 0, now))
                db.commit()
                settings = {int(row["library_id"]): dict(row) for row in db.execute("SELECT * FROM media_libraries WHERE server_id = ?", (server_id,)).fetchall()}
            libraries = [{**item, "animation_mode": settings.get(int(item["key"]), {}).get("animation_mode", "auto"),
                          "auto_animation": bool(settings.get(int(item["key"]), {}).get("auto_animation", _library_name_is_animation(item["title"], item["type"]))),
                          "is_animation": _library_is_animation(settings.get(int(item["key"]), {}).get("animation_mode", "auto"), item["title"], item["type"])} for item in upstream]
            return jsonify(libraries)
        except Exception as exc:
            return api_error(str(exc), "plex_library_failed", 400)

    @servers_bp.put("/api/servers/<int:server_id>/libraries/<int:library_id>/settings")
    @login_required
    def update_library_settings(server_id: int, library_id: int):
        data = request.get_json(force=True) or {}
        mode = str(data.get("animation_mode") or "auto")
        if mode not in {"auto", "animation", "normal"}:
            return api_error("动画媒体库模式无效", "invalid_animation_mode", 400)
        with connect() as db:
            library = db.execute("SELECT title, plex_type FROM media_libraries WHERE server_id = ? AND library_id = ?", (server_id, library_id)).fetchone()
            if library is None:
                return api_error("媒体库尚未同步，请先从媒体服务器重新拉取", "library_cache_missing", 404)
            db.execute("UPDATE media_libraries SET animation_mode = ?, updated_at = ? WHERE server_id = ? AND library_id = ?", (mode, utcnow(), server_id, library_id))
            db.commit()
        return jsonify({"ok": True, "animation_mode": mode, "is_animation": _library_is_animation(mode, library["title"], library["plex_type"])})

    @servers_bp.get("/api/servers/<int:server_id>/media-image")
    @login_required
    def media_image(server_id: int):
        with connect() as db:
            server = db.execute(
                "SELECT * FROM servers WHERE id = ? AND enabled = 1", (server_id,)
            ).fetchone()
        if server is None:
            return api_error("服务器不存在或已停用", "server_not_found", 404)
        try:
            path = validate_media_path(request.args.get("path", ""))
            plex = PlexServer(server_config_from_row(server), tags=load_tags(), auto_login=False)
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
        with connect() as db:
            server = db.execute("SELECT id FROM servers WHERE id = ? AND enabled = 1", (server_id,)).fetchone()
            if server is None:
                return api_error("服务器不存在或已停用", "server_not_found", 404)
            libraries = db.execute("""SELECT media_libraries.*, jobs.status AS job_status, jobs.stage AS job_stage,
                                      jobs.current_library AS job_current_library, jobs.processed AS job_processed,
                                      jobs.total AS job_total, jobs.error AS job_error
                               FROM media_libraries LEFT JOIN jobs ON jobs.id = media_libraries.sync_job_id
                               WHERE media_libraries.server_id = ? ORDER BY media_libraries.title COLLATE NOCASE""", (server_id,)).fetchall()
        if not libraries:
            try:
                with connect() as db:
                    server_row = db.execute("SELECT * FROM servers WHERE id = ? AND enabled = 1", (server_id,)).fetchone()
                plex = PlexServer(server_config_from_row(server_row), tags=load_tags(), auto_login=True)
                now = utcnow()
                with connect() as db:
                    for key, plex_type, title in plex.list_library():
                        db.execute("INSERT OR IGNORE INTO media_libraries (server_id, library_id, title, plex_type, auto_animation, updated_at) VALUES (?, ?, ?, ?, ?, ?)", (server_id, int(key), str(title), int(plex_type), 1 if _library_name_is_animation(title, plex_type) else 0, now))
                    db.commit()
                    libraries = db.execute("""SELECT media_libraries.*, jobs.status AS job_status, jobs.stage AS job_stage,
                                              jobs.current_library AS job_current_library, jobs.processed AS job_processed,
                                              jobs.total AS job_total, jobs.error AS job_error
                                       FROM media_libraries LEFT JOIN jobs ON jobs.id = media_libraries.sync_job_id
                                       WHERE media_libraries.server_id = ? ORDER BY media_libraries.title COLLATE NOCASE""", (server_id,)).fetchall()
            except Exception as exc:
                return api_error(str(exc), "media_library_bootstrap_failed", 400)
        with connect() as db:
            output = []
            for library in libraries:
                count = db.execute("SELECT COUNT(*) AS count FROM media_library_items WHERE server_id = ? AND library_id = ? AND parent_rating_key = '' AND plex_type IN ('movie','show','collection')", (server_id, library["library_id"])).fetchone()["count"]
                output.append({"id": int(library["library_id"]), "title": library["title"], "plex_type": int(library["plex_type"]),
                               "kind": "movie" if int(library["plex_type"]) == 1 else "show" if int(library["plex_type"]) == 2 else "collection",
                               "items": [], "item_count": int(count), "animation_mode": library["animation_mode"],
                               "auto_animation": bool(library["auto_animation"]), "is_animation": _library_is_animation(library["animation_mode"], library["title"], library["plex_type"]),
                               "plex_synced_at": library["plex_synced_at"], "tmdb_synced_at": library["tmdb_synced_at"],
                               "sync_status": library["job_status"] or library["sync_status"], "sync_job_id": library["sync_job_id"],
                               "sync_error": library["job_error"] or library["sync_error"], "sync_stage": library["job_stage"] or "",
                               "sync_current_library": library["job_current_library"] or "", "sync_processed": int(library["job_processed"] or 0),
                               "sync_total": int(library["job_total"] or 0)})
        return jsonify({"server_id": server_id, "libraries": output, "generated_at": utcnow()})

    @servers_bp.get("/api/servers/<int:server_id>/media-library/<int:library_id>/items")
    @login_required
    def media_library_items(server_id: int, library_id: int):
        with connect() as db:
            library = db.execute("SELECT * FROM media_libraries WHERE server_id = ? AND library_id = ?", (server_id, library_id)).fetchone()
            if library is None:
                return api_error("媒体库缓存不存在，请先同步", "library_cache_missing", 404)
            rows = db.execute("SELECT * FROM media_library_items WHERE server_id = ? AND library_id = ? AND parent_rating_key = '' AND plex_type IN ('movie','show','collection') ORDER BY title COLLATE NOCASE", (server_id, library_id)).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                raw = decode_payload(item.pop("raw_json", "{}"))
                item["external_ids"] = raw.get("external_ids") or extract_external_ids(raw)
                item["library_id"] = library_id
                item["show_kind"] = "动画" if library["plex_type"] == 2 and _library_is_animation(library["animation_mode"], library["title"], library["plex_type"]) else "真人"
                item["season_count"] = db.execute("SELECT COUNT(DISTINCT season_number) AS count FROM media_library_items WHERE server_id = ? AND library_id = ? AND parent_rating_key = ? AND plex_type = 'season'", (server_id, library_id, row["rating_key"])).fetchone()["count"]
                item["episode_count"] = db.execute("SELECT COUNT(*) AS count FROM media_library_items WHERE server_id = ? AND library_id = ? AND parent_rating_key = ? AND plex_type = 'episode'", (server_id, library_id, row["rating_key"])).fetchone()["count"]
                items.append(item)
        return jsonify({"server_id": server_id, "library_id": library_id, "items": items})

    @servers_bp.post("/api/servers/<int:server_id>/media-library/<int:library_id>/refresh")
    @login_required
    def refresh_media_library(server_id: int, library_id: int):
        try:
            job_id = manager.create_job("media_library_refresh", server_id, {"server_id": server_id, "library_id": library_id})
            with connect() as db:
                db.execute("UPDATE media_libraries SET sync_job_id = ?, sync_status = 'queued', sync_error = '', updated_at = ? WHERE server_id = ? AND library_id = ?", (job_id, utcnow(), server_id, library_id))
                db.commit()
            return jsonify({"id": job_id})
        except (TypeError, ValueError) as exc:
            return api_error(str(exc), "invalid_media_library_refresh", 400)

    @servers_bp.post("/api/servers/<int:server_id>/media-library/<int:library_id>/tmdb-refresh")
    @login_required
    def refresh_media_library_tmdb(server_id: int, library_id: int):
        try:
            job_id = manager.create_job("media_library_tmdb_refresh", server_id, {"server_id": server_id, "library_id": library_id})
            with connect() as db:
                db.execute("UPDATE media_libraries SET sync_job_id = ?, sync_status = 'queued', sync_error = '', updated_at = ? WHERE server_id = ? AND library_id = ?", (job_id, utcnow(), server_id, library_id))
                db.commit()
            return jsonify({"id": job_id})
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
            with connect() as db:
                server = db.execute("SELECT * FROM servers WHERE id = ? AND enabled = 1", (server_id,)).fetchone()
            if server is None:
                return api_error("服务器不存在或已停用", "server_not_found", 404)
            plex = PlexServer(server_config_from_row(server), tags=load_tags(), auto_login=True)
            metadata = plex.get_metadata(rating_key)
            if str(metadata.get("type") or "") != "show":
                return api_error("只支持重新检查剧集", "invalid_media_recheck_type", 400)
            with connect() as db:
                _upsert_media_library_item(db, server_id, library_id, metadata, "show")
                for season in plex.get_children(rating_key):
                    season_index = season.get("index")
                    if season_index is None:
                        season_index = season.get("parentIndex")
                    season_no = int(season_index or 0)
                    _upsert_media_library_item(db, server_id, library_id, season, "season", rating_key, season_no, None)
                for episode in plex.list_show_episodes(rating_key):
                    season_no = int(episode.get("parentIndex") or 0)
                    episode_no = int(episode.get("index") or 0)
                    _upsert_media_library_item(db, server_id, library_id, episode, "episode", rating_key, season_no, episode_no)
                    _remove_expected_episode_placeholder(db, server_id, library_id, rating_key, season_no, episode_no)
                library = db.execute("SELECT * FROM media_libraries WHERE server_id = ? AND library_id = ?", (server_id, library_id)).fetchone()
                db.commit()
                return jsonify(_cached_show_payload(db, server_id, library_id, db.execute("SELECT * FROM media_library_items WHERE server_id = ? AND library_id = ? AND rating_key = ?", (server_id, library_id, rating_key)).fetchone(), library))
        except Exception as exc:
            return api_error(str(exc), "media_recheck_failed", 400)

    @servers_bp.get("/api/servers/<int:server_id>/media-library/<int:library_id>/quarter-index")
    @login_required
    def media_library_quarter_index(server_id: int, library_id: int):
        with connect() as db:
            library = db.execute("SELECT * FROM media_libraries WHERE server_id = ? AND library_id = ?", (server_id, library_id)).fetchone()
            if library is None or int(library["plex_type"]) != 2:
                return api_error("电视剧库缓存不存在", "library_not_found", 404)
            shows = db.execute("SELECT * FROM media_library_items WHERE server_id = ? AND library_id = ? AND plex_type = 'show' ORDER BY title COLLATE NOCASE", (server_id, library_id)).fetchall()
            entries, live_items = [], []
            is_animation_library = _library_is_animation(library["animation_mode"], library["title"], library["plex_type"])
            for show in shows:
                full = _cached_show_payload(db, server_id, library_id, show, library)
                if is_animation_library:
                    for season in full["seasons"]:
                        dates = [episode["air_date"] for episode in season["episodes"] if episode.get("air_date")]
                        first_date = min(dates) if dates else season.get("release_date") or ""
                        added = [episode["added_at"] for episode in season["episodes"] if episode.get("added_at")]
                        entries.append({**full, "season": season["season"], "bucket": season["bucket"], "release_date": season.get("release_date") or "",
                                        "first_episode_date": first_date, "latest_added_at": max(added) if added else "",
                                        "episode_count": len(season["episodes"]), "missing_count": sum(1 for episode in season["episodes"] if episode.get("missing")),
                                        "is_special": season["season"] == 0, "is_undated": not bool(first_date)})
                else:
                    live_items.append(full)
            groups = {}
            for entry in entries:
                groups.setdefault(entry["bucket"], []).append(entry)
            def group_key(label: str):
                # Normal quarters are always newest-first.  Special episodes
                # and undated seasons are kept as explicit trailing groups.
                if label == "特别篇": return (1, 0, 0)
                if label == "未定档": return (2, 0, 0)
                try:
                    return (0, -int(label[:4]), -int(label.split("Q", 1)[1].split()[0]))
                except (TypeError, ValueError, IndexError):
                    return (2, 0, 0)
            for group in groups.values():
                group.sort(key=lambda item: (item.get("first_episode_date") or "9999-99-99", str(item.get("title") or "").casefold()))
            result = [{"label": label, "items": groups[label]} for label in sorted(groups, key=group_key)]
        return jsonify({"server_id": server_id, "library_id": library_id, "groups": result, "live_items": live_items})

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
        with connect() as db:
            library = db.execute("SELECT * FROM media_libraries WHERE server_id = ? AND library_id = ?", (server_id, library_id)).fetchone()
            row = db.execute("SELECT * FROM media_library_items WHERE server_id = ? AND library_id = ? AND rating_key = ?", (server_id, library_id, rating_key)).fetchone()
            if library is None or row is None:
                return api_error("媒体条目缓存不存在，请先同步媒体库", "media_item_not_found", 404)
            if row["plex_type"] == "show":
                return jsonify(_cached_show_payload(db, server_id, library_id, row, library))
            raw = decode_payload(row["raw_json"])
            item = dict(row)
            item.pop("raw_json", None)
            item.update({"library_id": library_id, "summary": raw.get("summary") or "", "external_ids": raw.get("external_ids") or extract_external_ids(raw), "collections": []})
        return jsonify(item)

    @jobs_bp.post("/api/jobs")
    @login_required
    def create_job():
        data = request.get_json(force=True)
        payload = data.get("payload") or {}
        if "mode" in data or "scope" in data:
            payload = {**payload, "mode": data.get("mode", payload.get("mode")), "scope": data.get("scope", payload.get("scope") or {})}
        try:
            job_id = manager.create_job(data.get("type") or "all", int(data["server_id"]), payload)
            return jsonify({"id": job_id})
        except (KeyError, TypeError, ValueError) as exc:
            return api_error(str(exc), "invalid_job", 400)

    @jobs_bp.get("/api/jobs")
    @login_required
    def list_jobs():
        return jsonify(manager.list_jobs())

    @jobs_bp.get("/api/jobs/<int:job_id>")
    @login_required
    def get_job(job_id: int):
        return jsonify(manager.get_job(job_id))

    @jobs_bp.get("/api/jobs/<int:job_id>/logs")
    @login_required
    def get_logs(job_id: int):
        after = int(request.args.get("after", "0"))
        limit = int(request.args.get("limit", "300"))
        return jsonify(manager.list_logs(job_id, after, limit))

    @jobs_bp.get("/api/jobs/<int:job_id>/changes")
    @login_required
    def get_job_changes(job_id: int):
        limit = max(1, min(int(request.args.get("limit", "500")), 2000))
        with connect() as db:
            rows = db.execute("SELECT * FROM changes WHERE job_id = ? ORDER BY id ASC LIMIT ?", (job_id, limit)).fetchall()
        changes = rows_to_dicts(rows)
        for change in changes:
            change["old_value"] = json.loads(change.get("old_value") or "null")
            change["new_value"] = json.loads(change.get("new_value") or "null")
            if change.get("rollback_status") == "rolled_back":
                change["status"] = "rolled_back"
            elif change.get("apply_status") in {"conflict", "failed"} or change.get(
                "rollback_status"
            ) in {"conflict", "failed"}:
                change["status"] = "conflict"
            elif change.get("apply_status") == "applied":
                change["status"] = "applied"
            else:
                change["status"] = "pending"
        return jsonify(changes)

    @jobs_bp.post("/api/jobs/<int:job_id>/rollback")
    @login_required
    def rollback_job(job_id: int):
        return jsonify({"id": manager.rollback_job(job_id)})

    @jobs_bp.post("/api/jobs/<int:job_id>/apply-preview")
    @login_required
    def apply_preview_job(job_id: int):
        try:
            return jsonify({"id": manager.apply_preview_job(job_id)})
        except ValueError as exc:
            return api_error(str(exc), "preview_not_applicable", 400)

    @jobs_bp.post("/api/jobs/<int:job_id>/cancel")
    @login_required
    def cancel_job(job_id: int):
        try:
            return jsonify({"ok": True, "status": manager.cancel_job(job_id)})
        except ValueError as exc:
            return api_error(str(exc), "job_not_found", 404)

    @jobs_bp.post("/api/jobs/<int:job_id>/retry")
    @login_required
    def retry_job(job_id: int):
        try:
            return jsonify({"id": manager.retry_job(job_id)})
        except ValueError as exc:
            return api_error(str(exc), "job_not_retryable", 400)

    @jobs_bp.get("/api/jobs/<int:job_id>/events")
    @login_required
    def job_events(job_id: int):
        initial_after = max(0, int(request.args.get("after", "0")))

        def stream():
            last_job = ""
            last_log_id = initial_after
            last_keepalive = time.monotonic()
            while True:
                try:
                    job = manager.get_job(job_id)
                except ValueError:
                    yield f"data: {json.dumps({'kind': 'error', 'error': '任务不存在'}, ensure_ascii=False)}\n\n"
                    return
                encoded = json.dumps(job, ensure_ascii=False, sort_keys=True)
                if encoded != last_job:
                    last_job = encoded
                    yield f"data: {json.dumps({'kind': 'job', 'job': job}, ensure_ascii=False)}\n\n"
                logs = manager.list_logs(job_id, after_id=last_log_id, limit=500)
                for log in logs:
                    last_log_id = max(last_log_id, int(log["id"]))
                    yield f"data: {json.dumps({'kind': 'log', 'log': log}, ensure_ascii=False)}\n\n"
                if job["status"] in TERMINAL_JOB_STATUSES:
                    return
                if time.monotonic() - last_keepalive >= 15:
                    last_keepalive = time.monotonic()
                    yield ": keepalive\n\n"
                time.sleep(1)

        response = Response(stream(), mimetype="text/event-stream")
        response.headers["Cache-Control"] = "no-cache"
        response.headers["X-Accel-Buffering"] = "no"
        return response

    @automation_bp.get("/api/schedules")
    @login_required
    def list_schedules():
        with connect() as db:
            rows = db.execute(
                """
                SELECT schedules.*, servers.name AS server_name
                FROM schedules JOIN servers ON servers.id = schedules.server_id
                ORDER BY schedules.id DESC
                """
            ).fetchall()
        schedules = rows_to_dicts(rows)
        for schedule in schedules:
            schedule["label"] = describe_schedule(schedule["schedule_type"], schedule["schedule_value"])
            schedule["payload"] = decode_payload(schedule.get("payload", "{}"))
        return jsonify(schedules)

    @automation_bp.post("/api/schedules")
    @login_required
    def save_schedule():
        data = request.get_json(force=True)
        schedule_type = data.get("schedule_type") or "cron"
        schedule_value = (data.get("schedule_value") or "").strip()
        schedule_payload = data.get("payload") or {}
        try:
            validate_schedule(schedule_type, schedule_value)
        except ValueError as exc:
            return api_error(str(exc), "invalid_schedule", 400)
        now = utcnow()
        enabled = 1 if data.get("enabled", True) else 0
        next_value = next_run_at(schedule_type, schedule_value) if enabled else None
        with connect() as db:
            schedule_id = data.get("id")
            if schedule_id:
                db.execute(
                    """
                    UPDATE schedules
                    SET server_id = ?, name = ?, schedule_type = ?, schedule_value = ?, payload = ?,
                        enabled = ?, next_run_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        int(data["server_id"]),
                        data.get("name") or "定时全量任务",
                        schedule_type,
                        schedule_value,
                        json.dumps(schedule_payload, ensure_ascii=False),
                        enabled,
                        next_value,
                        now,
                        int(schedule_id),
                    ),
                )
            else:
                db.execute(
                    """
                    INSERT INTO schedules (
                        server_id, name, schedule_type, schedule_value, payload, enabled,
                        next_run_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(data["server_id"]),
                        data.get("name") or "定时全量任务",
                        schedule_type,
                        schedule_value,
                        json.dumps(schedule_payload, ensure_ascii=False),
                        enabled,
                        next_value,
                        now,
                        now,
                    ),
                )
            db.commit()
        return jsonify({"ok": True})

    @automation_bp.get("/api/schedules/describe")
    @login_required
    def describe_schedule_api():
        schedule_type = request.args.get("type", "")
        schedule_value = request.args.get("value", "")
        try:
            validate_schedule(schedule_type, schedule_value)
            return jsonify(
                {
                    "label": describe_schedule(schedule_type, schedule_value),
                    "next_run_at": next_run_at(schedule_type, schedule_value),
                }
            )
        except ValueError as exc:
            return api_error(str(exc), "invalid_schedule", 400)

    @automation_bp.delete("/api/schedules/<int:schedule_id>")
    @login_required
    def delete_schedule(schedule_id: int):
        with connect() as db:
            db.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
            db.commit()
        return jsonify({"ok": True})

    @tools_bp.get("/api/tags")
    @login_required
    def get_tags():
        return jsonify(load_tags())

    @tools_bp.post("/api/tags")
    @login_required
    def update_tags():
        data = request.get_json(force=True)
        if not isinstance(data, dict) or not all(
            isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip()
            for k, v in data.items()
        ):
            return api_error("标签映射必须是非空字符串到非空字符串的 JSON 对象", "invalid_tags", 400)
        save_tags(data)
        return jsonify({"ok": True})

    @tools_bp.post("/api/tags/bulk-add")
    @login_required
    def bulk_add_tags():
        data = request.get_json(force=True)
        entries = data.get("entries") or []
        tags = load_tags()
        for entry in entries:
            source = (entry.get("source") or "").strip()
            target = (entry.get("target") or entry.get("suggested") or "").strip()
            if source and target:
                tags[source] = target
        save_tags(tags)
        return jsonify({"ok": True, "count": len(entries)})

    @tools_bp.post("/api/tag-suggestions/scan")
    @login_required
    def scan_tag_suggestions():
        data = request.get_json(force=True)
        job_id = manager.create_job("tag_suggestions", int(data["server_id"]), {})
        return jsonify({"id": job_id})

    @tools_bp.get("/api/tag-suggestions")
    @login_required
    def list_tag_suggestions():
        server_id = request.args.get("server_id")
        with connect() as db:
            if server_id:
                rows = db.execute(
                    "SELECT * FROM tag_suggestions WHERE server_id = ? ORDER BY count DESC, source ASC",
                    (int(server_id),),
                ).fetchall()
            else:
                rows = db.execute("SELECT * FROM tag_suggestions ORDER BY count DESC, source ASC").fetchall()
        return jsonify(rows_to_dicts(rows))

    @tools_bp.get("/api/diagnostics")
    @login_required
    def diagnostics_all():
        with connect() as db:
            servers = db.execute("SELECT id FROM servers WHERE enabled = 1 ORDER BY id ASC").fetchall()
        return jsonify([build_diagnostics(row["id"]) for row in servers])

    @tools_bp.get("/api/diagnostics/<int:server_id>")
    @login_required
    def diagnostics_server(server_id: int):
        return jsonify(build_diagnostics(server_id))

    @tools_bp.post("/api/diagnostics/<int:server_id>/write-test")
    @login_required
    def diagnostics_write_test(server_id: int):
        return jsonify({"ok": True, "message": "写入测试入口已保留；为避免误改 Plex 元数据，首版只做只读诊断。"})

    def build_diagnostics(server_id: int):
        checks = []
        status = "ok"
        with connect() as db:
            server = db.execute("SELECT * FROM servers WHERE id = ?", (server_id,)).fetchone()
            if server is None:
                return {"server_id": server_id, "status": "error", "checks": [{"name": "服务器", "status": "error", "message": "不存在"}]}
            db_size = DB_FILE.stat().st_size if DB_FILE.exists() else 0
            queued = db.execute("SELECT COUNT(*) AS count FROM jobs WHERE status = 'queued'").fetchone()["count"]
            running = db.execute("SELECT COUNT(*) AS count FROM jobs WHERE status = 'running'").fetchone()["count"]
            schedules = db.execute("SELECT COUNT(*) AS count FROM schedules WHERE server_id = ? AND enabled = 1", (server_id,)).fetchone()["count"]
        try:
            plex = PlexServer(server_config_from_row(server), tags=load_tags(), auto_login=False)
            friendly_name = plex.login()
            checks.append({"name": "Plex 连接", "status": "ok", "message": friendly_name})
            libraries = plex.list_library()
            checks.append({"name": "媒体库读取", "status": "ok", "message": f"{len(libraries)} 个媒体库"})
            try:
                activities = plex.activities()
                count = len(activities.get("MediaContainer", {}).get("Activity", []) or [])
                checks.append({"name": "PMS 活动", "status": "ok", "message": f"{count} 个活动"})
            except Exception as exc:
                checks.append({"name": "PMS 活动", "status": "warning", "message": str(exc)})
        except Exception as exc:
            status = "error"
            checks.append({"name": "Plex 连接", "status": "error", "message": str(exc)})
        checks.extend(
            [
                {"name": "数据库", "status": "ok", "message": f"{db_size} bytes"},
                {"name": "任务队列", "status": "warning" if queued > 10 else "ok", "message": f"排队 {queued}，运行 {running}"},
                {"name": "定时任务", "status": "ok", "message": f"{schedules} 个启用"},
                {
                    "name": "Webhook URL",
                    "status": "ok",
                    "message": url_for(
                        "automation.webhook",
                        server_id=server_id,
                        secret=server["webhook_secret"],
                        _external=True,
                    ),
                },
                {
                    "name": "任务 Worker",
                    "status": "ok" if manager.worker_status()["online"] else "warning",
                    "message": "在线" if manager.worker_status()["online"] else "离线",
                },
            ]
        )
        if any(check["status"] == "warning" for check in checks) and status != "error":
            status = "warning"
        return {"server_id": server_id, "server_name": server["name"], "status": status, "checks": checks}

    @tools_bp.post("/api/maintenance/jobs")
    @login_required
    def create_maintenance_job():
        data = request.get_json(force=True)
        job_id = manager.create_job("maintenance", int(data["server_id"]), data)
        return jsonify({"id": job_id})

    @tools_bp.post("/api/continue-watching/preview")
    @login_required
    def create_continue_watching_preview():
        data = request.get_json(force=True)
        job_id = manager.create_job(
            "continue_watching_preview",
            int(data["server_id"]),
            {"library_id": int(data["library_id"])},
        )
        return jsonify({"id": job_id, "message": "已创建继续观看候选预览任务。"})

    @tools_bp.get("/api/continue-watching/runs")
    @login_required
    def list_continue_watching_runs():
        with connect() as db:
            rows = db.execute(
                """
                SELECT continue_watching_runs.*, servers.name AS server_name, jobs.status AS job_status
                FROM continue_watching_runs
                JOIN servers ON servers.id = continue_watching_runs.server_id
                LEFT JOIN jobs ON jobs.id = continue_watching_runs.job_id
                ORDER BY continue_watching_runs.id DESC LIMIT 50
                """
            ).fetchall()
        return jsonify(rows_to_dicts(rows))

    @tools_bp.get("/api/continue-watching/runs/<int:run_id>/items")
    @login_required
    def list_continue_watching_items(run_id: int):
        with connect() as db:
            rows = db.execute(
                "SELECT * FROM continue_watching_items WHERE run_id = ? ORDER BY show_title ASC, season ASC, episode ASC",
                (run_id,),
            ).fetchall()
        return jsonify(rows_to_dicts(rows))

    @tools_bp.post("/api/continue-watching/apply")
    @login_required
    def apply_continue_watching_items():
        data = request.get_json(force=True)
        item_ids = [int(item_id) for item_id in (data.get("item_ids") or [])]
        if not item_ids:
            return api_error("请选择要执行的候选剧集", "missing_candidate_items", 400)
        with connect() as db:
            rows = db.execute(
                f"""
                SELECT DISTINCT server_id, library_id FROM continue_watching_items
                WHERE id IN ({','.join(['?'] * len(item_ids))}) AND status = 'candidate'
                """,
                item_ids,
            ).fetchall()
        if len(rows) != 1:
            return api_error("候选剧集必须有效且来自同一服务器和媒体库", "invalid_candidates", 400)
        server_id = int(rows[0]["server_id"])
        job_id = manager.create_job("continue_watching_apply", server_id, {"item_ids": item_ids})
        return jsonify({"id": job_id, "message": "已创建继续观看执行任务。"})

    @tools_bp.get("/api/settings/tmdb")
    @login_required
    def get_tmdb_settings():
        return jsonify({"configured": bool(get_setting("tmdb_api_key", DB_FILE))})

    @tools_bp.post("/api/settings/tmdb")
    @login_required
    def save_tmdb_settings():
        data = request.get_json(force=True)
        api_key = (data.get("api_key") or "").strip()
        if not api_key:
            return api_error("TMDB API Key 不能为空", "missing_tmdb_api_key", 400)
        set_setting("tmdb_api_key", api_key, DB_FILE)
        return jsonify({"ok": True})

    @tools_bp.get("/api/catalog/items")
    @login_required
    def list_catalog_items():
        media_type = (request.args.get("media_type") or "").strip()
        status = (request.args.get("status") or "").strip()
        server_id = request.args.get("server_id")
        q = (request.args.get("q") or "").strip()
        page = max(1, int(request.args.get("page", "1")))
        size = max(1, min(100, int(request.args.get("page_size", "24"))))
        clauses = ["1 = 1"]
        params: list[Any] = []
        if media_type:
            clauses.append("i.media_type = ?")
            params.append(media_type)
        if q:
            clauses.append("(i.title LIKE ? OR i.original_title LIKE ?)")
            params.extend([f"%{q}%", f"%{q}%"])
        join = ""
        if server_id:
            join = "LEFT JOIN media_match_results m ON m.media_type=i.media_type AND m.tmdb_id=i.tmdb_id AND IFNULL(m.season_number,-1)=IFNULL(i.season_number,-1) AND IFNULL(m.episode_number,-1)=IFNULL(i.episode_number,-1) AND m.server_id=?"
            params.insert(0, int(server_id))
            if status:
                clauses.append("m.status = ?")
                params.append(status)
        with connect() as db:
            total = db.execute(f"SELECT COUNT(*) AS count FROM tmdb_items i {join} WHERE {' AND '.join(clauses)}", params).fetchone()["count"]
            params.extend([(page - 1) * size, size])
            rows = db.execute(f"SELECT i.*, m.status, m.match_source, m.plex_rating_key FROM tmdb_items i {join} WHERE {' AND '.join(clauses)} ORDER BY i.release_date DESC, i.title LIMIT ? OFFSET ?", params).fetchall()
        return jsonify({"items": rows_to_dicts(rows), "page": page, "page_size": size, "total": total})

    @tools_bp.get("/api/catalog/items/<media_type>/<int:tmdb_id>")
    @login_required
    def catalog_item_detail(media_type: str, tmdb_id: int):
        with connect() as db:
            row = db.execute("SELECT * FROM tmdb_items WHERE media_type = ? AND tmdb_id = ? ORDER BY season_number, episode_number LIMIT 1", (media_type, tmdb_id)).fetchone()
            children = db.execute("SELECT * FROM tmdb_items WHERE parent_tmdb_id = ? ORDER BY season_number, episode_number", (tmdb_id,)).fetchall()
        if row is None:
            return api_error("目录项目不存在", "catalog_item_not_found", 404)
        item = dict(row); item["raw_json"] = decode_payload(item.get("raw_json")); item["children"] = rows_to_dicts(children)
        return jsonify(item)

    @tools_bp.get("/api/catalog/stats")
    @login_required
    def catalog_stats():
        with connect() as db:
            counts = {row["media_type"]: row["count"] for row in db.execute("SELECT media_type, COUNT(*) AS count FROM tmdb_items GROUP BY media_type").fetchall()}
            statuses = {row["status"]: row["count"] for row in db.execute("SELECT status, COUNT(*) AS count FROM media_match_results GROUP BY status").fetchall()}
        return jsonify({"items": counts, "matches": statuses})

    @tools_bp.post("/api/catalog/sync")
    @login_required
    def create_catalog_sync():
        data = request.get_json(force=True) or {}
        if not get_setting("tmdb_api_key", DB_FILE):
            return api_error("请先保存 TMDB API Read Access Token", "missing_tmdb_token", 400)
        server_id = int(data.get("server_id") or 0)
        if not server_id:
            with connect() as db:
                row = db.execute("SELECT id FROM servers WHERE enabled = 1 ORDER BY id LIMIT 1").fetchone()
            if row is None:
                return api_error("请先配置至少一台 Plex 服务器", "server_required", 400)
            server_id = int(row["id"])
        payload = {"media_types": data.get("media_types") or ["movie", "tv"], "filters": data.get("filters") or {}, "max_pages": data.get("max_pages") or 1}
        job_id = manager.create_job("tmdb_catalog_sync", server_id, payload)
        return jsonify({"id": job_id})

    @tools_bp.post("/api/catalog/plex-inventory/sync")
    @login_required
    def create_inventory_sync():
        data = request.get_json(force=True) or {}
        server_id = int(data.get("server_id") or 0)
        job_id = manager.create_job("plex_inventory_sync", server_id, {"server_id": server_id, "library_ids": data.get("library_ids")})
        return jsonify({"id": job_id})

    @tools_bp.get("/api/episode-match-overrides")
    @login_required
    def list_episode_match_overrides():
        clauses = ["1 = 1"]
        params: list[Any] = []
        if request.args.get("server_id"):
            clauses.append("server_id = ?")
            params.append(int(request.args["server_id"]))
        if request.args.get("library_id"):
            clauses.append("library_id = ?")
            params.append(int(request.args["library_id"]))
        with connect() as db:
            rows = db.execute(
                f"SELECT * FROM episode_match_overrides WHERE {' AND '.join(clauses)} "
                "ORDER BY server_id, library_id, plex_rating_key",
                params,
            ).fetchall()
        return jsonify(rows_to_dicts(rows))

    @tools_bp.put("/api/episode-match-overrides")
    @login_required
    def save_episode_match_override():
        data = request.get_json(force=True)
        try:
            server_id = int(data["server_id"])
            library_id = int(data["library_id"])
            rating_key = str(data["plex_rating_key"]).strip()
            tmdb_id = int(data["tmdb_id"])
        except (KeyError, TypeError, ValueError):
            return api_error("匹配覆盖参数无效", "invalid_match_override", 400)
        if not rating_key or tmdb_id <= 0:
            return api_error("匹配覆盖参数无效", "invalid_match_override", 400)
        now = utcnow()
        with connect() as db:
            db.execute(
                """
                INSERT INTO episode_match_overrides (
                    server_id, library_id, plex_rating_key, tmdb_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(server_id, library_id, plex_rating_key)
                DO UPDATE SET tmdb_id = excluded.tmdb_id, updated_at = excluded.updated_at
                """,
                (server_id, library_id, rating_key, tmdb_id, now, now),
            )
            db.commit()
        return jsonify({"ok": True})

    @tools_bp.delete("/api/episode-match-overrides/<int:server_id>/<int:library_id>/<rating_key>")
    @login_required
    def delete_episode_match_override(server_id: int, library_id: int, rating_key: str):
        with connect() as db:
            db.execute(
                """
                DELETE FROM episode_match_overrides
                WHERE server_id = ? AND library_id = ? AND plex_rating_key = ?
                """,
                (server_id, library_id, rating_key),
            )
            db.commit()
        return jsonify({"ok": True})

    @tools_bp.post("/api/episode-audits")
    @login_required
    def create_episode_audit():
        data = request.get_json(force=True)
        if not get_setting("tmdb_api_key", DB_FILE):
            return api_error("请先保存 TMDB API Key", "missing_tmdb_api_key", 400)
        server_id = int(data["server_id"])
        library_id = int(data["library_id"])
        options = data.get("options") or {}
        with connect() as db:
            server = db.execute("SELECT id FROM servers WHERE id = ? AND enabled = 1", (server_id,)).fetchone()
        if server is None:
            return api_error("服务器不存在或已禁用", "server_not_found", 404)
        job_id = manager.create_job(
            "episode_audit",
            server_id,
            {"library_id": library_id, "options": options},
        )
        return jsonify({"id": job_id})

    @tools_bp.get("/api/episode-audits")
    @login_required
    def list_episode_audits():
        with connect() as db:
            rows = db.execute(
                """
                SELECT episode_audit_runs.*, servers.name AS server_name, jobs.status AS job_status
                FROM episode_audit_runs
                JOIN servers ON servers.id = episode_audit_runs.server_id
                LEFT JOIN jobs ON jobs.id = episode_audit_runs.job_id
                ORDER BY episode_audit_runs.id DESC LIMIT 50
                """
            ).fetchall()
        runs = rows_to_dicts(rows)
        for run in runs:
            run["options"] = decode_payload(run.get("options", "{}"))
        return jsonify(runs)

    @tools_bp.get("/api/episode-audits/<int:run_id>")
    @login_required
    def get_episode_audit(run_id: int):
        with connect() as db:
            row = db.execute(
                """
                SELECT episode_audit_runs.*, servers.name AS server_name, jobs.status AS job_status
                FROM episode_audit_runs
                JOIN servers ON servers.id = episode_audit_runs.server_id
                LEFT JOIN jobs ON jobs.id = episode_audit_runs.job_id
                WHERE episode_audit_runs.id = ?
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return api_error("缺集检查记录不存在", "episode_audit_not_found", 404)
        data = dict(row)
        data["options"] = decode_payload(data.get("options", "{}"))
        return jsonify(data)

    @tools_bp.get("/api/episode-audits/<int:run_id>/items")
    @login_required
    def list_episode_audit_items(run_id: int):
        status = (request.args.get("status") or "").strip()
        q = f"%{(request.args.get('q') or '').strip()}%"
        clauses = ["run_id = ?"]
        params: list[Any] = [run_id]
        if status:
            clauses.append("status = ?")
            params.append(status)
        if q != "%%":
            clauses.append("(show_title LIKE ? OR tmdb_title LIKE ?)")
            params.extend([q, q])
        with connect() as db:
            rows = db.execute(
                f"SELECT * FROM episode_audit_items WHERE {' AND '.join(clauses)} ORDER BY show_title ASC, season ASC, episode ASC, id ASC",
                params,
            ).fetchall()
        items = rows_to_dicts(rows)
        for item in items:
            item["details"] = decode_payload(item.get("details", "{}"))
        return jsonify(items)

    @tools_bp.get("/api/episode-audits/<int:run_id>/report")
    @login_required
    def get_episode_audit_report(run_id: int):
        with connect() as db:
            run = db.execute(
                """
                SELECT episode_audit_runs.*, servers.name AS server_name
                FROM episode_audit_runs JOIN servers ON servers.id = episode_audit_runs.server_id
                WHERE episode_audit_runs.id = ?
                """,
                (run_id,),
            ).fetchone()
            rows = db.execute(
                """
                SELECT * FROM episode_audit_items
                WHERE run_id = ?
                ORDER BY show_title ASC, season ASC, episode ASC, id ASC
                """,
                (run_id,),
            ).fetchall()
        if run is None:
            return api_error("缺集检查记录不存在", "episode_audit_not_found", 404)
        groups: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            item["details"] = decode_payload(item.get("details", "{}"))
            key = item.get("plex_rating_key") or str(item.get("tmdb_id") or "") or item.get("show_title") or str(item["id"])
            group = groups.setdefault(
                key,
                {
                    "show_title": item.get("show_title") or "",
                    "plex_rating_key": item.get("plex_rating_key") or "",
                    "tmdb_id": item.get("tmdb_id"),
                    "tmdb_title": item.get("tmdb_title") or "",
                    "match_source": item.get("match_source") or "",
                    "representative_item_id": item.get("id"),
                    "missing_count": 0,
                    "present_count": 0,
                    "ignored_count": 0,
                    "unmatched": False,
                    "ambiguous": False,
                    "candidates": [],
                    "legacy_summary_only": True,
                    "episodes": [],
                    "ignored": [],
                    "seasons": {},
                },
            )
            status_value = item.get("status")
            formatted = format_episode_item(item)
            if status_value in {"present", "missing", "ignored_missing"}:
                group["legacy_summary_only"] = False
                season_key = str(formatted["season"])
                season = group["seasons"].setdefault(season_key, {"season": formatted["season"], "episodes": []})
                season["episodes"].append(formatted)
            if status_value == "missing":
                group["missing_count"] += 1
                group["episodes"].append(formatted)
            elif status_value == "present":
                group["present_count"] += 1
            elif status_value == "ignored_missing":
                group["ignored_count"] += 1
                group["ignored"].append(formatted)
            elif status_value == "ignored_show":
                group["ignored_count"] += 1
                group["ignored"].append(formatted)
            elif status_value == "unmatched_show":
                group["unmatched"] = True
            elif status_value == "ambiguous_match":
                group["ambiguous"] = True
                group["candidates"] = item["details"].get("candidates") or []
        report_groups = []
        for group in groups.values():
            group["seasons"] = sorted(group["seasons"].values(), key=lambda season: int(season["season"] or 0))
            for season in group["seasons"]:
                season["episodes"].sort(key=lambda episode: int(episode["episode"] or 0))
            if group["missing_count"] or group["present_count"] or group["ignored_count"] or group["unmatched"] or group["ambiguous"]:
                report_groups.append(group)
        report_groups.sort(key=lambda group: (0 if group["missing_count"] else 1, group["show_title"].lower()))
        run_data = dict(run)
        run_data["options"] = decode_payload(run_data.get("options", "{}"))
        legacy_summary_only = bool(report_groups) and not any(not group["legacy_summary_only"] for group in report_groups)
        return jsonify(
            {
                "run": run_data,
                "summary": {
                    "shows_with_missing": sum(1 for group in report_groups if group["missing_count"]),
                    "missing_episodes": sum(group["missing_count"] for group in report_groups),
                    "present_episodes": sum(group["present_count"] for group in report_groups),
                    "ignored_items": sum(group["ignored_count"] for group in report_groups),
                    "unmatched_shows": sum(1 for group in report_groups if group["unmatched"]),
                    "ambiguous_shows": sum(1 for group in report_groups if group["ambiguous"]),
                    "legacy_summary_only": legacy_summary_only,
                },
                "groups": report_groups,
            }
        )

    def format_episode_item(item: dict[str, Any]) -> dict[str, Any]:
        details = item.get("details") or {}
        return {
            "id": item.get("id"),
            "season": item.get("season"),
            "episode": item.get("episode"),
            "label": f"S{int(item['season']):02d}E{int(item['episode']):02d}" if item.get("season") is not None and item.get("episode") is not None else "整部剧",
            "air_date": item.get("air_date") or "",
            "title": details.get("episode_title") or "",
            "reason": details.get("ignore_reason") or details.get("reason") or "",
            "status": item.get("status") or "",
            "item_id": item.get("id"),
        }

    @tools_bp.get("/api/episode-audit-ignores")
    @login_required
    def list_episode_audit_ignores():
        server_id = request.args.get("server_id")
        library_id = request.args.get("library_id")
        clauses = ["1 = 1"]
        params: list[Any] = []
        if server_id:
            clauses.append("server_id = ?")
            params.append(int(server_id))
        if library_id:
            clauses.append("library_id = ?")
            params.append(int(library_id))
        with connect() as db:
            rows = db.execute(
                f"SELECT * FROM episode_audit_ignores WHERE {' AND '.join(clauses)} ORDER BY show_title ASC, season ASC, episode ASC",
                params,
            ).fetchall()
        return jsonify(rows_to_dicts(rows))

    @tools_bp.post("/api/episode-audit-ignores")
    @login_required
    def create_episode_audit_ignore():
        data = request.get_json(force=True)
        ignore_id = save_episode_ignore(data)
        return jsonify({"id": ignore_id})

    @tools_bp.post("/api/episode-audit-ignores/from-item")
    @login_required
    def create_episode_audit_ignore_from_item():
        data = request.get_json(force=True)
        item_id = int(data["item_id"])
        scope = data.get("scope") or "episode"
        reason = (data.get("reason") or "").strip()
        with connect() as db:
            row = db.execute(
                """
                SELECT episode_audit_items.*, episode_audit_runs.server_id, episode_audit_runs.library_id
                FROM episode_audit_items
                JOIN episode_audit_runs ON episode_audit_runs.id = episode_audit_items.run_id
                WHERE episode_audit_items.id = ?
                """,
                (item_id,),
            ).fetchone()
        if row is None:
            return api_error("缺集结果不存在", "episode_audit_item_not_found", 404)
        item = dict(row)
        payload = {
            "server_id": item["server_id"],
            "library_id": item["library_id"],
            "show_title": item.get("show_title") or "",
            "plex_rating_key": item.get("plex_rating_key") or "",
            "tmdb_id": item.get("tmdb_id"),
            "season": None if scope == "show" else item.get("season"),
            "episode": None if scope == "show" else item.get("episode"),
            "reason": reason,
        }
        ignore_id = save_episode_ignore(payload)
        mark_episode_items_ignored(item, scope, reason)
        return jsonify({"id": ignore_id})

    @tools_bp.delete("/api/episode-audit-ignores/<int:ignore_id>")
    @login_required
    def delete_episode_audit_ignore(ignore_id: int):
        with connect() as db:
            db.execute("DELETE FROM episode_audit_ignores WHERE id = ?", (ignore_id,))
            db.commit()
        return jsonify({"ok": True})

    def save_episode_ignore(data: dict[str, Any]) -> int:
        server_id = int(data["server_id"])
        library_id = int(data["library_id"])
        show_title = (data.get("show_title") or "").strip()
        plex_rating_key = str(data.get("plex_rating_key") or "")
        tmdb_id = data.get("tmdb_id")
        tmdb_id = int(str(tmdb_id)) if tmdb_id not in (None, "") else None
        season = data.get("season")
        episode = data.get("episode")
        season = int(str(season)) if season not in (None, "") else None
        episode = int(str(episode)) if episode not in (None, "") else None
        reason = (data.get("reason") or "").strip()
        now = utcnow()
        with connect() as db:
            db.execute(
                """
                DELETE FROM episode_audit_ignores
                WHERE server_id = ? AND library_id = ?
                  AND plex_rating_key = ?
                  AND IFNULL(tmdb_id, -1) = IFNULL(?, -1)
                  AND IFNULL(season, -1) = IFNULL(?, -1)
                  AND IFNULL(episode, -1) = IFNULL(?, -1)
                """,
                (server_id, library_id, plex_rating_key, tmdb_id, season, episode),
            )
            cur = db.execute(
                """
                INSERT INTO episode_audit_ignores (
                    server_id, library_id, show_title, plex_rating_key, tmdb_id, season, episode, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (server_id, library_id, show_title, plex_rating_key, tmdb_id, season, episode, reason, now),
            )
            db.commit()
            return int(cur.lastrowid or 0)

    def mark_episode_items_ignored(item: dict[str, Any], scope: str, reason: str) -> None:
        status_value = "ignored_show" if scope == "show" else "ignored_missing"
        clauses = ["run_id = ?", "status IN ('present', 'missing', 'unmatched_show', 'ambiguous_match')"]
        params: list[Any] = [item["run_id"]]
        if scope == "show":
            clauses.append("(plex_rating_key = ? OR (tmdb_id IS NOT NULL AND tmdb_id = ?) OR show_title = ?)")
            params.extend([item.get("plex_rating_key") or "", item.get("tmdb_id"), item.get("show_title") or ""])
        else:
            clauses.append("id = ?")
            params.append(item["id"])
        with connect() as db:
            rows = db.execute(f"SELECT id, details FROM episode_audit_items WHERE {' AND '.join(clauses)}", params).fetchall()
            for row in rows:
                details = decode_payload(row["details"])
                details["ignore_reason"] = reason
                db.execute("UPDATE episode_audit_items SET status = ?, details = ? WHERE id = ?", (status_value, json.dumps(details, ensure_ascii=False), row["id"]))
            refresh_episode_audit_counts(db, item["run_id"])
            db.commit()

    def refresh_episode_audit_counts(db: Any, run_id: int) -> None:
        db.execute(
            """
            UPDATE episode_audit_runs
            SET missing_count = (
                SELECT COUNT(*) FROM episode_audit_items
                WHERE run_id = ? AND status = 'missing'
            ),
            unmatched_count = (
                SELECT COUNT(*) FROM episode_audit_items
                WHERE run_id = ? AND status = 'unmatched_show'
            ),
            ambiguous_count = (
                SELECT COUNT(*) FROM episode_audit_items
                WHERE run_id = ? AND status = 'ambiguous_match'
            ),
            ignored_count = (
                SELECT COUNT(*) FROM episode_audit_items
                WHERE run_id = ? AND status IN ('ignored_missing', 'ignored_show')
            )
            WHERE id = ?
            """,
            (run_id, run_id, run_id, run_id, run_id),
        )

    @tools_bp.get("/api/collection-rules")
    @login_required
    def list_collection_rules():
        with connect() as db:
            rows = db.execute(
                """
                SELECT collection_rules.*, servers.name AS server_name
                FROM collection_rules JOIN servers ON servers.id = collection_rules.server_id
                ORDER BY collection_rules.id DESC
                """
            ).fetchall()
        return jsonify(rows_to_dicts(rows))

    @tools_bp.post("/api/collection-rules")
    @login_required
    def create_collection_rule():
        data = request.get_json(force=True)
        now = utcnow()
        with connect() as db:
            cur = db.execute(
                """
                INSERT INTO collection_rules (
                    server_id, library_id, name, match_field, match_value, collection_title,
                    title_sort_mode, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(data["server_id"]),
                    int(data["library_id"]),
                    data.get("name") or "合集规则",
                    data.get("match_field") or "title",
                    data.get("match_value") or "",
                    data.get("collection_title") or "",
                    data.get("title_sort_mode") or "pinyin",
                    1 if data.get("enabled", True) else 0,
                    now,
                    now,
                ),
            )
            db.commit()
        return jsonify({"id": int(cur.lastrowid or 0)})

    @tools_bp.put("/api/collection-rules/<int:rule_id>")
    @login_required
    def update_collection_rule(rule_id: int):
        data = request.get_json(force=True)
        with connect() as db:
            db.execute(
                """
                UPDATE collection_rules
                SET server_id = ?, library_id = ?, name = ?, match_field = ?, match_value = ?,
                    collection_title = ?, title_sort_mode = ?, enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    int(data["server_id"]),
                    int(data["library_id"]),
                    data.get("name") or "合集规则",
                    data.get("match_field") or "title",
                    data.get("match_value") or "",
                    data.get("collection_title") or "",
                    data.get("title_sort_mode") or "pinyin",
                    1 if data.get("enabled", True) else 0,
                    utcnow(),
                    rule_id,
                ),
            )
            db.commit()
        return jsonify({"ok": True})

    @tools_bp.delete("/api/collection-rules/<int:rule_id>")
    @login_required
    def delete_collection_rule(rule_id: int):
        with connect() as db:
            db.execute("DELETE FROM collection_rules WHERE id = ?", (rule_id,))
            db.commit()
        return jsonify({"ok": True})

    @tools_bp.post("/api/collection-rules/<int:rule_id>/preview")
    @login_required
    def preview_collection_rule(rule_id: int):
        with connect() as db:
            rule = db.execute("SELECT server_id FROM collection_rules WHERE id = ?", (rule_id,)).fetchone()
        if rule is None:
            return api_error("合集规则不存在", "collection_rule_not_found", 404)
        job_id = manager.create_job(
            "collection_rule", int(rule["server_id"]), {"rule_id": rule_id, "preview": True}
        )
        return jsonify({"id": job_id})

    @tools_bp.post("/api/collection-rules/<int:rule_id>/run")
    @login_required
    def run_collection_rule(rule_id: int):
        data = request.get_json(silent=True) or {}
        with connect() as db:
            rule = db.execute("SELECT server_id FROM collection_rules WHERE id = ?", (rule_id,)).fetchone()
        if rule is None:
            return api_error("合集规则不存在", "collection_rule_not_found", 404)
        job_id = manager.create_job("collection_rule", int(rule["server_id"]), {"rule_id": rule_id, "preview": bool(data.get("preview"))})
        return jsonify({"id": job_id})

    @automation_bp.get("/api/webhook-events")
    @login_required
    def list_webhook_events():
        with connect() as db:
            rows = db.execute(
                """
                SELECT webhook_events.*, servers.name AS server_name
                FROM webhook_events JOIN servers ON servers.id = webhook_events.server_id
                ORDER BY webhook_events.id DESC LIMIT 100
                """
            ).fetchall()
        events = rows_to_dicts(rows)
        for event in events:
            event["payload"] = decode_payload(event.get("payload", "{}"))
        return jsonify(events)

    @automation_bp.get("/api/webhook-rules")
    @login_required
    def list_webhook_rules():
        with connect() as db:
            rows = db.execute("SELECT * FROM webhook_rules ORDER BY id DESC").fetchall()
        return jsonify(rows_to_dicts(rows))

    @automation_bp.post("/api/webhook-rules")
    @login_required
    def save_webhook_rule():
        data = request.get_json(force=True)
        now = utcnow()
        with connect() as db:
            rule_id = data.get("id")
            if rule_id:
                db.execute(
                    "UPDATE webhook_rules SET server_id = ?, event = ?, action = ?, enabled = ?, updated_at = ? WHERE id = ?",
                    (int(data["server_id"]), data.get("event") or "library.new", data.get("action") or "localize", 1 if data.get("enabled", True) else 0, now, int(rule_id)),
                )
            else:
                db.execute(
                    "INSERT INTO webhook_rules (server_id, event, action, enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (int(data["server_id"]), data.get("event") or "library.new", data.get("action") or "localize", 1 if data.get("enabled", True) else 0, now, now),
                )
            db.commit()
        return jsonify({"ok": True})

    @automation_bp.delete("/api/webhook-rules/<int:rule_id>")
    @login_required
    def delete_webhook_rule(rule_id: int):
        with connect() as db:
            db.execute("DELETE FROM webhook_rules WHERE id = ?", (rule_id,))
            db.commit()
        return jsonify({"ok": True})

    @automation_bp.get("/api/notifications/channels")
    @login_required
    def list_notification_channels():
        with connect() as db:
            rows = db.execute("SELECT * FROM notification_channels ORDER BY id DESC").fetchall()
        channels = rows_to_dicts(rows)
        for channel in channels:
            channel["events"] = json.loads(channel.get("events") or "[]")
        return jsonify(channels)

    @automation_bp.post("/api/notifications/channels")
    @login_required
    def save_notification_channel():
        data = request.get_json(force=True)
        now = utcnow()
        events = json.dumps(data.get("events") or [], ensure_ascii=False)
        with connect() as db:
            channel_id = data.get("id")
            if channel_id:
                db.execute(
                    "UPDATE notification_channels SET name = ?, channel_type = ?, url = ?, events = ?, enabled = ?, updated_at = ? WHERE id = ?",
                    (data.get("name") or "Webhook", data.get("channel_type") or "webhook", data.get("url") or "", events, 1 if data.get("enabled", True) else 0, now, int(channel_id)),
                )
            else:
                db.execute(
                    "INSERT INTO notification_channels (name, channel_type, url, events, enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (data.get("name") or "Webhook", data.get("channel_type") or "webhook", data.get("url") or "", events, 1 if data.get("enabled", True) else 0, now, now),
                )
            db.commit()
        return jsonify({"ok": True})

    @automation_bp.delete("/api/notifications/channels/<int:channel_id>")
    @login_required
    def delete_notification_channel(channel_id: int):
        with connect() as db:
            db.execute("DELETE FROM notification_channels WHERE id = ?", (channel_id,))
            db.commit()
        return jsonify({"ok": True})

    @automation_bp.post("/api/notifications/channels/<int:channel_id>/test")
    @login_required
    def test_notification_channel(channel_id: int):
        with connect() as db:
            row = db.execute("SELECT * FROM notification_channels WHERE id = ?", (channel_id,)).fetchone()
        if row is None:
            return api_error("通知渠道不存在", "notification_channel_not_found", 404)
        try:
            import requests

            requests.post(
                row["url"],
                json={"event": "notification_test", "payload": {"message": "CLP 通知测试"}},
                timeout=10,
            ).raise_for_status()
        except Exception as exc:
            return api_error(str(exc), "notification_test_failed", 400)
        return jsonify({"ok": True})

    @automation_bp.post("/webhook/<int:server_id>/<secret>")
    def webhook(server_id: int, secret: str):
        with connect() as db:
            server = db.execute(
                "SELECT id, webhook_secret FROM servers WHERE id = ? AND enabled = 1",
                (server_id,),
            ).fetchone()
        if server is None or not hmac.compare_digest(server["webhook_secret"], secret):
            return api_error("Webhook 地址无效", "webhook_not_found", 404)
        source_ip = client_ip()
        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
        with connect() as db:
            db.execute("DELETE FROM webhook_rate_limits WHERE created_at < ?", (cutoff,))
            count = db.execute(
                """
                SELECT COUNT(*) AS count FROM webhook_rate_limits
                WHERE server_id = ? AND source_ip = ? AND created_at >= ?
                """,
                (server_id, source_ip, cutoff),
            ).fetchone()["count"]
            if count >= 60:
                db.commit()
                return api_error("Webhook 请求过于频繁", "webhook_rate_limited", 429)
            db.execute(
                "INSERT INTO webhook_rate_limits (server_id, source_ip, created_at) VALUES (?, ?, ?)",
                (server_id, source_ip, utcnow()),
            )
            db.commit()
        payload = request.form.get("payload")
        if not payload:
            return api_error("Webhook 缺少 payload", "missing_webhook_payload", 400)
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return api_error("Webhook payload 不是有效 JSON", "invalid_webhook_payload", 400)
        if not isinstance(data, dict):
            return api_error("Webhook payload 必须是 JSON 对象", "invalid_webhook_payload", 400)
        event_name = str(data.get("event") or "unknown")[:100]
        raw_metadata = data.get("Metadata") or {}
        if not isinstance(raw_metadata, dict):
            raw_metadata = {}
        allowed_keys = {
            "ratingKey",
            "librarySectionID",
            "type",
            "title",
            "guid",
            "grandparentRatingKey",
            "parentRatingKey",
        }
        metadata = {key: raw_metadata[key] for key in allowed_keys if key in raw_metadata}
        collections = raw_metadata.get("Collection") or []
        if isinstance(collections, list):
            metadata["Collection"] = [
                {"guid": str(item.get("guid"))[:500]}
                for item in collections[:100]
                if isinstance(item, dict) and item.get("guid")
            ]
        normalized = {"event": event_name, "Metadata": metadata}
        summary = str(metadata.get("title") or metadata.get("ratingKey") or event_name)[:500]
        with connect() as db:
            cur = db.execute(
                """
                INSERT INTO webhook_events (
                    server_id, event, summary, payload, status, source_ip, created_at
                ) VALUES (?, ?, ?, ?, 'received', ?, ?)
                """,
                (
                    server_id,
                    event_name,
                    summary,
                    json.dumps(normalized, ensure_ascii=False),
                    source_ip,
                    utcnow(),
                ),
            )
            event_id = int(cur.lastrowid or 0)
            rules = db.execute(
                "SELECT * FROM webhook_rules WHERE server_id = ? AND event = ? AND enabled = 1",
                (server_id, event_name),
            ).fetchall()
            db.commit()
        should_localize = (not rules and event_name == "library.new") or any(
            rule["action"] == "localize" for rule in rules
        )
        if should_localize and metadata:
            manager.create_job(
                "webhook", server_id, {"metadata": metadata, "webhook_event_id": event_id}
            )
        if any(rule["action"] == "notify" for rule in rules):
            manager.create_job(
                "notification_event",
                server_id,
                {"event": event_name, "summary": summary, "webhook_event_id": event_id},
            )
        return "OK", 200

    app.register_blueprint(auth_bp)
    app.register_blueprint(servers_bp)
    app.register_blueprint(jobs_bp)
    app.register_blueprint(automation_bp)
    app.register_blueprint(tools_bp)
    return app
