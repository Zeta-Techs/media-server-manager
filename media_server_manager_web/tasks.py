from __future__ import annotations

import json
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from media_server_manager.core import PlexServer, TaskCancelled, extract_external_ids

from .db import DB_FILE, connect, get_setting, load_tags, server_config_from_row, utcnow
from .scheduling import validate_schedule
from .services import JobQueue
from .tmdb import TMDBClient
from .catalog import sync_catalog, scan_plex_inventory, reconcile, reconcile_anime, sync_anime_quarter, discover_anime_schedules, due_anime_ids, update_show_schedule, enrich_item


class TaskManager:
    """Executes jobs already claimed by the dedicated worker process."""

    def __init__(self, db_file: Path | None = None) -> None:
        self.db_file = Path(db_file or DB_FILE)
        self.queue = JobQueue(self.db_file)
        self.progress_lock = threading.Lock()
        self.progress_buffers: Dict[int, Dict[str, Any]] = {}
        self.log_lock = threading.Lock()
        self.log_buffers: Dict[int, Dict[str, Any]] = {}

    def shutdown(self) -> None:
        return None

    def create_job(self, job_type: str, server_id: int, payload: Optional[Dict[str, Any]] = None) -> int:
        return self.queue.create_job(job_type, server_id, payload)

    def get_job(self, job_id: int) -> Dict[str, Any]:
        return self.queue.get_job(job_id)

    def list_jobs(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self.queue.list_jobs(limit)

    def list_logs(self, job_id: int, after_id: int = 0, limit: int = 300) -> List[Dict[str, Any]]:
        return self.queue.list_logs(job_id, after_id, limit)

    def cancel_queued_job(self, job_id: int) -> bool:
        return self.queue.cancel_queued_job(job_id)

    def _cancel_requested(self, job_id: int) -> bool:
        with connect(self.db_file) as db:
            row = db.execute(
                "SELECT cancel_requested_at, status FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return bool(row and (row["cancel_requested_at"] or row["status"] == "cancelled"))

    def _make_plex(self, job_id: int, server: Any) -> PlexServer:
        return PlexServer(
            server_config_from_row(server, self.db_file),
            tags=load_tags(self.db_file),
            log=lambda message: self._log(job_id, message),
            progress=lambda payload: self._progress(job_id, payload),
            cancelled=lambda: self._cancel_requested(job_id),
        )

    def _run_job(self, job_id: int) -> None:
        job = self.get_job(job_id)
        if job["status"] not in {"queued", "running"}:
            return
        with connect(self.db_file) as db:
            server = db.execute("SELECT * FROM servers WHERE id = ?", (job["server_id"],)).fetchone()
            if server is None:
                raise ValueError("服务器不存在")
            if job["status"] == "queued":
                db.execute(
                    """
                    UPDATE jobs SET status = 'running', started_at = ?, claimed_at = ?, stage = 'starting'
                    WHERE id = ? AND status = 'queued'
                    """,
                    (utcnow(), utcnow(), job_id),
                )
            else:
                db.execute(
                    "UPDATE jobs SET started_at = COALESCE(started_at, ?), stage = 'starting' WHERE id = ?",
                    (utcnow(), job_id),
                )
            db.commit()
        try:
            job_type = job["type"]
            payload = job.get("payload") or {}
            if job_type == "all":
                plex = self._make_plex(job_id, server)
                total = plex.count_all_items()
                self._progress(job_id, {"total": total, "stage": "counted"}, force=True)
                plex.loop_all()
                plex.loop_all_collections()
            elif job_type == "localize":
                self._run_localize_job(job_id, server, payload)
            elif job_type == "apply_change_set":
                self._run_apply_change_set_job(job_id, self._make_plex(job_id, server), payload)
            elif job_type == "rollback":
                self._run_rollback_job(job_id, self._make_plex(job_id, server), payload)
            elif job_type == "webhook":
                metadata = payload.get("metadata") or {}
                self._make_plex(job_id, server).process_new_item(metadata)
            elif job_type == "maintenance":
                self._run_maintenance_job(job_id, self._make_plex(job_id, server), payload)
            elif job_type == "collection_rule":
                self._run_collection_rule_job(job_id, self._make_plex(job_id, server), payload)
            elif job_type == "tag_suggestions":
                self._run_tag_suggestion_job(job_id, self._make_plex(job_id, server))
            elif job_type == "episode_audit":
                self._run_episode_audit_job(job_id, self._make_plex(job_id, server), payload)
            elif job_type == "continue_watching_preview":
                self._run_continue_watching_preview_job(job_id, self._make_plex(job_id, server), payload)
            elif job_type == "continue_watching_apply":
                self._run_continue_watching_apply_job(job_id, self._make_plex(job_id, server), payload)
            elif job_type == "notification_test":
                self._send_notifications("notification_test", {"job_id": job_id, "message": "CLP 通知测试"})
            elif job_type == "notification_event":
                self._send_notifications("webhook_event", payload)
            elif job_type == "tmdb_catalog_sync":
                run_id = int(payload.get("run_id") or 0)
                if run_id:
                    with connect(self.db_file) as db:
                        db.execute("UPDATE tmdb_sync_runs SET status='running', started_at=? WHERE id=?", (utcnow(), run_id)); db.commit()
                result = sync_catalog(self.db_file, payload, progress=lambda p: self._progress(job_id, p), cancelled=lambda: self._cancel_requested(job_id))
                if run_id:
                    with connect(self.db_file) as db:
                        db.execute("UPDATE tmdb_sync_runs SET status='succeeded', processed=?, errors=?, finished_at=? WHERE id=?", (result['processed'], result['errors'], utcnow(), run_id)); db.commit()
                self._log(job_id, f"TMDB 目录同步完成：{result['processed']} 项，错误 {result['errors']} 项。")
            elif job_type == "plex_inventory_sync":
                count = scan_plex_inventory(self.db_file, int(payload["server_id"]), payload.get("library_ids"), progress=lambda p: self._progress(job_id, p))
                reconcile(self.db_file, int(payload["server_id"]))
                self._log(job_id, f"Plex 资源扫描完成：{count} 项。")
            elif job_type == "media_reconcile":
                count = reconcile(self.db_file, int(payload["server_id"]))
                self._log(job_id, f"媒体对比完成：{count} 项。")
            elif job_type == "media_sync":
                sync_catalog(self.db_file, payload, progress=lambda p: self._progress(job_id, p), cancelled=lambda: self._cancel_requested(job_id))
                for server_id in payload.get("server_ids") or []:
                    scan_plex_inventory(self.db_file, int(server_id))
                    reconcile(self.db_file, int(server_id))
            elif job_type == "anime_quarter_sync":
                result = sync_anime_quarter(self.db_file, int(payload["year"]), str(payload["quarter"]), progress=lambda p: self._progress(job_id, p), cancelled=lambda: self._cancel_requested(job_id), max_pages=int(payload.get("max_pages", 20)))
                self._log(job_id, f"季度新番同步完成：{result['year']} {result['quarter']}，共 {result['processed']} 部。")
            elif job_type == "anime_current_sync":
                today = date.today(); quarter = f"Q{((today.month - 1) // 3) + 1}"
                result = sync_anime_quarter(self.db_file, today.year, quarter, progress=lambda p: self._progress(job_id, p), cancelled=lambda: self._cancel_requested(job_id), max_pages=int(payload.get("max_pages", 20)))
                self._log(job_id, f"当前季度同步完成：{result['year']} {result['quarter']}。")
            elif job_type == "anime_schedule_discovery":
                count = discover_anime_schedules(self.db_file, progress=lambda p: self._progress(job_id, p), cancelled=lambda: self._cancel_requested(job_id))
                self._log(job_id, f"新番排期发现完成：{count} 部。")
            elif job_type in {"anime_scheduled_sync", "anime_active_fallback_sync"}:
                tmdb_id = int(payload["tmdb_id"])
                details = enrich_item(self.db_file, "tv", tmdb_id)
                update_show_schedule(self.db_file, tmdb_id, details)
                with connect(self.db_file) as db:
                    server_ids = [int(payload.get("server_id"))] if payload.get("server_id") else [int(row["id"]) for row in db.execute("SELECT id FROM servers WHERE enabled=1").fetchall()]
                    db.execute("""UPDATE anime_sync_events SET executed_at=?, status='succeeded', job_id=?
                                 WHERE tmdb_id=? AND status='scheduled' AND scheduled_at <= ?""", (utcnow(), job_id, tmdb_id, utcnow()))
                    db.commit()
                for server_id in server_ids:
                    reconcile_anime(self.db_file, server_id)
                self._log(job_id, f"已同步番剧排期与详情：TMDB {tmdb_id}。")
            elif job_type == "anime_history_sync":
                for year in range(int(payload.get("start_year", 2000)), int(payload.get("end_year", date.today().year)) + 1):
                    for quarter in ("Q1", "Q2", "Q3", "Q4"):
                        if self._cancel_requested(job_id): raise TaskCancelled("任务已取消")
                        sync_anime_quarter(self.db_file, year, quarter, progress=lambda p: self._progress(job_id, p), cancelled=lambda: self._cancel_requested(job_id))
            elif job_type == "plex_tv_inventory_sync":
                count = scan_plex_inventory(self.db_file, int(payload["server_id"]), payload.get("library_ids"), progress=lambda p: self._progress(job_id, p))
                reconcile_anime(self.db_file, int(payload["server_id"]))
                self._log(job_id, f"电视剧库扫描完成：{count} 项。")
            elif job_type == "anime_reconcile":
                count = reconcile_anime(self.db_file, int(payload["server_id"]))
                self._log(job_id, f"新番对比完成：{count} 项。")
            else:
                raise ValueError(f"未知任务类型：{job_type}")
            if self._cancel_requested(job_id):
                raise TaskCancelled("任务已取消")
            with connect(self.db_file) as db:
                self._flush_progress(job_id, force_publish=False)
                db.execute("UPDATE jobs SET status = 'succeeded', finished_at = ? WHERE id = ?", (utcnow(), job_id))
                db.commit()
            self._log(job_id, "任务执行完成。")
            self._send_notifications("job_succeeded", self.get_job(job_id))
        except TaskCancelled as exc:
            self._flush_progress(job_id, force_publish=False)
            with connect(self.db_file) as db:
                db.execute(
                    "UPDATE jobs SET status = 'cancelled', finished_at = ?, error = ? WHERE id = ?",
                    (utcnow(), str(exc), job_id),
                )
                db.commit()
            self._log(job_id, "任务已安全取消。")
        except Exception as exc:
            self._flush_progress(job_id, force_publish=False)
            with connect(self.db_file) as db:
                db.execute(
                    "UPDATE jobs SET status = 'failed', finished_at = ?, error = ? WHERE id = ?",
                    (utcnow(), str(exc), job_id),
                )
                db.commit()
            self._log(job_id, f"任务失败：{exc}")
            self._send_notifications("job_failed", self.get_job(job_id))
        finally:
            self._flush_progress(job_id, force_publish=False)
            self._flush_logs(job_id)

    def _progress(self, job_id: int, payload: Dict[str, Any], force: bool = False) -> None:
        immediate_keys = {"stage", "library", "total"}
        if force or immediate_keys.intersection(payload):
            self._flush_progress(job_id, force_publish=False)
            self._apply_progress(job_id, payload)
            return

        now = time.monotonic()
        should_flush = False
        with self.progress_lock:
            buffer = self.progress_buffers.setdefault(
                job_id,
                {
                    "processed_delta": 0,
                    "changes_delta": 0,
                    "errors_delta": 0,
                    "last_flush": now,
                },
            )
            for key in ("processed_delta", "changes_delta", "errors_delta"):
                if key in payload:
                    buffer[key] += int(payload[key] or 0)
            if now - buffer["last_flush"] >= 0.5:
                should_flush = True
        if should_flush:
            self._flush_progress(job_id)

    def _flush_progress(self, job_id: int, force_publish: bool = True) -> None:
        with self.progress_lock:
            buffer = self.progress_buffers.get(job_id)
            if not buffer:
                return
            payload = {
                key: buffer.get(key, 0)
                for key in ("processed_delta", "changes_delta", "errors_delta")
                if buffer.get(key, 0)
            }
            if not payload:
                buffer["last_flush"] = time.monotonic()
                return
            buffer["processed_delta"] = 0
            buffer["changes_delta"] = 0
            buffer["errors_delta"] = 0
            buffer["last_flush"] = time.monotonic()
        self._apply_progress(job_id, payload, publish=force_publish)

    def _apply_progress(self, job_id: int, payload: Dict[str, Any], publish: bool = True) -> None:
        fields: Dict[str, Any] = {}
        increments: Dict[str, int] = {}
        for key in ("stage", "library"):
            if key in payload:
                fields["current_library" if key == "library" else key] = payload[key] or ""
        for key in ("total",):
            if key in payload:
                fields[key] = int(payload[key] or 0)
        for key, column in (
            ("processed_delta", "processed"),
            ("changes_delta", "changes"),
            ("errors_delta", "errors"),
        ):
            if key in payload:
                increments[column] = int(payload[key] or 0)
        if not fields and not increments:
            return
        with connect(self.db_file) as db:
            assignments = []
            values: List[Any] = []
            for column, value in fields.items():
                assignments.append(f"{column} = ?")
                values.append(value)
            for column, value in increments.items():
                assignments.append(f"{column} = {column} + ?")
                values.append(value)
            values.append(job_id)
            db.execute(f"UPDATE jobs SET {', '.join(assignments)} WHERE id = ?", values)
            db.commit()
        if publish:
            self._publish_job(job_id)

    def _log(self, job_id: int, message: str) -> None:
        should_flush = False
        with self.log_lock:
            buffer = self.log_buffers.setdefault(
                job_id, {"messages": [], "last_flush": 0.0}
            )
            buffer["messages"].append((job_id, message, utcnow()))
            should_flush = (
                len(buffer["messages"]) >= 20
                or time.monotonic() - float(buffer["last_flush"]) >= 0.25
            )
        if should_flush:
            self._flush_logs(job_id)

    def _flush_logs(self, job_id: int) -> None:
        with self.log_lock:
            buffer = self.log_buffers.get(job_id)
            if not buffer or not buffer["messages"]:
                return
            messages = list(buffer["messages"])
            buffer["messages"].clear()
            buffer["last_flush"] = time.monotonic()
        with connect(self.db_file) as db:
            db.executemany(
                "INSERT INTO job_logs (job_id, message, created_at) VALUES (?, ?, ?)",
                messages,
            )
            db.commit()

    def _publish_job(self, job_id: int) -> None:
        return None

    def _run_localize_job(self, job_id: int, server: Any, payload: Dict[str, Any]) -> None:
        mode = payload.get("mode") or "dry_run"
        if mode not in {"dry_run", "apply"}:
            raise ValueError("本地化模式无效")
        scope = payload.get("scope") or {}
        dry_run = mode == "dry_run"
        with connect(self.db_file) as db:
            cur = db.execute(
                "INSERT INTO change_sets (job_id, server_id, mode, scope, created_at) VALUES (?, ?, ?, ?, ?)",
                (job_id, server["id"], mode, json.dumps(scope, ensure_ascii=False), utcnow()),
            )
            change_set_id = int(cur.lastrowid or 0)
            db.commit()

        change_lock = threading.Lock()
        change_rows: List[tuple[Any, ...]] = []

        def flush_changes() -> None:
            with change_lock:
                if not change_rows:
                    return
                rows = list(change_rows)
                change_rows.clear()
            with connect(self.db_file) as db:
                db.executemany(
                    """
                    INSERT INTO changes (
                        change_set_id, job_id, server_id, library_id, media_type, rating_key,
                        title, field, old_value, new_value, old_locked, apply_status,
                        applied, created_at, applied_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                db.commit()

        def record_change(change: Dict[str, Any]) -> None:
            now = utcnow()
            row = (
                change_set_id,
                job_id,
                server["id"],
                int(change.get("library_id") or 0),
                change.get("media_type") or "",
                str(change.get("rating_key") or ""),
                change.get("title") or "",
                change.get("field") or "",
                json.dumps(change.get("old_value"), ensure_ascii=False),
                json.dumps(change.get("new_value"), ensure_ascii=False),
                None if change.get("old_locked") is None else int(bool(change.get("old_locked"))),
                "applied" if change.get("applied") else "pending",
                1 if change.get("applied") else 0,
                now,
                now if change.get("applied") else None,
            )
            with change_lock:
                change_rows.append(row)
                should_flush = len(change_rows) >= 50
            if should_flush:
                flush_changes()

        plex = PlexServer(
            server_config_from_row(server, self.db_file),
            tags=load_tags(self.db_file),
            log=lambda message: self._log(job_id, message),
            progress=lambda progress: self._progress(job_id, progress),
            change=record_change,
            cancelled=lambda: self._cancel_requested(job_id),
            dry_run=dry_run,
            scope=scope,
        )
        try:
            total = plex.count_all_items()
            self._progress(
                job_id, {"total": total, "stage": "preview" if dry_run else "apply"}, force=True
            )
            plex.loop_all()
            plex.loop_all_collections()
        finally:
            flush_changes()

    def rollback_job(self, source_job_id: int) -> int:
        return self.queue.rollback_job(source_job_id)

    def apply_preview_job(self, source_job_id: int) -> int:
        return self.queue.apply_preview_job(source_job_id)

    def _run_apply_change_set_job(
        self, job_id: int, plex: PlexServer, payload: Dict[str, Any]
    ) -> None:
        source_change_set_id = int(payload["source_change_set_id"])
        job = self.get_job(job_id)
        with connect(self.db_file) as db:
            source = db.execute(
                "SELECT * FROM change_sets WHERE id = ? AND mode = 'dry_run'",
                (source_change_set_id,),
            ).fetchone()
            if source is None or int(source["server_id"]) != int(job["server_id"]):
                raise ValueError("预览变更快照不存在")
            rows = db.execute(
                "SELECT * FROM changes WHERE change_set_id = ? ORDER BY id ASC",
                (source_change_set_id,),
            ).fetchall()
            cur = db.execute(
                """
                INSERT INTO change_sets (
                    job_id, server_id, mode, scope, source_change_set_id, created_at
                ) VALUES (?, ?, 'apply_snapshot', ?, ?, ?)
                """,
                (job_id, job["server_id"], source["scope"], source_change_set_id, utcnow()),
            )
            change_set_id = int(cur.lastrowid or 0)
            db.commit()
        self._progress(job_id, {"total": len(rows), "stage": "apply_snapshot"}, force=True)
        for row in rows:
            if self._cancel_requested(job_id):
                raise TaskCancelled("任务已取消")
            change = dict(row)
            old_value = json.loads(change["old_value"] or "null")
            new_value = json.loads(change["new_value"] or "null")
            status = "pending"
            error = ""
            applied = 0
            try:
                current_value, _ = plex.read_field_state(change["rating_key"], change["field"])
                if current_value != old_value:
                    status = "conflict"
                    error = "当前值与预览时不一致"
                    self._log(job_id, f"跳过冲突：{change['title']} · {change['field']}")
                    self._progress(job_id, {"processed_delta": 1, "errors_delta": 1})
                else:
                    plex.apply_change_value(change, new_value, True)
                    status = "applied"
                    applied = 1
                    self._log(job_id, f"已应用：{change['title']} · {change['field']}")
                    self._progress(job_id, {"processed_delta": 1, "changes_delta": 1})
            except Exception as exc:
                status = "failed"
                error = str(exc)
                self._log(job_id, f"应用失败：{change['title']} · {change['field']}：{exc}")
                self._progress(job_id, {"processed_delta": 1, "errors_delta": 1})
            with connect(self.db_file) as db:
                db.execute(
                    """
                    INSERT INTO changes (
                        change_set_id, job_id, server_id, library_id, media_type,
                        rating_key, title, field, old_value, new_value, old_locked,
                        apply_status, apply_error, applied, created_at, applied_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        change_set_id,
                        job_id,
                        job["server_id"],
                        change["library_id"],
                        change["media_type"],
                        change["rating_key"],
                        change["title"],
                        change["field"],
                        change["old_value"],
                        change["new_value"],
                        change["old_locked"],
                        status,
                        error,
                        applied,
                        utcnow(),
                        utcnow() if applied else None,
                    ),
                )
                db.commit()

    def _run_rollback_job(self, job_id: int, plex: PlexServer, payload: Dict[str, Any]) -> None:
        source_job_id = int(payload["source_job_id"])
        with connect(self.db_file) as db:
            rows = db.execute(
                "SELECT * FROM changes WHERE job_id = ? AND applied = 1 AND rollback_status = '' ORDER BY id ASC",
                (source_job_id,),
            ).fetchall()
        self._progress(job_id, {"total": len(rows), "stage": "rollback"}, force=True)
        for row in rows:
            change = dict(row)
            change["old_value"] = json.loads(change.get("old_value") or "null")
            change["new_value"] = json.loads(change.get("new_value") or "null")
            change["old_locked"] = (
                None if change.get("old_locked") is None else bool(change["old_locked"])
            )
            try:
                current_value, _ = plex.read_field_state(change["rating_key"], change["field"])
                if current_value != change["new_value"]:
                    with connect(self.db_file) as db:
                        db.execute(
                            "UPDATE changes SET rollback_status = 'conflict', rollback_error = ? WHERE id = ?",
                            ("当前值已发生变化", change["id"]),
                        )
                        db.commit()
                    self._log(job_id, f"跳过回滚冲突：{change['title']} · {change['field']}")
                    self._progress(job_id, {"processed_delta": 1, "errors_delta": 1})
                    continue
                plex.apply_recorded_change(change)
                with connect(self.db_file) as db:
                    db.execute(
                        "UPDATE changes SET rollback_status = 'rolled_back', rolled_back_at = ? WHERE id = ?",
                        (utcnow(), change["id"]),
                    )
                    db.commit()
                self._log(job_id, f"已回滚 {change['title']} 的 {change['field']}")
                self._progress(job_id, {"processed_delta": 1, "changes_delta": 1})
            except Exception as exc:
                with connect(self.db_file) as db:
                    db.execute(
                        "UPDATE changes SET rollback_status = 'failed', rollback_error = ? WHERE id = ?",
                        (str(exc), change["id"]),
                    )
                    db.commit()
                self._log(job_id, f"回滚失败：{change['title']} {change['field']}：{exc}")
                self._progress(job_id, {"processed_delta": 1, "errors_delta": 1})

    def _run_maintenance_job(self, job_id: int, plex: PlexServer, payload: Dict[str, Any]) -> None:
        action = payload.get("action")
        self._progress(job_id, {"stage": "maintenance", "total": 1}, force=True)
        if action == "scan_library":
            library_id = int(payload["library_id"])
            plex.scan_library(library_id)
            self._log(job_id, f"已触发媒体库扫描：{library_id}")
        elif action == "refresh_metadata":
            rating_key = str(payload["rating_key"])
            plex.refresh_metadata(rating_key)
            self._log(job_id, f"已触发元数据刷新：{rating_key}")
        elif action == "analyze_metadata":
            rating_key = str(payload["rating_key"])
            plex.analyze_metadata(rating_key)
            self._log(job_id, f"已触发媒体分析：{rating_key}")
        else:
            raise ValueError("未知维护操作")
        self._progress(job_id, {"processed_delta": 1, "changes_delta": 1})

    def _run_collection_rule_job(self, job_id: int, plex: PlexServer, payload: Dict[str, Any]) -> None:
        rule_id = int(payload["rule_id"])
        preview = bool(payload.get("preview"))
        matches = self.preview_collection_rule(rule_id, plex=plex)
        self._progress(job_id, {"stage": "collection_rule", "total": len(matches)}, force=True)
        with connect(self.db_file) as db:
            db.execute(
                "INSERT INTO collection_rule_runs (rule_id, job_id, status, matched_count, created_at) VALUES (?, ?, ?, ?, ?)",
                (rule_id, job_id, "preview" if preview else "reported", len(matches), utcnow()),
            )
            db.commit()
        for match in matches:
            self._log(job_id, f"{'[预览] ' if preview else ''}合集命中：{match['title']}")
            self._progress(job_id, {"processed_delta": 1})
        if not preview:
            self._log(job_id, "合集命中报告已生成；本任务不会写入 Plex 合集。")

    def _run_tag_suggestion_job(self, job_id: int, plex: PlexServer) -> None:
        self._progress(job_id, {"stage": "tag_suggestions"}, force=True)
        suggestions = plex.scan_unmapped_tags()
        server_id = int(self.get_job(job_id)["server_id"])
        now = utcnow()
        with connect(self.db_file) as db:
            db.execute("DELETE FROM tag_suggestions WHERE server_id = ?", (server_id,))
            for field, values in suggestions.items():
                for source, count in values.items():
                    db.execute(
                        """
                        INSERT INTO tag_suggestions (server_id, field, source, suggested, count, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (server_id, field, source, source, int(count), now),
                    )
            db.commit()
        self._log(job_id, f"已生成 {sum(len(values) for values in suggestions.values())} 条标签推荐。")

    def _run_continue_watching_preview_job(self, job_id: int, plex: PlexServer, payload: Dict[str, Any]) -> None:
        library_id = int(payload["library_id"])
        server_id = int(self.get_job(job_id)["server_id"])
        now = utcnow()
        with connect(self.db_file) as db:
            cur = db.execute(
                """
                INSERT INTO continue_watching_runs (job_id, server_id, library_id, mode, status, created_at)
                VALUES (?, ?, ?, 'preview', 'running', ?)
                """,
                (job_id, server_id, library_id, now),
            )
            run_id = int(cur.lastrowid or 0)
            db.commit()
        try:
            candidates = plex.episode_progress_candidates(library_id)
            self._progress(job_id, {"stage": "continue_watching_preview", "total": len(candidates)}, force=True)
            with connect(self.db_file) as db:
                for candidate in candidates:
                    db.execute(
                        """
                        INSERT INTO continue_watching_items (
                            run_id, server_id, library_id, show_title, show_rating_key, episode_rating_key,
                            season, episode, episode_title, duration, planned_offset, current_offset,
                            view_count, status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?)
                        """,
                        (
                            run_id,
                            server_id,
                            library_id,
                            candidate["show_title"],
                            candidate["show_rating_key"],
                            candidate["episode_rating_key"],
                            int(candidate["season"]),
                            int(candidate["episode"]),
                            candidate["episode_title"],
                            int(candidate["duration"]),
                            int(candidate["planned_offset"]),
                            int(candidate["current_offset"]),
                            int(candidate["view_count"]),
                            now,
                            now,
                        ),
                    )
                db.execute(
                    "UPDATE continue_watching_runs SET status = 'succeeded', candidate_count = ?, finished_at = ? WHERE id = ?",
                    (len(candidates), utcnow(), run_id),
                )
                db.commit()
            for candidate in candidates:
                self._log(job_id, f"候选：{candidate['show_title']} S{int(candidate['season']):02d}E{int(candidate['episode']):02d} {candidate['episode_title']}")
                self._progress(job_id, {"processed_delta": 1})
            self._log(job_id, f"继续观看候选生成完成：{len(candidates)} 个。")
        except Exception:
            with connect(self.db_file) as db:
                db.execute("UPDATE continue_watching_runs SET status = 'failed', finished_at = ? WHERE id = ?", (utcnow(), run_id))
                db.commit()
            raise

    def _run_continue_watching_apply_job(self, job_id: int, plex: PlexServer, payload: Dict[str, Any]) -> None:
        item_ids = [int(item_id) for item_id in (payload.get("item_ids") or [])]
        if not item_ids:
            raise ValueError("请选择要执行的候选剧集")
        server_id = int(self.get_job(job_id)["server_id"])
        now = utcnow()
        with connect(self.db_file) as db:
            source_items = db.execute(
                f"""
                SELECT * FROM continue_watching_items
                WHERE id IN ({','.join(['?'] * len(item_ids))})
                  AND server_id = ? AND status = 'candidate'
                ORDER BY id ASC
                """,
                (*item_ids, server_id),
            ).fetchall()
            if not source_items:
                raise ValueError("未找到可执行的候选剧集")
            library_id = int(source_items[0]["library_id"])
            if any(int(item["library_id"]) != library_id for item in source_items):
                raise ValueError("候选剧集必须来自同一个媒体库")
            cur = db.execute(
                """
                INSERT INTO continue_watching_runs (job_id, server_id, library_id, mode, status, candidate_count, created_at)
                VALUES (?, ?, ?, 'apply', 'running', ?, ?)
                """,
                (job_id, server_id, library_id, len(source_items), now),
            )
            run_id = int(cur.lastrowid or 0)
            copied_items = []
            for item in source_items:
                cur_item = db.execute(
                    """
                    INSERT INTO continue_watching_items (
                        run_id, server_id, library_id, show_title, show_rating_key, episode_rating_key,
                        season, episode, episode_title, duration, planned_offset, current_offset,
                        view_count, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                    """,
                    (
                        run_id,
                        server_id,
                        item["library_id"],
                        item["show_title"],
                        item["show_rating_key"],
                        item["episode_rating_key"],
                        item["season"],
                        item["episode"],
                        item["episode_title"],
                        item["duration"],
                        item["planned_offset"],
                        item["current_offset"],
                        item["view_count"],
                        now,
                        now,
                    ),
                )
                copied_items.append((int(cur_item.lastrowid or 0), dict(item)))
            db.commit()
        self._progress(job_id, {"stage": "continue_watching_apply", "total": len(copied_items)}, force=True)
        applied_count = 0
        error_count = 0
        for copied_id, item in copied_items:
            try:
                metadata = plex.get_metadata(str(item["episode_rating_key"]))
                current_offset = int(metadata.get("viewOffset") or 0)
                view_count = int(metadata.get("viewCount") or 0)
                if current_offset != int(item["current_offset"] or 0) or view_count != int(
                    item["view_count"] or 0
                ):
                    result = "剧集播放状态已变化，未写入。"
                    status = "conflict"
                    error_count += 1
                    self._log(
                        job_id,
                        f"跳过状态冲突：{item['show_title']} S{int(item['season']):02d}E{int(item['episode']):02d}",
                    )
                    self._progress(job_id, {"processed_delta": 1, "errors_delta": 1})
                else:
                    plex.set_playback_progress(
                        str(item["episode_rating_key"]), int(item["planned_offset"])
                    )
                    applied_count += 1
                    result = "已写入播放进度，Plex 首页可能需要刷新后显示。"
                    status = "applied"
                    self._log(job_id, f"已尝试加入继续观看：{item['show_title']} S{int(item['season']):02d}E{int(item['episode']):02d}")
                    self._progress(job_id, {"processed_delta": 1, "changes_delta": 1})
            except Exception as exc:
                error_count += 1
                result = str(exc)
                status = "failed"
                self._log(job_id, f"继续观看写入失败：{item['show_title']} S{int(item['season']):02d}E{int(item['episode']):02d}：{exc}")
                self._progress(job_id, {"processed_delta": 1, "errors_delta": 1})
            with connect(self.db_file) as db:
                db.execute(
                    "UPDATE continue_watching_items SET status = ?, result = ?, updated_at = ? WHERE id = ?",
                    (status, result, utcnow(), copied_id),
                )
                db.execute(
                    "UPDATE continue_watching_items SET status = ?, result = ?, updated_at = ? WHERE id = ?",
                    (status, result, utcnow(), item["id"]),
                )
                db.execute(
                    """
                    UPDATE continue_watching_runs
                    SET applied_count = ?, error_count = ?, status = 'running'
                    WHERE id = ?
                    """,
                    (applied_count, error_count, run_id),
                )
                db.commit()
        with connect(self.db_file) as db:
            db.execute(
                "UPDATE continue_watching_runs SET status = 'succeeded', applied_count = ?, error_count = ?, finished_at = ? WHERE id = ?",
                (applied_count, error_count, utcnow(), run_id),
            )
            db.commit()
        self._log(job_id, f"继续观看执行完成：成功 {applied_count}，失败 {error_count}。")

    def _run_episode_audit_job(self, job_id: int, plex: PlexServer, payload: Dict[str, Any]) -> None:
        library_id = int(payload["library_id"])
        options = payload.get("options") or {}
        ignore_specials = bool(options.get("ignore_specials", True))
        ignore_future = bool(options.get("ignore_future", True))
        only_ended = bool(options.get("only_ended", False))
        tmdb = TMDBClient(get_setting("tmdb_api_key", self.db_file))
        server_id = int(self.get_job(job_id)["server_id"])
        now = utcnow()
        with connect(self.db_file) as db:
            cur = db.execute(
                """
                INSERT INTO episode_audit_runs (job_id, server_id, library_id, status, options, created_at)
                VALUES (?, ?, ?, 'running', ?, ?)
                """,
                (job_id, server_id, library_id, json.dumps(options, ensure_ascii=False), now),
            )
            run_id = int(cur.lastrowid or 0)
            db.commit()
        try:
            shows = plex.list_show_items(library_id)
            self._progress(job_id, {"stage": "episode_audit", "total": len(shows)}, force=True)
            with connect(self.db_file) as db:
                db.execute("UPDATE episode_audit_runs SET total_shows = ? WHERE id = ?", (len(shows), run_id))
                db.commit()
            missing_count = 0
            unmatched_count = 0
            ambiguous_count = 0
            ignored_count = 0
            present_count = 0
            shows_with_missing: set[str] = set()
            for show in shows:
                title = show.get("title") or "未命名剧集"
                rating_key = str(show.get("ratingKey") or "")
                self._progress(job_id, {"library": title}, force=True)
                try:
                    existing = plex.collect_show_episode_numbers(rating_key)
                    match = self._match_tmdb_show(tmdb, show, server_id, library_id)
                    show_ignore = self._episode_ignore_for_show(server_id, library_id, show, match)
                    if show_ignore:
                        ignored_count += 1
                        self._insert_episode_audit_item(run_id, show, match, "ignored_show", {"reason": show_ignore.get("reason") or ""})
                        self._log(job_id, f"已忽略整部剧：{title}")
                        continue
                    if match.get("status") == "unmatched":
                        unmatched_count += 1
                        self._insert_episode_audit_item(run_id, show, match, "unmatched_show", None)
                        self._log(job_id, f"未匹配：{title}")
                        continue
                    if match.get("status") == "ambiguous":
                        ambiguous_count += 1
                        self._insert_episode_audit_item(
                            run_id,
                            show,
                            match,
                            "ambiguous_match",
                            {"candidates": match.get("candidates") or []},
                        )
                        self._log(job_id, f"低置信度匹配，已跳过：{title}")
                        continue
                    details = tmdb.tv_details(int(match["tmdb_id"]))
                    status = (details.get("status") or "").lower()
                    if only_ended and status not in {"ended", "canceled", "cancelled"}:
                        self._log(job_id, f"跳过未完结剧：{title} · {details.get('status') or '未知状态'}")
                        continue
                    show_missing = 0
                    for season in details.get("seasons") or []:
                        season_number = int(season.get("season_number") or 0)
                        if ignore_specials and season_number == 0:
                            continue
                        season_details = tmdb.season_details(int(match["tmdb_id"]), season_number)
                        for episode in season_details.get("episodes") or []:
                            episode_number = int(episode.get("episode_number") or 0)
                            if episode_number <= 0:
                                continue
                            if ignore_future and self._is_future_episode(episode.get("air_date")):
                                continue
                            episode_payload = {
                                "season": season_number,
                                "episode": episode_number,
                                "air_date": episode.get("air_date") or "",
                                "episode_title": episode.get("name") or "",
                                "overview": episode.get("overview") or "",
                            }
                            if (season_number, episode_number) in existing:
                                present_count += 1
                                self._insert_episode_audit_item(
                                    run_id,
                                    show,
                                    {**match, "tmdb_title": details.get("name") or ""},
                                    "present",
                                    episode_payload,
                                )
                            else:
                                ignored = self._episode_ignore_for_episode(
                                    server_id,
                                    library_id,
                                    show,
                                    match,
                                    season_number,
                                    episode_number,
                                )
                                status_value = "ignored_missing" if ignored else "missing"
                                if ignored:
                                    ignored_count += 1
                                else:
                                    show_missing += 1
                                    missing_count += 1
                                    shows_with_missing.add(rating_key or title)
                                self._insert_episode_audit_item(
                                    run_id,
                                    show,
                                    {**match, "tmdb_title": details.get("name") or ""},
                                    status_value,
                                    {**episode_payload, "ignore_reason": (ignored or {}).get("reason") or ""},
                                )
                    self._log(job_id, f"已检查：{title}，已有 {len(existing)} 集，缺失 {show_missing} 集。")
                except Exception as exc:
                    unmatched_count += 1
                    self._insert_episode_audit_item(
                        run_id,
                        show,
                        {"match_source": "error"},
                        "unmatched_show",
                        {"error": str(exc)},
                    )
                    self._log(job_id, f"检查失败：{title}：{exc}")
                    self._progress(job_id, {"errors_delta": 1})
                finally:
                    with connect(self.db_file) as db:
                        db.execute(
                            """
                            UPDATE episode_audit_runs
                            SET checked_shows = checked_shows + 1, missing_count = ?, unmatched_count = ?, ambiguous_count = ?, ignored_count = ?
                            WHERE id = ?
                            """,
                            (missing_count, unmatched_count, ambiguous_count, ignored_count, run_id),
                        )
                        db.commit()
                    self._progress(job_id, {"processed_delta": 1})
            with connect(self.db_file) as db:
                db.execute(
                    """
                    UPDATE episode_audit_runs
                    SET status = 'succeeded', missing_count = ?, unmatched_count = ?, ambiguous_count = ?, ignored_count = ?, finished_at = ?
                    WHERE id = ?
                    """,
                    (missing_count, unmatched_count, ambiguous_count, ignored_count, utcnow(), run_id),
                )
                db.commit()
            complete_shows = max(0, len(shows) - len(shows_with_missing) - unmatched_count)
            self._log(
                job_id,
                f"缺集检查完成：总剧数 {len(shows)}，完整 {complete_shows} 部，有缺失 {len(shows_with_missing)} 部，"
                f"已有 {present_count} 集，缺失 {missing_count} 集，已忽略 {ignored_count} 项，未匹配 {unmatched_count} 部，低置信度 {ambiguous_count} 部。",
            )
        except Exception:
            with connect(self.db_file) as db:
                db.execute("UPDATE episode_audit_runs SET status = 'failed', finished_at = ? WHERE id = ?", (utcnow(), run_id))
                db.commit()
            raise

    def _match_tmdb_show(
        self,
        tmdb: TMDBClient,
        show: Dict[str, Any],
        server_id: int,
        library_id: int,
    ) -> Dict[str, Any]:
        rating_key = str(show.get("ratingKey") or "")
        with connect(self.db_file) as db:
            override = db.execute(
                """
                SELECT tmdb_id FROM episode_match_overrides
                WHERE server_id = ? AND library_id = ? AND plex_rating_key = ?
                """,
                (server_id, library_id, rating_key),
            ).fetchone()
        if override is not None:
            return {
                "status": "matched",
                "tmdb_id": int(override["tmdb_id"]),
                "match_source": "manual_override",
            }
        ids = extract_external_ids(show)
        if ids.get("tmdb"):
            return {"status": "matched", "tmdb_id": int(ids["tmdb"]), "match_source": "tmdb_guid"}
        for source in ("tvdb", "imdb"):
            if ids.get(source):
                result = tmdb.find_tv_by_external_id(ids[source], source)
                if result:
                    return {
                        "status": "matched",
                        "tmdb_id": int(result["id"]),
                        "tmdb_title": result.get("name") or "",
                        "match_source": f"{source}_find",
                    }
        title = show.get("title") or ""
        year = self._show_year(show)
        results = tmdb.search_tv(title, year)
        if not results and year:
            results = tmdb.search_tv(title)
        if not results:
            return {"status": "unmatched", "match_source": "none"}
        if len(results) > 1:
            return {
                "status": "ambiguous",
                "tmdb_id": None,
                "tmdb_title": "",
                "match_source": "title_ambiguous",
                "candidates": [
                    {
                        "id": item.get("id"),
                        "name": item.get("name") or "",
                        "first_air_date": item.get("first_air_date") or "",
                    }
                    for item in results[:5]
                ],
            }
        return {
            "status": "matched",
            "tmdb_id": int(results[0]["id"]),
            "tmdb_title": results[0].get("name") or "",
            "match_source": "title_low_confidence",
        }

    @staticmethod
    def _show_year(show: Dict[str, Any]) -> Optional[int]:
        value = show.get("year")
        if value:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
        date_value = show.get("originallyAvailableAt") or ""
        if len(date_value) >= 4 and date_value[:4].isdigit():
            return int(date_value[:4])
        return None

    @staticmethod
    def _is_future_episode(air_date: str | None) -> bool:
        if not air_date:
            return False
        try:
            return date.fromisoformat(air_date) > date.today()
        except ValueError:
            return False

    def _insert_episode_audit_item(
        self,
        run_id: int,
        show: Dict[str, Any],
        match: Dict[str, Any],
        status: str,
        episode: Optional[Dict[str, Any]],
    ) -> None:
        details = episode or {}
        with connect(self.db_file) as db:
            db.execute(
                """
                INSERT INTO episode_audit_items (
                    run_id, show_title, plex_rating_key, tmdb_id, tmdb_title, match_source,
                    season, episode, air_date, status, details, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    show.get("title") or "",
                    str(show.get("ratingKey") or ""),
                    match.get("tmdb_id"),
                    match.get("tmdb_title") or "",
                    match.get("match_source") or "",
                    details.get("season"),
                    details.get("episode"),
                    details.get("air_date") or "",
                    status,
                    json.dumps(details, ensure_ascii=False),
                    utcnow(),
                ),
            )
            db.commit()

    def _episode_ignore_for_show(
        self,
        server_id: int,
        library_id: int,
        show: Dict[str, Any],
        match: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        return self._find_episode_ignore(server_id, library_id, show, match, None, None)

    def _episode_ignore_for_episode(
        self,
        server_id: int,
        library_id: int,
        show: Dict[str, Any],
        match: Dict[str, Any],
        season: int,
        episode: int,
    ) -> Optional[Dict[str, Any]]:
        return self._find_episode_ignore(server_id, library_id, show, match, season, episode)

    def _find_episode_ignore(
        self,
        server_id: int,
        library_id: int,
        show: Dict[str, Any],
        match: Dict[str, Any],
        season: Optional[int],
        episode: Optional[int],
    ) -> Optional[Dict[str, Any]]:
        plex_rating_key = str(show.get("ratingKey") or "")
        tmdb_id = match.get("tmdb_id")
        show_title = show.get("title") or ""
        with connect(self.db_file) as db:
            rows = db.execute(
                """
                SELECT * FROM episode_audit_ignores
                WHERE server_id = ? AND library_id = ?
                  AND (plex_rating_key = ? OR (tmdb_id IS NOT NULL AND tmdb_id = ?) OR show_title = ?)
                ORDER BY season IS NOT NULL ASC, id DESC
                """,
                (server_id, library_id, plex_rating_key, tmdb_id, show_title),
            ).fetchall()
        for row in rows:
            data = dict(row)
            if data.get("season") is None and data.get("episode") is None:
                return data
            if season is not None and episode is not None and int(data.get("season") or -1) == season and int(data.get("episode") or -1) == episode:
                return data
        return None

    def preview_collection_rule(self, rule_id: int, plex: Optional[PlexServer] = None) -> List[Dict[str, Any]]:
        with connect(self.db_file) as db:
            rule = db.execute("SELECT * FROM collection_rules WHERE id = ?", (rule_id,)).fetchone()
            if rule is None:
                raise ValueError("合集规则不存在")
            server = db.execute("SELECT * FROM servers WHERE id = ?", (rule["server_id"],)).fetchone()
        plex = plex or PlexServer(
            server_config_from_row(server, self.db_file), tags=load_tags(self.db_file)
        )
        library = next((item for item in plex.list_library() if int(item[0]) == int(rule["library_id"])), None)
        if not library:
            return []
        matches: List[Dict[str, Any]] = []
        needle = (rule["match_value"] or "").lower()
        for rating_key in plex.list_media_keys(library[:2], print_counts=False):
            metadata = plex.get_metadata(rating_key)
            title = metadata.get("title", "")
            if rule["match_field"] == "title":
                values = [title]
            else:
                values = [item.get("tag", "") for item in metadata.get(rule["match_field"].capitalize(), [])]
            if any(needle in str(value).lower() for value in values):
                matches.append({"rating_key": rating_key, "title": title})
        return matches

    def _send_notifications(self, event: str, payload: Dict[str, Any]) -> None:
        with connect(self.db_file) as db:
            rows = db.execute("SELECT * FROM notification_channels WHERE enabled = 1").fetchall()
        for row in rows:
            events = json.loads(row["events"] or "[]")
            if events and event not in events:
                continue
            try:
                import requests

                requests.post(
                    row["url"],
                    json={"event": event, "payload": payload, "product": "Media Server Manager"},
                    timeout=10,
                ).raise_for_status()
            except Exception:
                pass


def build_trigger(schedule_type: str, value: str) -> tuple[str, str]:
    """Compatibility validator for callers from the pre-worker architecture."""
    validate_schedule(schedule_type, value)
    return schedule_type, value.strip()

