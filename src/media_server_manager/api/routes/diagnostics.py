"""Diagnostics route helpers shared by the Flask compatibility adapter.

The HTTP decorators remain in the compatibility application while route
extraction is in progress.  This module keeps response assembly independent
from SQLAlchemy and provides the first fully extracted route boundary.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Blueprint, current_app, jsonify, url_for
from sqlalchemy.orm import Session

from ...application.diagnostics.service import DiagnosticsService
from ...infrastructure.db.repositories.jobs import JobRepository
from ...infrastructure.integrations.plex.client import PlexServer
from ..dependencies import get_database, get_database_path


def _decode_payload(value: object) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _job_payload(job: Any, server_name: str | None) -> dict[str, Any]:
    data = {column.name: getattr(job, column.name) for column in job.__table__.columns}
    for key, value in list(data.items()):
        if isinstance(value, datetime):
            data[key] = value.isoformat(timespec="seconds").replace("+00:00", "Z")
    data["server_name"] = server_name
    data["payload"] = _decode_payload(data.get("payload"))
    return data


def overview_payload(session: Session, database_path: Path) -> dict[str, Any]:
    snapshot = DiagnosticsService(session).overview(limit=5)
    recent = [_job_payload(job, name) for job, name in snapshot["recent_jobs"]]
    failed = [_job_payload(job, name) for job, name in snapshot["failed_jobs"]]
    return {
        "servers": {
            "total": snapshot["server_count"],
            "enabled": snapshot["enabled_server_count"],
        },
        "jobs": {
            "running": snapshot["running_jobs"],
            "queued": snapshot["queued_jobs"],
            "recent": recent,
            "failed": failed,
        },
        "automation": {
            "webhook_events": snapshot["webhook_events"],
            "active_schedules": snapshot["active_schedules"],
            "notification_channels": snapshot["notification_channels"],
        },
        "system": {"db_size": database_path.stat().st_size if database_path.exists() else 0},
    }


def _context() -> Any:
    return current_app.extensions["route_context"]


def _db_file() -> Path:
    return get_database_path()


def _login_required(view):
    def wrapped(*args: Any, **kwargs: Any):
        return _context().login_required(view)(*args, **kwargs)

    wrapped.__name__ = view.__name__
    return wrapped


@_login_required
def diagnostics_all():
    with get_database().session() as session:
        servers = _context().server_service(session).enabled_servers()
    return jsonify([build_diagnostics(server.id) for server in servers])


@_login_required
def diagnostics_server(server_id: int):
    return jsonify(build_diagnostics(server_id))


@_login_required
def diagnostics_write_test(server_id: int):
    return jsonify({"ok": True, "message": "写入测试入口已保留；为避免误改 Plex 元数据，首版只做只读诊断。"})


def build_diagnostics(server_id: int) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    status = "ok"
    server = _context().server_model_row(server_id)
    if server is None:
        return {
            "server_id": server_id,
            "status": "error",
            "checks": [{"name": "服务器", "status": "error", "message": "不存在"}],
        }
    db_size = _db_file().stat().st_size if _db_file().exists() else 0
    with get_database().session() as session:
        repository = JobRepository(session)
        counts = repository.status_counts()
        schedules = repository.enabled_schedule_count(server_id)
    queued = counts.get("queued", 0)
    running = counts.get("running", 0)
    try:
        plex = PlexServer(
            _context().server_config_from_row(server),
            tags=_context().load_tags(),
            auto_login=False,
        )
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
    worker_online = _context().manager().worker_status()["online"]
    checks.extend(
        [
            {"name": "数据库", "status": "ok", "message": f"{db_size} bytes"},
            {
                "name": "任务队列",
                "status": "warning" if queued > 10 else "ok",
                "message": f"排队 {queued}，运行 {running}",
            },
            {"name": "定时任务", "status": "ok", "message": f"{schedules} 个启用"},
            {
                "name": "Webhook URL",
                "status": "ok",
                "message": url_for(
                    "webhooks.webhook",
                    server_id=server_id,
                    secret=server["webhook_secret"],
                    _external=True,
                ),
            },
            {
                "name": "任务 Worker",
                "status": "ok" if worker_online else "warning",
                "message": "在线" if worker_online else "离线",
            },
        ]
    )
    if any(check["status"] == "warning" for check in checks) and status != "error":
        status = "warning"
    return {"server_id": server_id, "server_name": server["name"], "status": status, "checks": checks}


def create_diagnostics_blueprint() -> Blueprint:
    blueprint = Blueprint("diagnostics", __name__)
    blueprint.add_url_rule("/api/diagnostics", view_func=diagnostics_all, methods=["GET"])
    blueprint.add_url_rule("/api/diagnostics/<int:server_id>", view_func=diagnostics_server, methods=["GET"])
    blueprint.add_url_rule(
        "/api/diagnostics/<int:server_id>/write-test",
        view_func=diagnostics_write_test,
        methods=["POST"],
    )
    return blueprint
