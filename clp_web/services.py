from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .db import DB_FILE, connect, decode_payload, utcnow

JOB_TYPES = {
    "all",
    "localize",
    "apply_change_set",
    "rollback",
    "webhook",
    "maintenance",
    "collection_rule",
    "tag_suggestions",
    "episode_audit",
    "continue_watching_preview",
    "continue_watching_apply",
    "notification_test",
    "notification_event",
    "tmdb_catalog_sync",
    "plex_inventory_sync",
    "media_reconcile",
    "media_sync",
    "media_library_refresh",
    "media_library_tmdb_refresh",
    "media_library_full_refresh",
    "media_library_show_recheck",
}
TERMINAL_JOB_STATUSES = {"succeeded", "failed", "cancelled", "interrupted"}


class JobQueue:
    def __init__(self, db_file: Path | None = None) -> None:
        self.db_file = Path(db_file or DB_FILE)

    def shutdown(self) -> None:
        return None

    def create_job(
        self,
        job_type: str,
        server_id: int,
        payload: Optional[Dict[str, Any]] = None,
        retry_of: int | None = None,
    ) -> int:
        if job_type not in JOB_TYPES:
            raise ValueError("不支持的任务类型")
        payload = payload or {}
        with connect(self.db_file) as db:
            row = db.execute("SELECT id FROM servers WHERE id = ? AND enabled = 1", (server_id,)).fetchone() if server_id else True
            if row is None:
                raise ValueError("服务器不存在或已禁用")
            cur = db.execute(
                """
                INSERT INTO jobs (type, server_id, status, payload, retry_of, created_at)
                VALUES (?, ?, 'queued', ?, ?, ?)
                """,
                (job_type, server_id, json.dumps(payload, ensure_ascii=False), retry_of, utcnow()),
            )
            job_id = int(cur.lastrowid or 0)
            db.execute(
                "INSERT INTO job_logs (job_id, message, created_at) VALUES (?, ?, ?)",
                (job_id, "任务已加入队列。", utcnow()),
            )
            db.commit()
        return job_id

    def get_job(self, job_id: int) -> Dict[str, Any]:
        with connect(self.db_file) as db:
            row = db.execute(
                """
                SELECT jobs.*, servers.name AS server_name
                FROM jobs LEFT JOIN servers ON servers.id = jobs.server_id
                WHERE jobs.id = ?
                """,
                (job_id,),
            ).fetchone()
        if row is None:
            raise ValueError("任务不存在")
        data = dict(row)
        data["payload"] = decode_payload(data.get("payload"))
        return data

    def list_jobs(self, limit: int = 50) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with connect(self.db_file) as db:
            rows = db.execute(
                """
                SELECT jobs.*, servers.name AS server_name
                FROM jobs LEFT JOIN servers ON servers.id = jobs.server_id
                ORDER BY jobs.id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        jobs = [dict(row) for row in rows]
        for job in jobs:
            job["payload"] = decode_payload(job.get("payload"))
        return jobs

    def list_logs(self, job_id: int, after_id: int = 0, limit: int = 300) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit or 300), 2000))
        with connect(self.db_file) as db:
            if after_id > 0:
                rows = db.execute(
                    "SELECT * FROM job_logs WHERE job_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
                    (job_id, after_id, limit),
                ).fetchall()
            else:
                rows = db.execute(
                    """
                    SELECT * FROM (
                        SELECT * FROM job_logs WHERE job_id = ? ORDER BY id DESC LIMIT ?
                    ) ORDER BY id ASC
                    """,
                    (job_id, limit),
                ).fetchall()
        return [dict(row) for row in rows]

    def cancel_job(self, job_id: int) -> str:
        with connect(self.db_file) as db:
            row = db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise ValueError("任务不存在")
            if row["status"] == "queued":
                db.execute(
                    "UPDATE jobs SET status = 'cancelled', finished_at = ?, error = '用户取消' WHERE id = ?",
                    (utcnow(), job_id),
                )
                status = "cancelled"
            elif row["status"] == "running":
                db.execute(
                    "UPDATE jobs SET cancel_requested_at = COALESCE(cancel_requested_at, ?) WHERE id = ?",
                    (utcnow(), job_id),
                )
                status = "cancelling"
            else:
                status = row["status"]
            db.commit()
        return status

    def cancel_queued_job(self, job_id: int) -> bool:
        return self.cancel_job(job_id) in {"cancelled", "cancelling"}

    def retry_job(self, source_job_id: int) -> int:
        source = self.get_job(source_job_id)
        if source["status"] not in {"failed", "cancelled", "interrupted"}:
            raise ValueError("只有失败、取消或中断的任务可以重试")
        if source["type"] == "media_library_show_recheck":
            from .rechecks import retry_recheck
            return retry_recheck(self.db_file, source)
        return self.create_job(
            source["type"], int(source["server_id"]), source.get("payload") or {}, source_job_id
        )

    def rollback_job(self, source_job_id: int) -> int:
        source = self.get_job(source_job_id)
        return self.create_job(
            "rollback", int(source["server_id"]), {"source_job_id": source_job_id}
        )

    def apply_preview_job(self, source_job_id: int) -> int:
        source = self.get_job(source_job_id)
        payload = source.get("payload") or {}
        if source.get("status") != "succeeded":
            raise ValueError("预览任务成功结束后才能执行")
        if source.get("type") == "localize" and payload.get("mode") == "dry_run":
            with connect(self.db_file) as db:
                change_set = db.execute(
                    "SELECT id FROM change_sets WHERE job_id = ? ORDER BY id DESC LIMIT 1",
                    (source_job_id,),
                ).fetchone()
            if change_set is None:
                raise ValueError("预览任务没有可执行的变更快照")
            return self.create_job(
                "apply_change_set",
                int(source["server_id"]),
                {"source_job_id": source_job_id, "source_change_set_id": int(change_set["id"])},
            )
        if source.get("type") == "continue_watching_preview":
            with connect(self.db_file) as db:
                run = db.execute(
                    """
                    SELECT id FROM continue_watching_runs
                    WHERE job_id = ? AND mode = 'preview' AND status = 'succeeded'
                    ORDER BY id DESC LIMIT 1
                    """,
                    (source_job_id,),
                ).fetchone()
                if run is None:
                    raise ValueError("未找到该预览任务的继续观看候选")
                rows = db.execute(
                    "SELECT id FROM continue_watching_items WHERE run_id = ? AND status = 'candidate'",
                    (run["id"],),
                ).fetchall()
            item_ids = [int(row["id"]) for row in rows]
            if not item_ids:
                raise ValueError("该预览任务没有可执行的候选剧集")
            return self.create_job(
                "continue_watching_apply",
                int(source["server_id"]),
                {"item_ids": item_ids, "source_preview_job_id": source_job_id},
            )
        raise ValueError("只有本地化预览任务或继续观看预览任务可以直接执行")

    def worker_status(self, stale_seconds: int = 15) -> Dict[str, Any]:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)
        with connect(self.db_file) as db:
            rows = db.execute(
                "SELECT * FROM worker_heartbeats ORDER BY heartbeat_at DESC"
            ).fetchall()
        workers = [dict(row) for row in rows]
        online = any(
            datetime.fromisoformat(row["heartbeat_at"].replace("Z", "+00:00")) >= cutoff
            for row in workers
        )
        return {"online": online, "workers": workers}
