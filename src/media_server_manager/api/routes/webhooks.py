# mypy: ignore-errors
# ruff: noqa: F821
from __future__ import annotations

from typing import Any

from flask import Blueprint

from ..dependencies import get_database


def register_routes(blueprint: Blueprint, context: dict[str, Any]) -> None:
    """Webhook and notification routes."""
    # Route functions are registered here, while the application factory
    # supplies its request/session dependencies through this explicit context.
    from media_server_manager.api import app_factory as _factory

    globals().update(vars(_factory))
    globals().update(context)
    webhooks_bp = blueprint
    @webhooks_bp.get("/api/webhook-events")
    @login_required
    def list_webhook_events():
        with get_database().session() as db:
            events = AutomationRepository(db).webhook_events()
        for event in events:
            event["payload"] = decode_payload(event.get("payload", "{}"))
        return jsonify(events)

    @webhooks_bp.get("/api/webhook-rules")
    @login_required
    def list_webhook_rules():
        with get_database().session() as db:
            rows = AutomationRepository(db).list_webhook_rules()
        return jsonify(rows)

    @webhooks_bp.post("/api/webhook-rules")
    @login_required
    def save_webhook_rule():
        data = request.get_json(force=True)
        now = utcnow()
        with get_database().transaction() as uow:
            AutomationRepository(uow.session).save_webhook_rule(data, now)
        return jsonify({"ok": True})

    @webhooks_bp.delete("/api/webhook-rules/<int:rule_id>")
    @login_required
    def delete_webhook_rule(rule_id: int):
        with get_database().transaction() as uow:
            AutomationRepository(uow.session).delete_webhook_rule(rule_id)
        return jsonify({"ok": True})

    @webhooks_bp.get("/api/notifications/channels")
    @login_required
    def list_notification_channels():
        with get_database().session() as db:
            channels = AutomationRepository(db).list_notification_channels()
        for channel in channels:
            channel["events"] = json.loads(channel.get("events") or "[]")
        return jsonify(channels)

    @webhooks_bp.post("/api/notifications/channels")
    @login_required
    def save_notification_channel():
        data = request.get_json(force=True)
        now = utcnow()
        events = json.dumps(data.get("events") or [], ensure_ascii=False)
        with get_database().transaction() as uow:
            AutomationRepository(uow.session).save_notification_channel(data, now, events)
        return jsonify({"ok": True})

    @webhooks_bp.delete("/api/notifications/channels/<int:channel_id>")
    @login_required
    def delete_notification_channel(channel_id: int):
        with get_database().transaction() as uow:
            AutomationRepository(uow.session).delete_notification_channel(channel_id)
        return jsonify({"ok": True})

    @webhooks_bp.post("/api/notifications/channels/<int:channel_id>/test")
    @login_required
    def test_notification_channel(channel_id: int):
        with get_database().session() as db:
            row = AutomationRepository(db).notification_channel(channel_id)
        if row is None:
            return api_error("通知渠道不存在", "notification_channel_not_found", 404)
        try:
            import requests

            requests.post(
                row["url"],
                json={"event": "notification_test", "payload": {"message": "MSM 通知测试"}},
                timeout=10,
            ).raise_for_status()
        except Exception as exc:
            return api_error(str(exc), "notification_test_failed", 400)
        return jsonify({"ok": True})

    @webhooks_bp.post("/webhook/<int:server_id>/<secret>")
    def webhook(server_id: int, secret: str):
        with get_database().session() as db:
            server = AutomationRepository(db).enabled_webhook_server(server_id)
            webhook_secret = server.webhook_secret if server is not None else ""
        if server is None or not hmac.compare_digest(webhook_secret, secret):
            return api_error("Webhook 地址无效", "webhook_not_found", 404)
        source_ip = client_ip()
        cutoff = (
            (datetime.now(timezone.utc) - timedelta(minutes=1))
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
        with get_database().transaction() as uow:
            if not AutomationRepository(uow.session).allow_webhook(server_id, source_ip, cutoff, utcnow()):
                return api_error("Webhook 请求过于频繁", "webhook_rate_limited", 429)
        payload = request.form.get("payload")
        if not payload:
            return api_error("Webhook 缺少 payload", "missing_webhook_payload", 400)
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return api_error("Webhook payload 不是有效 JSON", "invalid_webhook_payload", 400)
        if not isinstance(data, dict):
            return api_error("Webhook payload 必须是 JSON 对象", "invalid_webhook_payload", 400)
        event_name = str(data.get("event") or "unknown")[:100]
        raw_metadata = data.get("Metadata") or {}
        if not isinstance(raw_metadata, dict):
            raw_metadata = {}
        allowed_keys = {
            "ratingKey",
            "librarySectionID",
            "type",
            "title",
            "guid",
            "grandparentRatingKey",
            "parentRatingKey",
        }
        metadata = {key: raw_metadata[key] for key in allowed_keys if key in raw_metadata}
        collections = raw_metadata.get("Collection") or []
        if isinstance(collections, list):
            metadata["Collection"] = [
                {"guid": str(item.get("guid"))[:500]}
                for item in collections[:100]
                if isinstance(item, dict) and item.get("guid")
            ]
        normalized = {"event": event_name, "Metadata": metadata}
        summary = str(metadata.get("title") or metadata.get("ratingKey") or event_name)[:500]
        # Event, rules, and the debounce queue share one SQLAlchemy transaction.
        with get_database().transaction() as uow:
            repository = AutomationRepository(uow.session)
            event_id = repository.record_webhook(server_id, event_name, summary, json.dumps(normalized, ensure_ascii=False), source_ip, utcnow())
            rules = repository.matching_webhook_rules(server_id, event_name)
            recheck_result = RecheckRepository(uow.session).request(server_id, event_name, metadata)
            repository.update_webhook_result(event_id, recheck_result)
        should_localize = (not rules and event_name == "library.new") or any(
            rule["action"] == "localize" for rule in rules
        )
        if should_localize and metadata:
            manager.create_job("webhook", server_id, {"metadata": metadata, "webhook_event_id": event_id})
        if any(rule["action"] == "notify" for rule in rules):
            manager.create_job(
                "notification_event",
                server_id,
                {"event": event_name, "summary": summary, "webhook_event_id": event_id},
            )
        return "OK", 200

