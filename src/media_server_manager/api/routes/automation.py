"""Automation schedule routes backed by the automation repository."""

from __future__ import annotations

from functools import wraps
from typing import Any, Callable

from flask import Blueprint, current_app, jsonify, request

from ...application.job_orchestration.scheduling import describe_schedule, next_run_at, validate_schedule
from ...infrastructure.db.repositories.automation import AutomationRepository
from ...infrastructure.db.runtime import utcnow
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


@_login_required
def list_schedules():
    with get_database().session() as session:
        schedules = AutomationRepository(session).list_schedules()
    for schedule in schedules:
        schedule["label"] = describe_schedule(schedule["schedule_type"], schedule["schedule_value"])
    return jsonify(schedules)


@_login_required
def save_schedule():
    data = request.get_json(force=True)
    schedule_type = data.get("schedule_type") or "cron"
    schedule_value = (data.get("schedule_value") or "").strip()
    schedule_payload = data.get("payload") or {}
    try:
        validate_schedule(schedule_type, schedule_value)
    except ValueError as exc:
        return _error(str(exc), "invalid_schedule", 400)
    now = utcnow()
    enabled = bool(data.get("enabled", True))
    next_value = next_run_at(schedule_type, schedule_value) if enabled else None
    with get_database().transaction() as uow:
        AutomationRepository(uow.session).save_schedule(
            schedule_id=int(data["id"]) if data.get("id") else None,
            server_id=int(data["server_id"]),
            name=data.get("name") or "定时全量任务",
            schedule_type=schedule_type,
            schedule_value=schedule_value,
            payload=schedule_payload,
            enabled=enabled,
            next_run_at=next_value,
            now=now,
        )
    return jsonify({"ok": True})


@_login_required
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
        return _error(str(exc), "invalid_schedule", 400)


@_login_required
def delete_schedule(schedule_id: int):
    with get_database().transaction() as uow:
        deleted = AutomationRepository(uow.session).delete_schedule(schedule_id)
    if not deleted:
        return _error("定时任务不存在", "schedule_not_found", 404)
    return jsonify({"ok": True})


@_login_required
def get_tags():
    with get_database().session() as session:
        return jsonify(AutomationRepository(session).tags())


@_login_required
def update_tags():
    data = request.get_json(force=True)
    if not isinstance(data, dict) or not all(
        isinstance(key, str) and key.strip() and isinstance(value, str) and value.strip()
        for key, value in data.items()
    ):
        return _error("标签映射必须是非空字符串到非空字符串的 JSON 对象", "invalid_tags", 400)
    with get_database().transaction() as uow:
        AutomationRepository(uow.session).replace_tags(data)
    return jsonify({"ok": True})


@_login_required
def bulk_add_tags():
    data = request.get_json(force=True)
    entries = data.get("entries") or []
    with get_database().transaction() as uow:
        repository = AutomationRepository(uow.session)
        tags = repository.tags()
        for entry in entries:
            source = (entry.get("source") or "").strip()
            target = (entry.get("target") or entry.get("suggested") or "").strip()
            if source and target:
                tags[source] = target
        repository.replace_tags(tags)
    return jsonify({"ok": True, "count": len(entries)})


@_login_required
def scan_tag_suggestions():
    data = request.get_json(force=True)
    job_id = current_app.extensions["task_manager"].create_job("tag_suggestions", int(data["server_id"]), {})
    return jsonify({"id": job_id})


@_login_required
def list_tag_suggestions():
    server_id = request.args.get("server_id")
    with get_database().session() as session:
        rows = AutomationRepository(session).tag_suggestions(int(server_id) if server_id else None)
    return jsonify(rows)


def create_automation_blueprint() -> Blueprint:
    blueprint = Blueprint("automation", __name__)
    blueprint.add_url_rule("/api/schedules", view_func=list_schedules, methods=["GET"])
    blueprint.add_url_rule("/api/schedules", view_func=save_schedule, methods=["POST"])
    blueprint.add_url_rule("/api/schedules/describe", view_func=describe_schedule_api, methods=["GET"])
    blueprint.add_url_rule("/api/schedules/<int:schedule_id>", view_func=delete_schedule, methods=["DELETE"])
    blueprint.add_url_rule("/api/tags", view_func=get_tags, methods=["GET"])
    blueprint.add_url_rule("/api/tags", view_func=update_tags, methods=["POST"])
    blueprint.add_url_rule("/api/tags/bulk-add", view_func=bulk_add_tags, methods=["POST"])
    blueprint.add_url_rule("/api/tag-suggestions/scan", view_func=scan_tag_suggestions, methods=["POST"])
    blueprint.add_url_rule("/api/tag-suggestions", view_func=list_tag_suggestions, methods=["GET"])
    return blueprint

