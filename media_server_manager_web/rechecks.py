"""Durable, per-show debounce and retry queue for Plex episode additions."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .db import connect, utcnow


def _after(timestamp: str, seconds: int) -> str:
    return (
        (datetime.fromisoformat(timestamp.replace("Z", "+00:00")) + timedelta(seconds=seconds))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def request_recheck(db: Any, server_id: int, event: str, metadata: dict) -> str:
    """Called inside the webhook transaction; never contacts Plex/TMDB."""
    if event != "library.new":
        return "not_new_item"
    if metadata.get("type") != "episode":
        return "not_episode"
    try:
        library_id = int(metadata.get("librarySectionID") or 0)
    except (TypeError, ValueError):
        return "library_not_found"
    library = db.execute(
        "SELECT * FROM media_libraries WHERE server_id=? AND library_id=?", (server_id, library_id)
    ).fetchone()
    if library is None:
        return "library_not_found"
    if library["plex_type"] != 2 or not library["auto_recheck_new_episodes"]:
        return "auto_recheck_disabled"
    show_key = str(metadata.get("grandparentRatingKey") or "").strip()
    if not show_key:
        return "show_key_missing"
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    request = db.execute(
        "SELECT * FROM media_library_recheck_requests WHERE server_id=? AND library_id=? AND rating_key=?",
        (server_id, library_id, show_key),
    ).fetchone()
    if request and request["status"] in {"pending", "running", "retrying"}:
        # Slide the debounce window while waiting. During execution retain the
        # event watermark so completion can schedule a follow-up, never drop it.
        db.execute(
            "UPDATE media_library_recheck_requests SET last_event_at=?, next_run_at=?, updated_at=? WHERE id=?",
            (now, _after(now, 300), now, request["id"]),
        )
        return "auto_recheck_merged"
    if request and request["status"] == "succeeded" and request["last_event_at"] >= _after(now, -300):
        db.execute(
            "UPDATE media_library_recheck_requests SET last_event_at=?,next_run_at=?,status='pending',updated_at=? WHERE id=?",
            (now, _after(now, 300), now, request["id"]),
        )
        return "auto_recheck_merged"
    db.execute(
        """INSERT INTO media_library_recheck_requests
        (server_id,library_id,rating_key,first_event_at,last_event_at,next_run_at,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(server_id,library_id,rating_key) DO UPDATE SET
        first_event_at=excluded.first_event_at,last_event_at=excluded.last_event_at,
        next_run_at=excluded.next_run_at,attempt=0,status='pending',job_id=NULL,
        dispatched_event_at='',last_error='',missing_count=NULL,updated_at=excluded.updated_at""",
        (server_id, library_id, show_key, now, now, _after(now, 300), now, now),
    )
    return "auto_recheck_queued"


def finish_recheck(db: Any, request: Any, job: Any, now: str) -> None:
    """Reconcile terminal jobs, including worker restarts and manual retries."""
    if job["status"] == "succeeded":
        status = "pending" if request["last_event_at"] > request["dispatched_event_at"] else "succeeded"
        next_run = _after(request["last_event_at"], 300)
        db.execute(
            "UPDATE media_library_recheck_requests SET status=?,next_run_at=?,attempt=0,last_error='',updated_at=? WHERE id=?",
            (status, next_run, now, request["id"]),
        )
    elif job["status"] in {"failed", "interrupted"} and request["attempt"] == 0:
        db.execute(
            "UPDATE media_library_recheck_requests SET status='retrying',attempt=1,next_run_at=?,last_error=?,updated_at=? WHERE id=?",
            (_after(now, 60), job["error"], now, request["id"]),
        )
        db.execute(
            "INSERT INTO job_logs(job_id,message,created_at) VALUES (?,?,?)",
            (job["id"], "自动重检失败，60 秒后重试一次。", now),
        )
    else:
        db.execute(
            "UPDATE media_library_recheck_requests SET status='failed',last_error=?,updated_at=? WHERE id=?",
            (job["error"] or "任务已取消", now, request["id"]),
        )


def enqueue_due_rechecks(db_file: Path) -> None:
    now = utcnow()
    with connect(db_file) as db:
        db.execute("BEGIN IMMEDIATE")
        for request in db.execute(
            "SELECT * FROM media_library_recheck_requests WHERE status='running'"
        ).fetchall():
            job = db.execute("SELECT * FROM jobs WHERE id=?", (request["job_id"],)).fetchone()
            if job and job["status"] in {"succeeded", "failed", "interrupted", "cancelled"}:
                finish_recheck(db, request, job, now)
        due = db.execute(
            """SELECT r.* FROM media_library_recheck_requests r
            JOIN servers s ON s.id=r.server_id
            JOIN media_libraries l ON l.server_id=r.server_id AND l.library_id=r.library_id
            WHERE r.status IN ('pending','retrying') AND r.next_run_at <= ?
            AND s.enabled=1 AND l.plex_type=2 AND l.auto_recheck_new_episodes=1
            ORDER BY r.next_run_at,r.id""",
            (now,),
        ).fetchall()
        for request in due:
            # Reuse the existing server serialization; hold pending requests
            # until any queued/running refresh for this server has finished.
            busy = db.execute(
                "SELECT 1 FROM jobs WHERE server_id=? AND status IN ('queued','running') AND type IN ('media_library_refresh','media_library_full_refresh','media_library_tmdb_refresh','media_library_show_recheck') LIMIT 1",
                (request["server_id"],),
            ).fetchone()
            if busy:
                continue
            payload = dict(
                server_id=request["server_id"],
                library_id=request["library_id"],
                rating_key=request["rating_key"],
                auto_recheck_request_id=request["id"],
                attempt=request["attempt"],
            )
            job_id = db.execute(
                "INSERT INTO jobs(type,server_id,status,payload,created_at) VALUES ('media_library_show_recheck',?,'queued',?,?)",
                (request["server_id"], json.dumps(payload), now),
            ).lastrowid
            db.execute(
                "UPDATE media_library_recheck_requests SET status='running',job_id=?,dispatched_event_at=last_event_at,updated_at=? WHERE id=?",
                (job_id, now, request["id"]),
            )
            db.execute(
                "INSERT INTO job_logs(job_id,message,created_at) VALUES (?,?,?)",
                (job_id, "由 Plex 新增集事件创建，执行指定电视剧的 Plex → TMDB → 缺集对比。", now),
            )
        db.commit()


def retry_recheck(db_file: Path, source_job: dict) -> int:
    payload = source_job.get("payload") or {}
    with connect(db_file) as db:
        db.execute("BEGIN IMMEDIATE")
        request = db.execute(
            "SELECT * FROM media_library_recheck_requests WHERE id=?",
            (payload.get("auto_recheck_request_id"),),
        ).fetchone()
        if request is None:
            raise ValueError("自动重检请求不存在")
        if request["job_id"] != source_job["id"] or request["status"] in {"pending", "retrying"}:
            raise ValueError("该剧集已有更新的自动重检请求")
        active = db.execute(
            "SELECT 1 FROM jobs WHERE id=? AND status IN ('queued','running')", (request["job_id"],)
        ).fetchone()
        if active:
            raise ValueError("该剧集已有重检任务正在执行")
        now = utcnow()
        # A manual retry is one additional attempt, never another automatic
        # retry cycle. Bind it atomically to the current request generation.
        payload = {**payload, "attempt": 1}
        job_id = db.execute(
            "INSERT INTO jobs(type,server_id,status,payload,retry_of,created_at) VALUES ('media_library_show_recheck',?,'queued',?,?,?)",
            (source_job["server_id"], json.dumps(payload), source_job["id"], now),
        ).lastrowid
        db.execute(
            "UPDATE media_library_recheck_requests SET status='running',attempt=1,job_id=?,dispatched_event_at=last_event_at,last_error='',updated_at=? WHERE id=?",
            (job_id, now, request["id"]),
        )
        db.execute(
            "INSERT INTO job_logs(job_id,message,created_at) VALUES (?,?,?)",
            (job_id, "手动重试自动重检任务。", now),
        )
        db.commit()
    if job_id is None:
        raise RuntimeError("创建重检任务失败")
    return int(job_id)
