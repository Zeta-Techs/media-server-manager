"""Task queue, progress stream and change-set routes."""

from __future__ import annotations

import json
import time
from functools import wraps
from typing import Any, Callable

from flask import Blueprint, Response, current_app, jsonify, request

from ...infrastructure.db.repositories.jobs import JobRepository
from ..dependencies import get_database


def _ctx() -> Any:
    return current_app.extensions["route_context"]


def _manager() -> Any:
    return current_app.extensions["task_manager"]


def _error(message: str, code: str, status: int):
    return jsonify({"error": message, "code": code}), status


def _login_required(view: Callable[..., Any]):
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any):
        return _ctx().login_required(view)(*args, **kwargs)

    return wrapped


@_login_required
def create_job():
    data = request.get_json(force=True)
    payload = data.get("payload") or {}
    if "mode" in data or "scope" in data:
        payload = {
            **payload,
            "mode": data.get("mode", payload.get("mode")),
            "scope": data.get("scope", payload.get("scope") or {}),
        }
    try:
        job_id = _manager().create_job(data.get("type") or "all", int(data["server_id"]), payload)
        return jsonify({"id": job_id})
    except (KeyError, TypeError, ValueError) as exc:
        return _error(str(exc), "invalid_job", 400)


@_login_required
def list_jobs():
    return jsonify(_manager().list_jobs())


@_login_required
def get_job(job_id: int):
    return jsonify(_manager().get_job(job_id))


@_login_required
def get_logs(job_id: int):
    after = int(request.args.get("after", "0"))
    limit = int(request.args.get("limit", "300"))
    return jsonify(_manager().list_logs(job_id, after, limit))


@_login_required
def get_job_changes(job_id: int):
    limit = max(1, min(int(request.args.get("limit", "500")), 2000))
    with get_database().session() as session:
        changes = JobRepository(session).changes(job_id, limit)
    for change in changes:
        change["old_value"] = json.loads(str(change.get("old_value") or "null"))
        change["new_value"] = json.loads(str(change.get("new_value") or "null"))
        if change.get("rollback_status") == "rolled_back":
            change["status"] = "rolled_back"
        elif change.get("apply_status") in {"conflict", "failed"} or change.get("rollback_status") in {
            "conflict",
            "failed",
        }:
            change["status"] = "conflict"
        elif change.get("apply_status") == "applied":
            change["status"] = "applied"
        else:
            change["status"] = "pending"
    return jsonify(changes)


@_login_required
def rollback_job(job_id: int):
    return jsonify({"id": _manager().rollback_job(job_id)})


@_login_required
def apply_preview_job(job_id: int):
    try:
        return jsonify({"id": _manager().apply_preview_job(job_id)})
    except ValueError as exc:
        return _error(str(exc), "preview_not_applicable", 400)


@_login_required
def cancel_job(job_id: int):
    try:
        return jsonify({"ok": True, "status": _manager().cancel_job(job_id)})
    except ValueError as exc:
        return _error(str(exc), "job_not_found", 404)


@_login_required
def retry_job(job_id: int):
    try:
        return jsonify({"id": _manager().retry_job(job_id)})
    except ValueError as exc:
        return _error(str(exc), "job_not_retryable", 400)


@_login_required
def job_events(job_id: int):
    initial_after = max(0, int(request.args.get("after", "0")))

    def stream():
        last_job = ""
        last_log_id = initial_after
        last_keepalive = time.monotonic()
        while True:
            try:
                job = _manager().get_job(job_id)
            except ValueError:
                yield f"data: {json.dumps({'kind': 'error', 'error': '任务不存在'}, ensure_ascii=False)}\n\n"
                return
            encoded = json.dumps(job, ensure_ascii=False, sort_keys=True)
            if encoded != last_job:
                last_job = encoded
                yield f"data: {json.dumps({'kind': 'job', 'job': job}, ensure_ascii=False)}\n\n"
            logs = _manager().list_logs(job_id, after_id=last_log_id, limit=500)
            for log in logs:
                last_log_id = max(last_log_id, int(log["id"]))
                yield f"data: {json.dumps({'kind': 'log', 'log': log}, ensure_ascii=False)}\n\n"
            if job["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                return
            if time.monotonic() - last_keepalive >= 15:
                last_keepalive = time.monotonic()
                yield ": keepalive\n\n"
            time.sleep(1)

    response = Response(stream(), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    return response


def create_jobs_blueprint() -> Blueprint:
    blueprint = Blueprint("jobs", __name__)
    blueprint.add_url_rule("/api/jobs", view_func=create_job, methods=["POST"])
    blueprint.add_url_rule("/api/jobs", view_func=list_jobs, methods=["GET"])
    blueprint.add_url_rule("/api/jobs/<int:job_id>", view_func=get_job, methods=["GET"])
    blueprint.add_url_rule("/api/jobs/<int:job_id>/logs", view_func=get_logs, methods=["GET"])
    blueprint.add_url_rule("/api/jobs/<int:job_id>/changes", view_func=get_job_changes, methods=["GET"])
    blueprint.add_url_rule("/api/jobs/<int:job_id>/rollback", view_func=rollback_job, methods=["POST"])
    blueprint.add_url_rule("/api/jobs/<int:job_id>/apply-preview", view_func=apply_preview_job, methods=["POST"])
    blueprint.add_url_rule("/api/jobs/<int:job_id>/cancel", view_func=cancel_job, methods=["POST"])
    blueprint.add_url_rule("/api/jobs/<int:job_id>/retry", view_func=retry_job, methods=["POST"])
    blueprint.add_url_rule("/api/jobs/<int:job_id>/events", view_func=job_events, methods=["GET"])
    return blueprint
