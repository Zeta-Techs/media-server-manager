"""Authentication, setup and user preference routes."""

from __future__ import annotations

import json
from functools import wraps
from typing import Any, Callable, cast

from flask import Blueprint, current_app, jsonify, render_template, request, session

from ...application.auth.service import AuthService
from ...infrastructure.db.runtime import DEFAULT_USERNAME
from ...infrastructure.security.passwords import hash_password, new_csrf_token, verify_password
from ..dependencies import get_database, get_database_path


def _ctx() -> Any:
    return current_app.extensions["route_context"]


def _db_file():
    return get_database_path()


def _error(message: str, code: str, status: int):
    return jsonify({"error": message, "code": code}), status


def _login_required(view: Callable[..., Any]):
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any):
        return _ctx().login_required(view)(*args, **kwargs)

    return wrapped


def _client_ip() -> str:
    return request.remote_addr or "unknown"


def index():
    return render_template("index.html", initialized=_ctx().has_user())


def media_library_detail_page():
    return render_template("index.html", initialized=_ctx().has_user())


def api_setup():
    data = request.get_json(force=True)
    username = (data.get("username") or "admin").strip()
    password = data.get("password") or ""
    if not username or len(username) > 64:
        return _error("用户名不能为空且不能超过 64 个字符", "invalid_username", 400)
    if len(password) < 10 or len(password) > 128:
        return _error("密码长度需要在 10 到 128 位之间", "invalid_password", 400)
    with get_database().transaction(immediate=True) as uow:
        auth = AuthService(uow.session)
        if auth.has_users():
            return _error("应用已经初始化", "already_initialized", 409)
        user = auth.create_user(username, hash_password(password))
    session.clear()
    session["user_id"] = int(cast(Any, user).id)
    session["csrf_token"] = new_csrf_token()
    return jsonify({"ok": True, "csrf_token": session["csrf_token"]})


def api_login():
    data = request.get_json(force=True)
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    with get_database().transaction() as uow:
        user, limited = AuthService(uow.session).authenticate(
            username, _client_ip(), password, verify_password
        )
        if limited:
            return _error("登录失败次数过多，请 10 分钟后重试", "login_rate_limited", 429)
        if user is None:
            return _error("用户名或密码错误", "invalid_credentials", 401)
    session.clear()
    session["user_id"] = int(cast(Any, user).id)
    session["csrf_token"] = new_csrf_token()
    return jsonify({"ok": True, "csrf_token": session["csrf_token"]})


def api_logout():
    session.clear()
    session["csrf_token"] = new_csrf_token()
    return jsonify({"ok": True, "csrf_token": session["csrf_token"]})


@_login_required
def api_change_password():
    data = request.get_json(force=True)
    current_password = data.get("current_password") or ""
    new_password = data.get("new_password") or ""
    if len(new_password) < 10 or len(new_password) > 128:
        return _error("新密码长度需要在 10 到 128 位之间", "invalid_password", 400)
    with get_database().transaction() as uow:
        changed = AuthService(uow.session).change_password(
            int(session["user_id"]), current_password, new_password, verify_password, hash_password
        )
        if not changed:
            return _error("当前密码错误", "invalid_credentials", 401)
    return jsonify({"ok": True})


def api_session():
    csrf_token = session.setdefault("csrf_token", new_csrf_token())
    return jsonify(
        {
            "initialized": _ctx().has_user(),
            "authenticated": _ctx().current_user_exists(),
            "default_username": DEFAULT_USERNAME,
            "csrf_token": csrf_token,
        }
    )


@_login_required
def api_preferences():
    with get_database().session() as db:
        rows = AuthService(db).preferences(int(session["user_id"]))
    result: dict[str, Any] = {}
    for row in rows:
        try:
            result[row.key] = json.loads(row.value)
        except (TypeError, json.JSONDecodeError):
            result[row.key] = row.value
    return jsonify(result)


@_login_required
def update_preferences():
    data = request.get_json(force=True)
    if not isinstance(data, dict) or any(not isinstance(key, str) or len(key) > 64 for key in data):
        return _error("用户偏好格式无效", "invalid_preferences", 400)
    with get_database().transaction() as uow:
        auth = AuthService(uow.session)
        for key, value in data.items():
            encoded = json.dumps(value, ensure_ascii=False)
            if len(encoded) > 512 * 1024:
                return _error("用户偏好内容过大", "invalid_preferences", 400)
            auth.set_preference(int(session["user_id"]), key, encoded)
    return jsonify({"ok": True})


def create_auth_blueprint() -> Blueprint:
    blueprint = Blueprint("auth", __name__)
    blueprint.add_url_rule("/", view_func=index, methods=["GET"])
    blueprint.add_url_rule("/media-library/detail", view_func=media_library_detail_page, methods=["GET"])
    blueprint.add_url_rule("/api/setup", view_func=api_setup, methods=["POST"])
    blueprint.add_url_rule("/api/auth/login", view_func=api_login, methods=["POST"])
    blueprint.add_url_rule("/api/auth/logout", view_func=api_logout, methods=["POST"])
    blueprint.add_url_rule("/api/auth/password", view_func=api_change_password, methods=["PUT"])
    blueprint.add_url_rule("/api/session", view_func=api_session, methods=["GET"])
    blueprint.add_url_rule("/api/preferences", view_func=api_preferences, methods=["GET"])
    blueprint.add_url_rule("/api/preferences", view_func=update_preferences, methods=["PUT"])
    return blueprint


