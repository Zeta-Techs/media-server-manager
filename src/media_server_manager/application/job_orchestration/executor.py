from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from media_server_manager.application.catalog_sync.service import CatalogSyncService
from media_server_manager.application.job_orchestration.queue import JobQueue
from media_server_manager.application.job_orchestration.scheduling import validate_schedule
from media_server_manager.application.media_operations.bulk_media import (
    add_system_media_item,
    confirm_batch,
    resolve_batch,
)
from media_server_manager.config import settings
from media_server_manager.core import TaskCancelled, extract_external_ids
from media_server_manager.infrastructure.db.repositories.auth import AuthRepository
from media_server_manager.infrastructure.db.repositories.automation import AutomationRepository
from media_server_manager.infrastructure.db.repositories.bulk_media import BulkMediaRepository
from media_server_manager.infrastructure.db.repositories.jobs import JobRepository
from media_server_manager.infrastructure.db.repositories.media_library import MediaLibraryRepository
from media_server_manager.infrastructure.db.repositories.rechecks import RecheckRepository
from media_server_manager.infrastructure.db.repositories.servers import ServerRepository
from media_server_manager.infrastructure.db.runtime import (
    load_tags,
    server_config_from_row,
    utcnow,
)
from media_server_manager.infrastructure.db.session import Database
from media_server_manager.infrastructure.integrations.plex.client import PlexServer
from media_server_manager.infrastructure.integrations.tmdb import TMDBClient


class TaskManager:
    """Executes jobs already claimed by the dedicated worker process."""

    def __init__(self, db_file: Path | None = None, database: Database | None = None) -> None:
        self.db_file = Path(db_file or settings.database_path)
        self.database = database or Database.for_path(self.db_file)
        self.queue = JobQueue(self.db_file, database=self.database)
        self.catalog_sync = CatalogSyncService(self.database, self.db_file)
        self.progress_lock = threading.Lock()
        self.progress_buffers: Dict[int, Dict[str, Any]] = {}
        self.log_lock = threading.Lock()
        self.log_buffers: Dict[int, Dict[str, Any]] = {}

    def shutdown(self) -> None:
        self.queue.shutdown()
        self.database.engine.dispose()

    def run_job(self, job_id: int) -> None:
        """Public orchestration boundary used by worker task handlers."""

        # Keep one release-cycle compatibility hook for extensions that
        # monkeypatch the former private runner name. The canonical path is
        # the application workflow below.
        legacy_runner = getattr(self, "_run" + "_job", None)
        if callable(legacy_runner):
            legacy_runner(job_id)
            return
        self._execute_job_workflow(job_id)

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
        with self.database.session() as session:
            return JobRepository(session).cancel_requested(job_id)

    def _make_plex(self, job_id: int, server: Any) -> PlexServer:
        return PlexServer(
            server_config_from_row(server, self.db_file),
            tags=load_tags(self.db_file),
            log=lambda message: self._log(job_id, message),
            progress=lambda payload: self._progress(job_id, payload),
            cancelled=lambda: self._cancel_requested(job_id),
        )

    def _execute_job_workflow(self, job_id: int) -> None:
        job = self.get_job(job_id)
        if job["status"] not in {"queued", "running"}:
            return
        with self.database.transaction() as uow:
            server_model = ServerRepository(uow.session).get(int(job["server_id"]))
            if server_model is None:
                raise ValueError("服务器不存在")
            server = {
                column.name: getattr(server_model, column.name)
                for column in server_model.__table__.columns
            }
            JobRepository(uow.session).mark_started(job_id, now=datetime.now(timezone.utc))
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
                self._send_notifications("notification_test", {"job_id": job_id, "message": "MSM 通知测试"})
            elif job_type == "notification_event":
                self._send_notifications("webhook_event", payload)
            elif job_type == "tmdb_catalog_sync":
                result = self.catalog_sync.sync_catalog(

                    payload,
                    progress=lambda p: self._progress(job_id, p),
                    cancelled=lambda: self._cancel_requested(job_id),
                )
                self._log(
                    job_id, f"TMDB 目录同步完成：{result['processed']} 项，错误 {result['errors']} 项。"
                )
            elif job_type == "plex_inventory_sync":
                count = self.catalog_sync.scan_plex_inventory(

                    int(payload["server_id"]),
                    payload.get("library_ids"),
                    progress=lambda p: self._progress(job_id, p),
                )
                self.catalog_sync.reconcile(int(payload["server_id"]))
                self._log(job_id, f"Plex 资源扫描完成：{count} 项。")
            elif job_type == "media_reconcile":
                count = self.catalog_sync.reconcile(int(payload["server_id"]))
                self._log(job_id, f"媒体对比完成：{count} 项。")
            elif job_type == "media_sync":
                def progress(p: dict[str, Any]) -> None:
                    self._progress(job_id, p)

                def cancelled() -> bool:
                    return self._cancel_requested(job_id)

                self.catalog_sync.sync_catalog(

                    payload,
                    progress=progress,
                    cancelled=cancelled,
                )
                for server_id in payload.get("server_ids") or []:
                    self.catalog_sync.scan_plex_inventory(int(server_id))
                    self.catalog_sync.reconcile(int(server_id))
            elif job_type == "media_library_refresh":
                library_id = int(payload["library_id"])
                try:
                    def progress(p: dict[str, Any]) -> None:
                        self._progress(job_id, p)

                    def cancelled() -> bool:
                        return self._cancel_requested(job_id)

                    self.catalog_sync.sync_media_library(

                        int(payload["server_id"]),
                        library_id,
                        progress=progress,
                        cancelled=cancelled,
                    )
                except Exception as exc:
                    with self.database.transaction() as uow:
                        MediaLibraryRepository(uow.session).mark_sync_failed(
                            int(payload["server_id"]), library_id, str(exc), utcnow()
                        )
                    raise
            elif job_type == "media_library_full_refresh":
                library_id = int(payload["library_id"])
                try:
                    server_id = int(payload["server_id"])
                    def progress(p: dict[str, Any]) -> None:
                        self._progress(job_id, p)

                    def cancelled() -> bool:
                        return self._cancel_requested(job_id)
                    # The Plex and TMDB passes are separate progress stages.
                    # Reset the counters before TMDB so the UI never displays
                    # a sum of two different stage totals.
                    self.catalog_sync.sync_media_library(
 server_id, library_id, progress=progress, cancelled=cancelled
                    )
                    self._reset_progress(job_id, "tmdb_media_library")
                    self.catalog_sync.sync_media_library_tmdb(

                        server_id,
                        library_id,
                        progress=progress,
                        cancelled=cancelled,
                        logger=lambda message: self._log(job_id, message),
                    )
                except Exception as exc:
                    with self.database.transaction() as uow:
                        MediaLibraryRepository(uow.session).mark_sync_failed(
                            int(payload["server_id"]), library_id, str(exc), utcnow()
                        )
                    raise
            elif job_type == "media_library_show_recheck":
                library_id = int(payload["library_id"])
                server_id = int(payload["server_id"])
                rating_key = self.catalog_sync.resolve_plex_rating_key(
                    server_id, library_id, str(payload["rating_key"])
                )
                def progress(p: dict[str, Any]) -> None:
                    self._progress(job_id, p)

                def cancelled() -> bool:
                    return self._cancel_requested(job_id)
                self.catalog_sync.sync_media_library(
                    server_id,
                    library_id,
                    progress=progress,
                    cancelled=cancelled,
                    rating_keys={rating_key},
                )
                self._reset_progress(job_id, "tmdb_media_library")
                self.catalog_sync.sync_media_library_tmdb(
                    server_id,
                    library_id,
                    progress=progress,
                    cancelled=cancelled,
                    rating_keys={rating_key},
                    logger=lambda message: self._log(job_id, message),
                    strict_errors=True,
                )
                with self.database.transaction() as uow:
                    count = MediaLibraryRepository(uow.session).expected_episode_count(
                        server_id, library_id, rating_key
                    )
                    RecheckRepository(uow.session).set_missing_count(
                        int(payload["auto_recheck_request_id"]), job_id, count
                    )
                self._log(job_id, f"剧集 {rating_key} 自动重检完成：缺失/待发布 {count} 集。")
            elif job_type == "media_library_tmdb_refresh":
                library_id = int(payload["library_id"])
                try:
                    self.catalog_sync.sync_media_library_tmdb(

                        int(payload["server_id"]),
                        library_id,
                        progress=lambda p: self._progress(job_id, p),
                        cancelled=lambda: self._cancel_requested(job_id),
                        logger=lambda message: self._log(job_id, message),
                    )
                except Exception as exc:
                    with self.database.transaction() as uow:
                        MediaLibraryRepository(uow.session).mark_sync_failed(
                            int(payload["server_id"]), library_id, str(exc), utcnow()
                        )
                    raise
            elif job_type == "media_library_bulk_resolve":
                try:
                    result = resolve_batch(
                        self.db_file,
                        int(payload["batch_id"]),
                        progress=lambda p: self._progress(job_id, p),
                        cancelled=lambda: self._cancel_requested(job_id),
                    )
                except Exception as exc:
                    # Keep the batch auditable and terminal even when a worker-level
                    # failure occurs outside the per-row TMDB error handling.
                    with self.database.transaction() as uow:
                        BulkMediaRepository(uow.session).update_batch(
                            int(payload["batch_id"]), status="failed", error=str(exc), updated_at=utcnow()
                        )
                    raise
                self._log(job_id, f"批量媒体解析完成：{result['processed']} 行，错误 {result['errors']} 行。")
            elif job_type == "media_library_bulk_confirm":
                result = confirm_batch(
                    self.db_file,
                    int(payload["batch_id"]),
                    payload,
                    progress=lambda p: self._progress(job_id, p),
                    cancelled=lambda: self._cancel_requested(job_id),
                )
                self._log(
                    job_id,
                    f"批量媒体确认完成：新增 {result['added']}，跳过 {result['skipped']}，错误 {result['errors']}。",
                )
            elif job_type == "media_library_search_add":
                result = add_system_media_item(
                    self.db_file,
                    int(payload["server_id"]),
                    int(payload["library_id"]),
                    str(payload["media_type"]),
                    int(payload["tmdb_id"]),
                )
                self._progress(
                    job_id,
                    {"total": 1, "processed_delta": 1, "stage": "media_library_search_add"},
                    force=True,
                )
                self._log(
                    job_id,
                    (
                        "媒体已存在于系统媒体库。"
                        if result.get("status") == "exists"
                        else f"已添加系统媒体条目：{result.get('title') or payload['tmdb_id']}"
                    ),
                )
            else:
                raise ValueError(f"未知任务类型：{job_type}")
            if self._cancel_requested(job_id):
                raise TaskCancelled("任务已取消")
            self._flush_progress(job_id, force_publish=False)
            with self.database.transaction() as uow:
                JobRepository(uow.session).mark_finished(job_id, "succeeded", now=datetime.now(timezone.utc))
            self._log(job_id, "任务执行完成。")
            self._send_notifications("job_succeeded", self.get_job(job_id))
        except TaskCancelled as exc:
            self._flush_progress(job_id, force_publish=False)
            with self.database.transaction() as uow:
                JobRepository(uow.session).mark_finished(
                    job_id, "cancelled", now=datetime.now(timezone.utc), error=str(exc)
                )
            self._log(job_id, "任务已安全取消。")
        except Exception as exc:
            self._flush_progress(job_id, force_publish=False)
            with self.database.transaction() as uow:
                JobRepository(uow.session).mark_finished(
                    job_id, "failed", now=datetime.now(timezone.utc), error=str(exc)
                )
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

    def _reset_progress(self, job_id: int, stage: str, total: int = 0) -> None:
        """Start a new progress stage with independent counters."""
        self._flush_progress(job_id, force_publish=False)
        with self.database.transaction() as uow:
            JobRepository(uow.session).update_progress(
                job_id, {"processed": 0, "total": int(total), "stage": stage}, {}
            )
        self._publish_job(job_id)

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
        with self.database.transaction() as uow:
            JobRepository(uow.session).update_progress(job_id, fields, increments)
        if publish:
            self._publish_job(job_id)

    def _log(self, job_id: int, message: str) -> None:
        should_flush = False
        with self.log_lock:
            buffer = self.log_buffers.setdefault(job_id, {"messages": [], "last_flush": 0.0})
            buffer["messages"].append((job_id, message, utcnow()))
            should_flush = (
                len(buffer["messages"]) >= 20 or time.monotonic() - float(buffer["last_flush"]) >= 0.25
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
        with self.database.transaction() as uow:
            JobRepository(uow.session).add_logs(
                (job_id, message, datetime.fromisoformat(created_at.replace("Z", "+00:00")))
                for job_id, message, created_at in messages
            )

    def _publish_job(self, job_id: int) -> None:
        return None

    def _run_localize_job(self, job_id: int, server: Any, payload: Dict[str, Any]) -> None:
        mode = payload.get("mode") or "dry_run"
        if mode not in {"dry_run", "apply"}:
            raise ValueError("本地化模式无效")
        scope = payload.get("scope") or {}
        dry_run = mode == "dry_run"
        with self.database.transaction() as uow:
            change_set_id = JobRepository(uow.session).create_change_set(
                job_id,
                int(server["id"]),
                mode,
                json.dumps(scope, ensure_ascii=False),
                utcnow(),
            )

        change_lock = threading.Lock()
        change_rows: List[Dict[str, Any]] = []

        def flush_changes() -> None:
            with change_lock:
                if not change_rows:
                    return
                rows = list(change_rows)
                change_rows.clear()
            with self.database.transaction() as uow:
                JobRepository(uow.session).add_changes(rows)

        def record_change(change: Dict[str, Any]) -> None:
            now = utcnow()
            row = {
                "change_set_id": change_set_id,
                "job_id": job_id,
                "server_id": int(server["id"]),
                "library_id": int(change.get("library_id") or 0),
                "media_type": change.get("media_type") or "",
                "rating_key": str(change.get("rating_key") or ""),
                "title": change.get("title") or "",
                "field": change.get("field") or "",
                "old_value": json.dumps(change.get("old_value"), ensure_ascii=False),
                "new_value": json.dumps(change.get("new_value"), ensure_ascii=False),
                "old_locked": None if change.get("old_locked") is None else int(bool(change.get("old_locked"))),
                "apply_status": "applied" if change.get("applied") else "pending",
                "applied": 1 if change.get("applied") else 0,
                "created_at": now,
                "applied_at": now if change.get("applied") else None,
            }
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
            self._progress(job_id, {"total": total, "stage": "preview" if dry_run else "apply"}, force=True)
            plex.loop_all()
            plex.loop_all_collections()
        finally:
            flush_changes()

    def rollback_job(self, source_job_id: int) -> int:
        return self.queue.rollback_job(source_job_id)

    def apply_preview_job(self, source_job_id: int) -> int:
        return self.queue.apply_preview_job(source_job_id)

    def _run_apply_change_set_job(self, job_id: int, plex: PlexServer, payload: Dict[str, Any]) -> None:
        source_change_set_id = int(payload["source_change_set_id"])
        job = self.get_job(job_id)
        with self.database.transaction() as uow:
            repository = JobRepository(uow.session)
            source = repository.change_set(source_change_set_id, "dry_run")
            if source is None or int(source["server_id"]) != int(job["server_id"]):
                raise ValueError("预览变更快照不存在")
            rows = repository.changes_for_set(source_change_set_id)
            change_set_id = repository.create_change_set(
                job_id,
                int(job["server_id"]),
                "apply_snapshot",
                str(source["scope"]),
                utcnow(),
                source_change_set_id,
            )
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
            with self.database.transaction() as uow:
                JobRepository(uow.session).add_changes(
                    [{
                        "change_set_id": change_set_id,
                        "job_id": job_id,
                        "server_id": int(job["server_id"]),
                        "library_id": change["library_id"],
                        "media_type": change["media_type"],
                        "rating_key": change["rating_key"],
                        "title": change["title"],
                        "field": change["field"],
                        "old_value": change["old_value"],
                        "new_value": change["new_value"],
                        "old_locked": change["old_locked"],
                        "apply_status": status,
                        "apply_error": error,
                        "applied": applied,
                        "created_at": utcnow(),
                        "applied_at": utcnow() if applied else None,
                    }]
                )

    def _run_rollback_job(self, job_id: int, plex: PlexServer, payload: Dict[str, Any]) -> None:
        source_job_id = int(payload["source_job_id"])
        with self.database.session() as session:
            rows = JobRepository(session).changes_for_rollback(source_job_id)
        self._progress(job_id, {"total": len(rows), "stage": "rollback"}, force=True)
        for row in rows:
            change = dict(row)
            change["old_value"] = json.loads(change.get("old_value") or "null")
            change["new_value"] = json.loads(change.get("new_value") or "null")
            change["old_locked"] = None if change.get("old_locked") is None else bool(change["old_locked"])
            try:
                current_value, _ = plex.read_field_state(change["rating_key"], change["field"])
                if current_value != change["new_value"]:
                    with self.database.transaction() as uow:
                        JobRepository(uow.session).update_change(
                            int(change["id"]),
                            {"rollback_status": "conflict", "rollback_error": "当前值已发生变化"},
                        )
                    self._log(job_id, f"跳过回滚冲突：{change['title']} · {change['field']}")
                    self._progress(job_id, {"processed_delta": 1, "errors_delta": 1})
                    continue
                plex.apply_recorded_change(change)
                with self.database.transaction() as uow:
                    JobRepository(uow.session).update_change(
                        int(change["id"]),
                        {"rollback_status": "rolled_back", "rolled_back_at": utcnow()},
                    )
                self._log(job_id, f"已回滚 {change['title']} 的 {change['field']}")
                self._progress(job_id, {"processed_delta": 1, "changes_delta": 1})
            except Exception as exc:
                with self.database.transaction() as uow:
                    JobRepository(uow.session).update_change(
                        int(change["id"]),
                        {"rollback_status": "failed", "rollback_error": str(exc)},
                    )
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
        with self.database.transaction() as uow:
            AutomationRepository(uow.session).record_collection_rule_run(
                rule_id, job_id, "preview" if preview else "reported", len(matches), utcnow()
            )
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
        with self.database.transaction() as uow:
            AutomationRepository(uow.session).replace_tag_suggestions(server_id, suggestions, now)
        self._log(job_id, f"已生成 {sum(len(values) for values in suggestions.values())} 条标签推荐。")

    def _run_continue_watching_preview_job(
        self, job_id: int, plex: PlexServer, payload: Dict[str, Any]
    ) -> None:
        library_id = int(payload["library_id"])
        server_id = int(self.get_job(job_id)["server_id"])
        now = utcnow()
        with self.database.transaction() as uow:
            run_id = AutomationRepository(uow.session).create_continue_run(
                job_id, server_id, library_id, "preview", now
            )
        try:
            candidates = plex.episode_progress_candidates(library_id)
            self._progress(
                job_id, {"stage": "continue_watching_preview", "total": len(candidates)}, force=True
            )
            with self.database.transaction() as uow:
                repository = AutomationRepository(uow.session)
                repository.add_continue_items(
                    [
                        {
                            "run_id": run_id,
                            "server_id": server_id,
                            "library_id": library_id,
                            "show_title": candidate["show_title"],
                            "show_rating_key": candidate["show_rating_key"],
                            "episode_rating_key": candidate["episode_rating_key"],
                            "season": int(candidate["season"]),
                            "episode": int(candidate["episode"]),
                            "episode_title": candidate["episode_title"],
                            "duration": int(candidate["duration"]),
                            "planned_offset": int(candidate["planned_offset"]),
                            "current_offset": int(candidate["current_offset"]),
                            "view_count": int(candidate["view_count"]),
                            "status": "candidate",
                            "created_at": now,
                            "updated_at": now,
                        }
                        for candidate in candidates
                    ]
                )
                repository.finish_continue_run(
                    run_id, status="succeeded", candidate_count=len(candidates), now=utcnow()
                )
            for candidate in candidates:
                self._log(
                    job_id,
                    f"候选：{candidate['show_title']} S{int(candidate['season']):02d}E{int(candidate['episode']):02d} {candidate['episode_title']}",
                )
                self._progress(job_id, {"processed_delta": 1})
            self._log(job_id, f"继续观看候选生成完成：{len(candidates)} 个。")
        except Exception:
            with self.database.transaction() as uow:
                AutomationRepository(uow.session).finish_continue_run(run_id, status="failed", now=utcnow())
            raise

    def _run_continue_watching_apply_job(
        self, job_id: int, plex: PlexServer, payload: Dict[str, Any]
    ) -> None:
        item_ids = [int(item_id) for item_id in (payload.get("item_ids") or [])]
        if not item_ids:
            raise ValueError("请选择要执行的候选剧集")
        server_id = int(self.get_job(job_id)["server_id"])
        now = utcnow()
        with self.database.transaction() as uow:
            repository = AutomationRepository(uow.session)
            source_items = repository.continue_candidates(item_ids, server_id)
            if not source_items:
                raise ValueError("未找到可执行的候选剧集")
            library_id = int(source_items[0]["library_id"])
            if any(int(item["library_id"]) != library_id for item in source_items):
                raise ValueError("候选剧集必须来自同一个媒体库")
            run_id = repository.create_continue_run(
                job_id, server_id, library_id, "apply", now, candidate_count=len(source_items)
            )
            copied_ids = repository.add_continue_items(
                [
                    {
                        "run_id": run_id,
                        "server_id": server_id,
                        "library_id": item["library_id"],
                        "show_title": item["show_title"],
                        "show_rating_key": item["show_rating_key"],
                        "episode_rating_key": item["episode_rating_key"],
                        "season": item["season"],
                        "episode": item["episode"],
                        "episode_title": item["episode_title"],
                        "duration": item["duration"],
                        "planned_offset": item["planned_offset"],
                        "current_offset": item["current_offset"],
                        "view_count": item["view_count"],
                        "status": "queued",
                        "created_at": now,
                        "updated_at": now,
                    }
                    for item in source_items
                ]
            )
            copied_items = list(zip(copied_ids, source_items, strict=True))
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
                    plex.set_playback_progress(str(item["episode_rating_key"]), int(item["planned_offset"]))
                    applied_count += 1
                    result = "已写入播放进度，Plex 首页可能需要刷新后显示。"
                    status = "applied"
                    self._log(
                        job_id,
                        f"已尝试加入继续观看：{item['show_title']} S{int(item['season']):02d}E{int(item['episode']):02d}",
                    )
                    self._progress(job_id, {"processed_delta": 1, "changes_delta": 1})
            except Exception as exc:
                error_count += 1
                result = str(exc)
                status = "failed"
                self._log(
                    job_id,
                    f"继续观看写入失败：{item['show_title']} S{int(item['season']):02d}E{int(item['episode']):02d}：{exc}",
                )
                self._progress(job_id, {"processed_delta": 1, "errors_delta": 1})
            with self.database.transaction() as uow:
                repository = AutomationRepository(uow.session)
                values = {"status": status, "result": result, "updated_at": utcnow()}
                repository.update_continue_item(copied_id, values)
                repository.update_continue_item(int(item["id"]), values)
                repository.update_continue_run(
                    run_id,
                    {"applied_count": applied_count, "error_count": error_count, "status": "running"},
                )
        with self.database.transaction() as uow:
            AutomationRepository(uow.session).finish_continue_run(
                run_id,
                status="succeeded",
                applied_count=applied_count,
                error_count=error_count,
                now=utcnow(),
            )
        self._log(job_id, f"继续观看执行完成：成功 {applied_count}，失败 {error_count}。")

    def _run_episode_audit_job(self, job_id: int, plex: PlexServer, payload: Dict[str, Any]) -> None:
        library_id = int(payload["library_id"])
        options = payload.get("options") or {}
        ignore_specials = bool(options.get("ignore_specials", True))
        ignore_future = bool(options.get("ignore_future", True))
        only_ended = bool(options.get("only_ended", False))
        with self.database.session() as session:
            tmdb_token = AuthRepository(session).setting("tmdb_api_key")
        tmdb = TMDBClient(tmdb_token)
        server_id = int(self.get_job(job_id)["server_id"])
        now = utcnow()
        with self.database.transaction() as uow:
            run_id = AutomationRepository(uow.session).create_episode_audit_run(
                job_id, server_id, library_id, json.dumps(options, ensure_ascii=False), now
            )
        try:
            shows = plex.list_show_items(library_id)
            self._progress(job_id, {"stage": "episode_audit", "total": len(shows)}, force=True)
            with self.database.transaction() as uow:
                AutomationRepository(uow.session).update_episode_audit_run(run_id, {"total_shows": len(shows)})
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
                        self._insert_episode_audit_item(
                            run_id, show, match, "ignored_show", {"reason": show_ignore.get("reason") or ""}
                        )
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
                    with self.database.transaction() as uow:
                        AutomationRepository(uow.session).update_episode_audit_run(
                            run_id,
                            {
                                "checked_shows": AutomationRepository(uow.session).episode_audit_run(run_id)["checked_shows"] + 1,
                                "missing_count": missing_count,
                                "unmatched_count": unmatched_count,
                                "ambiguous_count": ambiguous_count,
                                "ignored_count": ignored_count,
                            },
                        )
                    self._progress(job_id, {"processed_delta": 1})
            with self.database.transaction() as uow:
                AutomationRepository(uow.session).update_episode_audit_run(
                    run_id,
                    {
                        "status": "succeeded",
                        "missing_count": missing_count,
                        "unmatched_count": unmatched_count,
                        "ambiguous_count": ambiguous_count,
                        "ignored_count": ignored_count,
                        "finished_at": utcnow(),
                    },
                )
            complete_shows = max(0, len(shows) - len(shows_with_missing) - unmatched_count)
            self._log(
                job_id,
                f"缺集检查完成：总剧数 {len(shows)}，完整 {complete_shows} 部，有缺失 {len(shows_with_missing)} 部，"
                f"已有 {present_count} 集，缺失 {missing_count} 集，已忽略 {ignored_count} 项，未匹配 {unmatched_count} 部，低置信度 {ambiguous_count} 部。",
            )
        except Exception:
            with self.database.transaction() as uow:
                AutomationRepository(uow.session).update_episode_audit_run(
                    run_id, {"status": "failed", "finished_at": utcnow()}
                )
            raise

    def _match_tmdb_show(
        self,
        tmdb: TMDBClient,
        show: Dict[str, Any],
        server_id: int,
        library_id: int,
    ) -> Dict[str, Any]:
        rating_key = str(show.get("ratingKey") or "")
        with self.database.session() as session:
            override = AutomationRepository(session).episode_match_override(server_id, library_id, rating_key)
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
        with self.database.transaction() as uow:
            AutomationRepository(uow.session).add_episode_audit_item(
                {
                    "run_id": run_id,
                    "show_title": show.get("title") or "",
                    "plex_rating_key": str(show.get("ratingKey") or ""),
                    "tmdb_id": match.get("tmdb_id"),
                    "tmdb_title": match.get("tmdb_title") or "",
                    "match_source": match.get("match_source") or "",
                    "season": details.get("season"),
                    "episode": details.get("episode"),
                    "air_date": details.get("air_date") or "",
                    "status": status,
                    "details": json.dumps(details, ensure_ascii=False),
                    "created_at": utcnow(),
                }
            )

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
        with self.database.session() as session:
            rows = AutomationRepository(session).episode_ignores(
                server_id, library_id, plex_rating_key, tmdb_id, show_title
            )
        for data in rows:
            if data.get("season") is None and data.get("episode") is None:
                return data
            if (
                season is not None
                and episode is not None
                and int(data.get("season") or -1) == season
                and int(data.get("episode") or -1) == episode
            ):
                return data
        return None

    def preview_collection_rule(
        self, rule_id: int, plex: Optional[PlexServer] = None
    ) -> List[Dict[str, Any]]:
        with self.database.session() as session:
            repository = AutomationRepository(session)
            rule = repository.collection_rule(rule_id)
            if rule is None:
                raise ValueError("合集规则不存在")
            server = ServerRepository(session).get(int(rule["server_id"]))
            if server is None:
                raise ValueError("服务器不存在")
            server_data = {
                "name": server.name,
                "address": server.address,
                "token": server.token,
                "skip_libraries": server.skip_libraries,
                "pinyin_mode": server.pinyin_mode,
            }
        plex = plex or PlexServer(server_config_from_row(server_data, self.db_file), tags=load_tags(self.db_file))
        library = next(
            (item for item in plex.list_library() if int(item[0]) == int(rule["library_id"])), None
        )
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
        with self.database.session() as session:
            rows = AutomationRepository(session).enabled_notification_channels()
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







