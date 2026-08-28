from __future__ import annotations

import json
import stat
import threading
from types import SimpleNamespace

from clp_web import app as app_module
from clp_web import db as db_module
from clp_web import services as services_module


def make_client(tmp_path, monkeypatch):
    db_file = tmp_path / "clp.db"
    monkeypatch.setattr(db_module, "DB_FILE", db_file)
    monkeypatch.setattr(app_module, "DB_FILE", db_file)
    monkeypatch.setattr(services_module, "DB_FILE", db_file)
    monkeypatch.setattr(app_module, "connect", lambda: db_module.connect(db_file))
    monkeypatch.setattr(app_module, "init_db", lambda: db_module.init_db(db_file))
    app = app_module.create_app()
    app.testing = True
    return app, app.test_client(), db_file


def session_headers(client):
    data = client.get("/api/session").get_json()
    return data, {"X-CSRF-Token": data["csrf_token"]}


def setup_admin(client):
    _, headers = session_headers(client)
    response = client.post(
        "/api/setup",
        json={"username": "admin", "password": "secure-password"},
        headers=headers,
    )
    assert response.status_code == 200
    return {"X-CSRF-Token": response.get_json()["csrf_token"]}


def create_server(client, headers, name="Home"):
    response = client.post(
        "/api/servers",
        json={
            "name": name,
            "address": "http://127.0.0.1:32400",
            "token": "plex-secret",
            "pinyin_mode": "first_letter",
        },
        headers=headers,
    )
    assert response.status_code == 200
    return int(response.get_json()["id"])


def test_first_setup_csrf_session_secret_and_server_redaction(tmp_path, monkeypatch):
    app, client, db_file = make_client(tmp_path, monkeypatch)
    session_data, headers = session_headers(client)
    assert session_data["initialized"] is False
    assert client.post("/api/setup", json={"username": "a", "password": "secure-password"}).status_code == 403
    assert client.post(
        "/api/setup", json={"username": "a", "password": "short"}, headers=headers
    ).status_code == 400
    headers = setup_admin(client)
    server_id = create_server(client, headers)
    servers = client.get("/api/servers").get_json()
    assert servers[0]["token_configured"] is True
    assert "token" not in servers[0]
    assert f"/webhook/{server_id}/" in servers[0]["webhook_url"]
    update = client.put(
        f"/api/servers/{server_id}",
        json={
            "name": "Renamed",
            "address": "http://127.0.0.1:32400",
            "token": "",
            "pinyin_mode": "full_spell",
            "enabled": True,
        },
        headers=headers,
    )
    assert update.status_code == 200
    with db_module.connect(db_file) as db:
        assert db.execute("SELECT token FROM servers WHERE id = ?", (server_id,)).fetchone()["token"] == "plex-secret"
    secret_file = tmp_path / "session_secret"
    assert secret_file.exists()
    assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600
    app.extensions["task_manager"].shutdown()


def test_logout_login_rate_limit_and_password_change(tmp_path, monkeypatch):
    _, client, _ = make_client(tmp_path, monkeypatch)
    headers = setup_admin(client)
    changed = client.put(
        "/api/auth/password",
        json={"current_password": "secure-password", "new_password": "new-secure-password"},
        headers=headers,
    )
    assert changed.status_code == 200
    logout = client.post("/api/auth/logout", json={}, headers=headers)
    headers = {"X-CSRF-Token": logout.get_json()["csrf_token"]}
    for _ in range(5):
        assert client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "wrong"},
            headers=headers,
        ).status_code == 401
    assert client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "new-secure-password"},
        headers=headers,
    ).status_code == 429


def test_webhook_secret_normalization_and_rate_limit(tmp_path, monkeypatch):
    _, client, db_file = make_client(tmp_path, monkeypatch)
    headers = setup_admin(client)
    server_id = create_server(client, headers)
    server = client.get("/api/servers").get_json()[0]
    path = server["webhook_url"].replace("http://localhost", "")
    assert client.post(f"/webhook/{server_id}/bad", data={"payload": "{}"}).status_code == 404
    assert client.post(path, data={"payload": "not-json"}).status_code == 400
    payload = {
        "event": "library.new",
        "Metadata": {
            "ratingKey": "42",
            "librarySectionID": "7",
            "type": "movie",
            "title": "Example",
            "ignored": "secret-noise",
        },
    }
    assert client.post(path, data={"payload": json.dumps(payload)}).status_code == 200
    with db_module.connect(db_file) as db:
        event = json.loads(db.execute("SELECT payload FROM webhook_events").fetchone()["payload"])
        assert "ignored" not in event["Metadata"]
        assert db.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"] == 1
    for _ in range(59):
        client.post(path, data={"payload": json.dumps({"event": "play"})})
    assert client.post(path, data={"payload": json.dumps({"event": "play"})}).status_code == 429


def test_schedule_uses_standard_cron_semantics(tmp_path, monkeypatch):
    _, client, db_file = make_client(tmp_path, monkeypatch)
    headers = setup_admin(client)
    server_id = create_server(client, headers)
    describe = client.get("/api/schedules/describe?type=cron&value=0%2010%20*%20*%20sun")
    assert describe.get_json()["label"] == "每周日 10:00"
    response = client.post(
        "/api/schedules",
        json={
            "server_id": server_id,
            "name": "Sunday",
            "schedule_type": "cron",
            "schedule_value": "0 10 * * 0",
            "payload": {"type": "all", "payload": {}},
            "enabled": True,
        },
        headers=headers,
    )
    assert response.status_code == 200
    with db_module.connect(db_file) as db:
        row = db.execute("SELECT next_run_at FROM schedules").fetchone()
        assert row["next_run_at"].endswith("Z")


def test_job_cancel_retry_tags_and_match_override(tmp_path, monkeypatch):
    _, client, db_file = make_client(tmp_path, monkeypatch)
    headers = setup_admin(client)
    server_id = create_server(client, headers)
    job_id = client.post(
        "/api/jobs",
        json={"type": "all", "server_id": server_id},
        headers=headers,
    ).get_json()["id"]
    cancelled = client.post(f"/api/jobs/{job_id}/cancel", json={}, headers=headers)
    assert cancelled.get_json()["status"] == "cancelled"
    retry_id = client.post(f"/api/jobs/{job_id}/retry", json={}, headers=headers).get_json()["id"]
    assert retry_id != job_id
    assert client.post("/api/tags", json={"Action": "动作"}, headers=headers).status_code == 200
    assert client.get("/api/tags").get_json() == {"Action": "动作"}
    override = {
        "server_id": server_id,
        "library_id": 7,
        "plex_rating_key": "show-1",
        "tmdb_id": 123,
    }
    assert client.put("/api/episode-match-overrides", json=override, headers=headers).status_code == 200
    assert client.get(f"/api/episode-match-overrides?server_id={server_id}").get_json()[0]["tmdb_id"] == 123
    with db_module.connect(db_file) as db:
        db.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (retry_id,))
        db.commit()
    response = client.post(f"/api/jobs/{retry_id}/cancel", json={}, headers=headers)
    assert response.get_json()["status"] == "cancelling"


def test_health_reports_worker_offline(tmp_path, monkeypatch):
    _, client, _ = make_client(tmp_path, monkeypatch)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.get_json()["worker"]["online"] is False


def test_overview_media_and_thumbnail_proxy_are_authenticated_and_scoped(tmp_path, monkeypatch):
    _, client, _ = make_client(tmp_path, monkeypatch)
    assert client.get("/api/overview/media").status_code == 403
    headers = setup_admin(client)
    server_id = create_server(client, headers)

    class FakePlex:
        def __init__(self, *_args, **_kwargs):
            return None

        def list_recent_media(self, _limit):
            return [
                {
                    "rating_key": "42",
                    "title": "示例电影",
                    "year": 2026,
                    "type": "movie",
                    "thumb": "/library/metadata/42/thumb/987",
                    "added_at": 200,
                }
            ]

        def fetch_media_image(self, path):
            assert path == "/library/metadata/42/thumb/987"
            return SimpleNamespace(
                headers={"Content-Type": "image/png"},
                content=b"png-bytes",
            )

    monkeypatch.setattr(app_module, "PlexServer", FakePlex)
    media_response = client.get("/api/overview/media?limit=12")
    assert media_response.status_code == 200
    payload = media_response.get_json()
    assert payload["warnings"] == []
    assert payload["items"][0]["title"] == "示例电影"
    assert "plex-secret" not in json.dumps(payload)
    image_path = payload["items"][0]["image_url"].split("?path=", 1)[1]

    image_response = client.get(f"/api/servers/{server_id}/media-image?path={image_path}")
    assert image_response.status_code == 200
    assert image_response.data == b"png-bytes"
    assert image_response.headers["Content-Type"].startswith("image/png")
    assert image_response.headers["Cache-Control"] == "private, max-age=3600"
    assert client.get(
        f"/api/servers/{server_id}/media-image?path=https://example.com/image.png"
    ).get_json()["code"] == "invalid_media_path"
    assert client.get(
        f"/api/servers/{server_id}/media-image?path=/library/metadata/42/../secret"
    ).get_json()["code"] == "invalid_media_path"


def test_error_codes_and_public_change_statuses(tmp_path, monkeypatch):
    _, client, db_file = make_client(tmp_path, monkeypatch)
    headers = setup_admin(client)
    server_id = create_server(client, headers)
    missing = client.post("/api/servers/999/test", json={}, headers=headers)
    assert missing.get_json() == {"error": "服务器不存在", "code": "server_not_found"}
    bad_webhook = client.post(f"/webhook/{server_id}/wrong", data={"payload": "{}"})
    assert bad_webhook.get_json()["code"] == "webhook_not_found"

    now = db_module.utcnow()
    with db_module.connect(db_file) as db:
        job_id = int(
            db.execute(
                "INSERT INTO jobs (type, server_id, status, created_at) VALUES ('all', ?, 'succeeded', ?)",
                (server_id, now),
            ).lastrowid
        )
        change_set_id = int(
            db.execute(
                "INSERT INTO change_sets (job_id, server_id, mode, created_at) VALUES (?, ?, 'apply', ?)",
                (job_id, server_id, now),
            ).lastrowid
        )
        statuses = [
            ("pending", "", "1"),
            ("applied", "", "2"),
            ("conflict", "", "3"),
            ("applied", "rolled_back", "4"),
        ]
        for apply_status, rollback_status, rating_key in statuses:
            db.execute(
                """
                INSERT INTO changes (
                    change_set_id, job_id, server_id, rating_key, field,
                    apply_status, rollback_status, created_at
                ) VALUES (?, ?, ?, ?, 'titleSort', ?, ?, ?)
                """,
                (
                    change_set_id,
                    job_id,
                    server_id,
                    rating_key,
                    apply_status,
                    rollback_status,
                    now,
                ),
            )
        db.commit()
    changes = client.get(f"/api/jobs/{job_id}/changes").get_json()
    assert [change["status"] for change in changes] == [
        "pending",
        "applied",
        "conflict",
        "rolled_back",
    ]


def test_first_setup_is_atomic_and_deleted_user_invalidates_session(tmp_path, monkeypatch):
    app, client, db_file = make_client(tmp_path, monkeypatch)
    clients = [app.test_client(), app.test_client()]
    csrf_tokens = [entry.get("/api/session").get_json()["csrf_token"] for entry in clients]
    barrier = threading.Barrier(2)
    statuses = [0, 0]

    def setup(index: int) -> None:
        barrier.wait()
        response = clients[index].post(
            "/api/setup",
            json={"username": f"admin-{index}", "password": "secure-password"},
            headers={"X-CSRF-Token": csrf_tokens[index]},
        )
        statuses[index] = response.status_code

    threads = [threading.Thread(target=setup, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(statuses) == [200, 409]

    authenticated = clients[statuses.index(200)]
    with db_module.connect(db_file) as db:
        db.execute("DELETE FROM users")
        db.commit()
    response = authenticated.get("/api/servers")
    assert response.status_code == 403
    assert response.get_json()["code"] == "not_initialized"

    setup_admin(client)
    with client.session_transaction() as session:
        session["user_id"] = 999999
    response = client.get("/api/servers")
    assert response.status_code == 401
    assert response.get_json()["code"] == "unauthorized"
