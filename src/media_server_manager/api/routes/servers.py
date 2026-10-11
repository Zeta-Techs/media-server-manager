"""Plex server management HTTP routes.

The CRUD and secret management endpoints live here.  Less frequently used
Plex library endpoints still use the compatibility adapter until their
application services are extracted.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Callable

from flask import Blueprint, current_app, jsonify, render_template, request, session, url_for

from ...application.server_management.service import ServerManagementService
from ...core import join_skip_libraries
from ...infrastructure.db.repositories.auth import AuthRepository
from ...infrastructure.db.runtime import new_secret
from ...infrastructure.integrations.plex.auth import check_pin, create_pin
from ..dependencies import get_database, get_database_path


def _error(message: str, code: str, status: int):
    return jsonify({"error": message, "code": code}), status


def _ctx() -> Any:
    return current_app.extensions["route_context"]


def _db_file():
    return get_database_path()


def _validate_address(value: str) -> str:
    return _ctx().validate_address(value)


def _serialize(server: Any) -> dict[str, Any]:
    data = {
        "id": server.id,
        "name": server.name,
        "address": server.address,
        "skip_libraries": server.skip_libraries,
        "pinyin_mode": server.pinyin_mode,
        "auth_source": server.auth_source,
        "enabled": server.enabled,
        "created_at": server.created_at,
        "updated_at": server.updated_at,
        "token_configured": bool(server.token),
        "webhook_url": url_for(
            "webhooks.webhook", server_id=server.id, secret=server.webhook_secret, _external=True
        ),
    }
    return data


def _login_required(view: Callable[..., Any]):
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any):
        return _ctx().login_required(view)(*args, **kwargs)

    return wrapped


@_login_required
def list_servers():
    with get_database().session() as session:
        rows = ServerManagementService(session).all_servers()
        return jsonify([_serialize(server) for server in rows])


@_login_required
def create_server():
    data = request.get_json(force=True)
    name = (data.get("name") or "Plex Server").strip()
    try:
        address = _validate_address(data.get("address") or "")
    except ValueError as exc:
        return _error(str(exc), "invalid_server_address", 400)
    token = (data.get("token") or "").strip()
    pinyin_mode = data.get("pinyin_mode") or "first_letter"
    if not token:
        return _error("Token 不能为空", "missing_token", 400)
    if pinyin_mode not in {"first_letter", "full_spell"}:
        return _error("拼音模式无效", "invalid_pinyin_mode", 400)
    skip_libraries = data.get("skip_libraries") or []
    skip_value = skip_libraries if isinstance(skip_libraries, str) else join_skip_libraries(skip_libraries)
    with get_database().transaction() as uow:
        server = ServerManagementService(uow.session).create_server(
            name=name,
            address=address,
            token=token,
            skip_libraries=skip_value,
            pinyin_mode=pinyin_mode,
            auth_source=data.get("auth_source") or "manual",
            webhook_secret=new_secret(),
            enabled=bool(data.get("enabled", True)),
        )
        server_id = int(server.id)
    return jsonify({"id": server_id})


@_login_required
def update_server(server_id: int):
    data = request.get_json(force=True)
    skip_libraries = data.get("skip_libraries") or []
    skip_value = skip_libraries if isinstance(skip_libraries, str) else join_skip_libraries(skip_libraries)
    with get_database().transaction() as uow:
        service = ServerManagementService(uow.session)
        existing = service.get_server(server_id)
        if existing is None:
            return _error("服务器不存在", "server_not_found", 404)
        try:
            address = _validate_address(data.get("address") or existing.address)
        except ValueError as exc:
            return _error(str(exc), "invalid_server_address", 400)
        token = (data.get("token") or "").strip() or existing.token
        pinyin_mode = data.get("pinyin_mode") or "first_letter"
        if pinyin_mode not in {"first_letter", "full_spell"}:
            return _error("拼音模式无效", "invalid_pinyin_mode", 400)
        service.update_server(
            existing,
            name=(data.get("name") or "Plex Server").strip(),
            address=address,
            token=token,
            skip_libraries=skip_value,
            pinyin_mode=pinyin_mode,
            enabled=bool(data.get("enabled", True)),
        )
    return jsonify({"ok": True})


@_login_required
def delete_server(server_id: int):
    with get_database().transaction() as uow:
        deleted = ServerManagementService(uow.session).delete_server(server_id)
    if not deleted:
        return _error("服务器不存在", "server_not_found", 404)
    return jsonify({"ok": True})


@_login_required
def rotate_webhook_secret(server_id: int):
    secret = new_secret()
    with get_database().transaction() as uow:
        service = ServerManagementService(uow.session)
        server = service.get_server(server_id)
        if server is None:
            return _error("服务器不存在", "server_not_found", 404)
        service.update_server(server, webhook_secret=secret)
    return jsonify(
        {
            "ok": True,
            "webhook_url": url_for("webhooks.webhook", server_id=server_id, secret=secret, _external=True),
        }
    )


@_login_required
def plex_oauth_start():
    forward_url = url_for("servers.plex_oauth_callback", _external=True)
    result = create_pin(forward_url)
    flow_id = secrets.token_urlsafe(24)
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    with get_database().transaction() as uow:
        repository = AuthRepository(uow.session)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        repository.purge_oauth_flows(now)
        repository.create_oauth_flow(flow_id, int(session["user_id"]), int(result["pin_id"]), expires_at, now)
    return jsonify({"flow_id": flow_id, "code": result["code"], "auth_url": result["auth_url"]})


def plex_oauth_callback():
    return render_template("plex_oauth_callback.html")


@_login_required
def plex_oauth_status(flow_id: str):
    try:
        with get_database().session() as session_db:
            flow = AuthRepository(session_db).oauth_flow(flow_id, int(session["user_id"]))
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        if flow is None or str(flow["expires_at"]) < now:
            return _error("授权请求不存在或已过期", "oauth_flow_expired", 404)
        if flow["token"]:
            resources = json.loads(str(flow["resources"] or "[]"))
        else:
            result = check_pin(int(str(flow["pin_id"])))
            if not result.get("claimed"):
                return jsonify({"claimed": False, "flow_id": flow_id})
            token = str(result["auth_token"])
            resources = []
            for index, resource in enumerate(result.get("servers") or []):
                clean = {key: value for key, value in resource.items() if key != "token"}
                clean["resource_index"] = index
                resources.append(clean)
            with get_database().transaction() as uow:
                AuthRepository(uow.session).update_oauth_flow(
                    flow_id, token, json.dumps(resources, ensure_ascii=False)
                )
        return jsonify({"claimed": True, "flow_id": flow_id, "servers": resources})
    except Exception as exc:
        return jsonify({"claimed": False, "error": str(exc), "code": "oauth_failed"}), 400


@_login_required
def plex_oauth_import_server():
    data = request.get_json(force=True)
    flow_id = data.get("flow_id") or ""
    resource_index = int(data.get("resource_index", -1))
    with get_database().session() as session_db:
        flow = AuthRepository(session_db).oauth_flow(flow_id, int(session["user_id"]))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    if flow is None or str(flow["expires_at"]) < now or not flow["token"]:
        return _error("OAuth 授权结果不存在或已过期", "oauth_flow_expired", 404)
    resources = json.loads(str(flow["resources"] or "[]"))
    if resource_index < 0 or resource_index >= len(resources):
        return _error("OAuth 服务器选择无效", "invalid_oauth_resource", 400)
    resource = resources[resource_index]
    name = (resource.get("name") or "Plex Server").strip()
    allowed_connections = resource.get("connections") or []
    requested_uri = (data.get("connection_uri") or "").rstrip("/")
    connection = next(
        (item for item in allowed_connections if item.get("uri") == requested_uri),
        resource.get("best_connection") or (allowed_connections[0] if allowed_connections else {}),
    )
    try:
        address = _validate_address(connection.get("uri") or "")
    except ValueError as exc:
        return _error(str(exc), "invalid_server_address", 400)
    with get_database().transaction() as uow:
        service = ServerManagementService(uow.session)
        existing = service.repository.by_address(address)
        if existing is None:
            server = service.create_server(
                name=name,
                address=address,
                token=str(flow["token"]),
                auth_source="plex_oauth",
                webhook_secret=new_secret(),
            )
        else:
            server = service.update_server(
                existing,
                name=name,
                token=str(flow["token"]),
                auth_source="plex_oauth",
                enabled=True,
            )
        server_id = int(server.id)
    return jsonify({"id": server_id})


def create_servers_blueprint() -> Blueprint:
    """Create a fresh blueprint for every Flask application instance."""
    blueprint = Blueprint("servers", __name__)
    blueprint.add_url_rule("/api/servers", view_func=list_servers, methods=["GET"])
    blueprint.add_url_rule("/api/servers", view_func=create_server, methods=["POST"])
    blueprint.add_url_rule("/api/servers/<int:server_id>", view_func=update_server, methods=["PUT"])
    blueprint.add_url_rule("/api/servers/<int:server_id>", view_func=delete_server, methods=["DELETE"])
    blueprint.add_url_rule(
        "/api/servers/<int:server_id>/webhook-secret/rotate",
        view_func=rotate_webhook_secret,
        methods=["POST"],
    )
    blueprint.add_url_rule("/api/plex/oauth/start", view_func=plex_oauth_start, methods=["POST"])
    blueprint.add_url_rule("/plex/oauth/callback", view_func=plex_oauth_callback, methods=["GET"])
    blueprint.add_url_rule("/api/plex/oauth/status/<flow_id>", view_func=plex_oauth_status, methods=["GET"])
    blueprint.add_url_rule("/api/plex/oauth/import-server", view_func=plex_oauth_import_server, methods=["POST"])
    return blueprint


