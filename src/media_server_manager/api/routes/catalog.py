# mypy: ignore-errors
# ruff: noqa: F821
from __future__ import annotations

from typing import Any

from flask import Blueprint

from ..dependencies import get_database


def register_routes(blueprint: Blueprint, context: dict[str, Any]) -> None:
    """Catalog and catalog-related automation routes."""
    # Route functions are registered here, while the application factory
    # supplies its request/session dependencies through this explicit context.
    from media_server_manager.api import app_factory as _factory

    globals().update(vars(_factory))
    globals().update(context)
    catalog_bp = blueprint
    @catalog_bp.post("/api/maintenance/jobs")
    @login_required
    def create_maintenance_job():
        data = request.get_json(force=True)
        job_id = manager.create_job("maintenance", int(data["server_id"]), data)
        return jsonify({"id": job_id})

    @catalog_bp.post("/api/continue-watching/preview")
    @login_required
    def create_continue_watching_preview():
        data = request.get_json(force=True)
        job_id = manager.create_job(
            "continue_watching_preview",
            int(data["server_id"]),
            {"library_id": int(data["library_id"])},
        )
        return jsonify({"id": job_id, "message": "已创建继续观看候选预览任务。"})

    @catalog_bp.get("/api/continue-watching/runs")
    @login_required
    def list_continue_watching_runs():
        with get_database().session() as db:
            rows = AutomationRepository(db).continue_runs()
        return jsonify(rows)

    @catalog_bp.get("/api/continue-watching/runs/<int:run_id>/items")
    @login_required
    def list_continue_watching_items(run_id: int):
        with get_database().session() as db:
            rows = AutomationRepository(db).continue_items(run_id)
        return jsonify(rows)

    @catalog_bp.post("/api/continue-watching/apply")
    @login_required
    def apply_continue_watching_items():
        data = request.get_json(force=True)
        item_ids = [int(item_id) for item_id in (data.get("item_ids") or [])]
        if not item_ids:
            return api_error("请选择要执行的候选剧集", "missing_candidate_items", 400)
        with get_database().session() as db:
            rows = AutomationRepository(db).candidate_servers(item_ids)
        if len(rows) != 1:
            return api_error("候选剧集必须有效且来自同一服务器和媒体库", "invalid_candidates", 400)
        server_id = int(rows[0]["server_id"])
        job_id = manager.create_job("continue_watching_apply", server_id, {"item_ids": item_ids})
        return jsonify({"id": job_id, "message": "已创建继续观看执行任务。"})

    @catalog_bp.get("/api/settings/tmdb")
    @login_required
    def get_tmdb_settings():
        with get_database().session() as db:
            configured = bool(AuthRepository(db).setting("tmdb_api_key"))
        return jsonify({"configured": configured})

    @catalog_bp.post("/api/settings/tmdb")
    @login_required
    def save_tmdb_settings():
        data = request.get_json(force=True)
        api_key = (data.get("api_key") or "").strip()
        if not api_key:
            return api_error("TMDB API Key 不能为空", "missing_tmdb_api_key", 400)
        with get_database().transaction() as uow:
            AuthRepository(uow.session).set_setting("tmdb_api_key", api_key)
        return jsonify({"ok": True})

    @catalog_bp.get("/api/catalog/items")
    @login_required
    def list_catalog_items():
        media_type = (request.args.get("media_type") or "").strip()
        status = (request.args.get("status") or "").strip()
        server_id = request.args.get("server_id")
        q = (request.args.get("q") or "").strip()
        page = max(1, int(request.args.get("page", "1")))
        size = max(1, min(100, int(request.args.get("page_size", "24"))))
        with get_database().session() as db:
            rows, total = CatalogRepository(db).list_items(media_type, q, page, size, int(server_id) if server_id else None, status)
        return jsonify({"items": rows, "page": page, "page_size": size, "total": total})

    @catalog_bp.get("/api/catalog/items/<media_type>/<int:tmdb_id>")
    @login_required
    def catalog_item_detail(media_type: str, tmdb_id: int):
        with get_database().session() as db:
            row, children = CatalogRepository(db).detail(media_type, tmdb_id)
        if row is None:
            return api_error("目录项目不存在", "catalog_item_not_found", 404)
        item = dict(row)
        item["raw_json"] = decode_payload(item.get("raw_json"))
        item["children"] = children
        return jsonify(item)

    @catalog_bp.get("/api/catalog/stats")
    @login_required
    def catalog_stats():
        with get_database().session() as db:
            counts, statuses = CatalogRepository(db).stats()
        return jsonify({"items": counts, "matches": statuses})

    @catalog_bp.post("/api/catalog/sync")
    @login_required
    def create_catalog_sync():
        data = request.get_json(force=True) or {}
        with get_database().session() as db:
            tmdb_configured = bool(AuthRepository(db).setting("tmdb_api_key"))
        if not tmdb_configured:
            return api_error("请先保存 TMDB API Read Access Token", "missing_tmdb_token", 400)
        server_id = int(data.get("server_id") or 0)
        if not server_id:
            with get_database().session() as db:
                enabled = ServerManagementService(db).enabled_servers()
            if not enabled:
                return api_error("请先配置至少一台 Plex 服务器", "server_required", 400)
            server_id = int(enabled[0].id)
        payload = {
            "media_types": data.get("media_types") or ["movie", "tv"],
            "filters": data.get("filters") or {},
            "max_pages": data.get("max_pages") or 1,
        }
        job_id = manager.create_job("tmdb_catalog_sync", server_id, payload)
        return jsonify({"id": job_id})

    @catalog_bp.post("/api/catalog/plex-inventory/sync")
    @login_required
    def create_inventory_sync():
        data = request.get_json(force=True) or {}
        server_id = int(data.get("server_id") or 0)
        job_id = manager.create_job(
            "plex_inventory_sync", server_id, {"server_id": server_id, "library_ids": data.get("library_ids")}
        )
        return jsonify({"id": job_id})

    @catalog_bp.get("/api/episode-match-overrides")
    @login_required
    def list_episode_match_overrides():
        with get_database().session() as db:
            rows = AutomationRepository(db).episode_match_overrides(
                int(request.args["server_id"]) if request.args.get("server_id") else None,
                int(request.args["library_id"]) if request.args.get("library_id") else None,
            )
        return jsonify(rows)

    @catalog_bp.put("/api/episode-match-overrides")
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
        with get_database().transaction() as uow:
            AutomationRepository(uow.session).save_episode_match_override(server_id, library_id, rating_key, tmdb_id, now)
        return jsonify({"ok": True})

    @catalog_bp.delete("/api/episode-match-overrides/<int:server_id>/<int:library_id>/<rating_key>")
    @login_required
    def delete_episode_match_override(server_id: int, library_id: int, rating_key: str):
        with get_database().transaction() as uow:
            AutomationRepository(uow.session).delete_episode_match_override(server_id, library_id, rating_key)
        return jsonify({"ok": True})

    @catalog_bp.post("/api/episode-audits")
    @login_required
    def create_episode_audit():
        data = request.get_json(force=True)
        with get_database().session() as db:
            tmdb_configured = bool(AuthRepository(db).setting("tmdb_api_key"))
        if not tmdb_configured:
            return api_error("请先保存 TMDB API Key", "missing_tmdb_api_key", 400)
        server_id = int(data["server_id"])
        library_id = int(data["library_id"])
        options = data.get("options") or {}
        with get_database().session() as db:
            server = AutomationRepository(db).enabled_webhook_server(server_id)
        if server is None:
            return api_error("服务器不存在或已禁用", "server_not_found", 404)
        job_id = manager.create_job(
            "episode_audit",
            server_id,
            {"library_id": library_id, "options": options},
        )
        return jsonify({"id": job_id})

    @catalog_bp.get("/api/episode-audits")
    @login_required
    def list_episode_audits():
        with get_database().session() as db:
            runs = AutomationRepository(db).episode_audit_runs()
        for run in runs:
            run["options"] = decode_payload(run.get("options", "{}"))
        return jsonify(runs)

    @catalog_bp.get("/api/episode-audits/<int:run_id>")
    @login_required
    def get_episode_audit(run_id: int):
        with get_database().session() as db:
            row = AutomationRepository(db).episode_audit_run(run_id)
        if row is None:
            return api_error("缺集检查记录不存在", "episode_audit_not_found", 404)
        data = dict(row)
        data["options"] = decode_payload(data.get("options", "{}"))
        return jsonify(data)

    @catalog_bp.get("/api/episode-audits/<int:run_id>/items")
    @login_required
    def list_episode_audit_items(run_id: int):
        status = (request.args.get("status") or "").strip()
        with get_database().session() as db:
            items = AutomationRepository(db).episode_audit_items(run_id, status, (request.args.get("q") or "").strip())
        for item in items:
            item["details"] = decode_payload(item.get("details", "{}"))
        return jsonify(items)

    @catalog_bp.get("/api/episode-audits/<int:run_id>/report")
    @login_required
    def get_episode_audit_report(run_id: int):
        with get_database().session() as db:
            repository = AutomationRepository(db)
            run = repository.episode_audit_run(run_id)
            rows = repository.episode_audit_items(run_id)
        if run is None:
            return api_error("缺集检查记录不存在", "episode_audit_not_found", 404)
        groups: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            item["details"] = decode_payload(item.get("details", "{}"))
            key = (
                item.get("plex_rating_key")
                or str(item.get("tmdb_id") or "")
                or item.get("show_title")
                or str(item["id"])
            )
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
                season = group["seasons"].setdefault(
                    season_key, {"season": formatted["season"], "episodes": []}
                )
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
            group["seasons"] = sorted(
                group["seasons"].values(), key=lambda season: int(season["season"] or 0)
            )
            for season in group["seasons"]:
                season["episodes"].sort(key=lambda episode: int(episode["episode"] or 0))
            if (
                group["missing_count"]
                or group["present_count"]
                or group["ignored_count"]
                or group["unmatched"]
                or group["ambiguous"]
            ):
                report_groups.append(group)
        report_groups.sort(
            key=lambda group: (0 if group["missing_count"] else 1, group["show_title"].lower())
        )
        run_data = dict(run)
        run_data["options"] = decode_payload(run_data.get("options", "{}"))
        legacy_summary_only = bool(report_groups) and not any(
            not group["legacy_summary_only"] for group in report_groups
        )
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
            "label": f"S{int(item['season']):02d}E{int(item['episode']):02d}"
            if item.get("season") is not None and item.get("episode") is not None
            else "整部剧",
            "air_date": item.get("air_date") or "",
            "title": details.get("episode_title") or "",
            "reason": details.get("ignore_reason") or details.get("reason") or "",
            "status": item.get("status") or "",
            "item_id": item.get("id"),
        }

    @catalog_bp.get("/api/episode-audit-ignores")
    @login_required
    def list_episode_audit_ignores():
        with get_database().session() as db:
            rows = AutomationRepository(db).episode_audit_ignores(
                int(request.args["server_id"]) if request.args.get("server_id") else None,
                int(request.args["library_id"]) if request.args.get("library_id") else None,
            )
        return jsonify(rows)

    @catalog_bp.post("/api/episode-audit-ignores")
    @login_required
    def create_episode_audit_ignore():
        data = request.get_json(force=True)
        ignore_id = save_episode_ignore(data)
        return jsonify({"id": ignore_id})

    @catalog_bp.post("/api/episode-audit-ignores/from-item")
    @login_required
    def create_episode_audit_ignore_from_item():
        data = request.get_json(force=True)
        item_id = int(data["item_id"])
        scope = data.get("scope") or "episode"
        reason = (data.get("reason") or "").strip()
        with get_database().session() as db:
            row = AutomationRepository(db).episode_audit_item_with_run(item_id)
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

    @catalog_bp.delete("/api/episode-audit-ignores/<int:ignore_id>")
    @login_required
    def delete_episode_audit_ignore(ignore_id: int):
        with get_database().transaction() as uow:
            AutomationRepository(uow.session).delete_episode_ignore(ignore_id)
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
        with get_database().transaction() as uow:
            return AutomationRepository(uow.session).save_episode_ignore(
                {"server_id": server_id, "library_id": library_id, "show_title": show_title, "plex_rating_key": plex_rating_key, "tmdb_id": tmdb_id, "season": season, "episode": episode, "reason": reason},
                now,
            )

    def mark_episode_items_ignored(item: dict[str, Any], scope: str, reason: str) -> None:
        with get_database().transaction() as uow:
            repository = AutomationRepository(uow.session)
            repository.mark_episode_items_ignored(item, scope, reason)
            repository.refresh_episode_audit_counts(item["run_id"])

    @catalog_bp.get("/api/collection-rules")
    @login_required
    def list_collection_rules():
        with get_database().session() as db:
            rows = AutomationRepository(db).collection_rules()
        return jsonify(rows)

    @catalog_bp.post("/api/collection-rules")
    @login_required
    def create_collection_rule():
        data = request.get_json(force=True)
        now = utcnow()
        with get_database().transaction() as uow:
            rule_id = AutomationRepository(uow.session).save_collection_rule(data, now)
        return jsonify({"id": rule_id})

    @catalog_bp.put("/api/collection-rules/<int:rule_id>")
    @login_required
    def update_collection_rule(rule_id: int):
        data = request.get_json(force=True)
        with get_database().transaction() as uow:
            AutomationRepository(uow.session).save_collection_rule(data, utcnow(), rule_id)
        return jsonify({"ok": True})

    @catalog_bp.delete("/api/collection-rules/<int:rule_id>")
    @login_required
    def delete_collection_rule(rule_id: int):
        with get_database().transaction() as uow:
            AutomationRepository(uow.session).delete_collection_rule(rule_id)
        return jsonify({"ok": True})

    @catalog_bp.post("/api/collection-rules/<int:rule_id>/preview")
    @login_required
    def preview_collection_rule(rule_id: int):
        with get_database().session() as db:
            rule = AutomationRepository(db).collection_rule(rule_id)
        if rule is None:
            return api_error("合集规则不存在", "collection_rule_not_found", 404)
        job_id = manager.create_job(
            "collection_rule", int(rule["server_id"]), {"rule_id": rule_id, "preview": True}
        )
        return jsonify({"id": job_id})

    @catalog_bp.post("/api/collection-rules/<int:rule_id>/run")
    @login_required
    def run_collection_rule(rule_id: int):
        data = request.get_json(silent=True) or {}
        with get_database().session() as db:
            rule = AutomationRepository(db).collection_rule(rule_id)
        if rule is None:
            return api_error("合集规则不存在", "collection_rule_not_found", 404)
        job_id = manager.create_job(
            "collection_rule",
            int(rule["server_id"]),
            {"rule_id": rule_id, "preview": bool(data.get("preview"))},
        )
        return jsonify({"id": job_id})

