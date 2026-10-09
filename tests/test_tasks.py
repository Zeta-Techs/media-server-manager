from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from media_server_manager_web import tasks as tasks_module
from media_server_manager_web.db import connect, init_db, new_secret, utcnow
from media_server_manager_web.services import JobQueue
from media_server_manager_web.tasks import TaskManager


def seed_server(db_file):
    now = utcnow()
    with connect(db_file) as db:
        server_id = db.execute(
            """
            INSERT INTO servers (name, address, token, webhook_secret, created_at, updated_at)
            VALUES ('Home', 'http://plex.local', 'token', ?, ?, ?)
            """,
            (new_secret(), now, now),
        ).lastrowid
        db.commit()
    return int(server_id)


def seed_preview(db_file, server_id, current='"旧"', target='"JIU"'):
    now = utcnow()
    with connect(db_file) as db:
        job_id = db.execute(
            """
            INSERT INTO jobs (type, server_id, status, payload, created_at, finished_at)
            VALUES ('localize', ?, 'succeeded', '{"mode":"dry_run"}', ?, ?)
            """,
            (server_id, now, now),
        ).lastrowid
        change_set_id = db.execute(
            "INSERT INTO change_sets (job_id, server_id, mode, created_at) VALUES (?, ?, 'dry_run', ?)",
            (job_id, server_id, now),
        ).lastrowid
        db.execute(
            """
            INSERT INTO changes (
                change_set_id, job_id, server_id, library_id, media_type, rating_key,
                title, field, old_value, new_value, old_locked, created_at
            ) VALUES (?, ?, ?, 1, 'movie', '42', '旧标题', 'titleSort', ?, ?, 0, ?)
            """,
            (change_set_id, job_id, server_id, current, target, now),
        )
        db.commit()
    return int(job_id), int(change_set_id)


class FakePlex:
    def __init__(self, current):
        self.current = current
        self.applied = []

    def read_field_state(self, rating_key, field):
        return self.current, False

    def apply_change_value(self, change, value, locked):
        self.applied.append((change["rating_key"], value, locked))
        self.current = value

    def apply_recorded_change(self, change):
        self.apply_change_value(change, change["old_value"], change["old_locked"])


def test_apply_preview_uses_exact_snapshot(tmp_path):
    db_file = tmp_path / "media_server_manager.db"
    init_db(db_file)
    server_id = seed_server(db_file)
    preview_job, _ = seed_preview(db_file, server_id)
    queue = JobQueue(db_file)
    apply_job = queue.apply_preview_job(preview_job)
    plex = FakePlex("旧")
    runner = TaskManager(db_file)
    runner._run_apply_change_set_job(apply_job, plex, queue.get_job(apply_job)["payload"])
    assert plex.applied == [("42", "JIU", True)]
    with connect(db_file) as db:
        row = db.execute(
            "SELECT apply_status, applied FROM changes WHERE job_id = ?", (apply_job,)
        ).fetchone()
        assert dict(row) == {"apply_status": "applied", "applied": 1}


def test_apply_preview_marks_conflict_without_write(tmp_path):
    db_file = tmp_path / "media_server_manager.db"
    init_db(db_file)
    server_id = seed_server(db_file)
    preview_job, _ = seed_preview(db_file, server_id)
    queue = JobQueue(db_file)
    apply_job = queue.apply_preview_job(preview_job)
    plex = FakePlex("用户已修改")
    runner = TaskManager(db_file)
    runner._run_apply_change_set_job(apply_job, plex, queue.get_job(apply_job)["payload"])
    assert plex.applied == []
    with connect(db_file) as db:
        assert (
            db.execute("SELECT apply_status FROM changes WHERE job_id = ?", (apply_job,)).fetchone()[
                "apply_status"
            ]
            == "conflict"
        )


def test_rollback_requires_current_new_value_and_restores_lock(tmp_path):
    db_file = tmp_path / "media_server_manager.db"
    init_db(db_file)
    server_id = seed_server(db_file)
    now = utcnow()
    with connect(db_file) as db:
        source_job = db.execute(
            "INSERT INTO jobs (type, server_id, status, created_at, finished_at) VALUES ('localize', ?, 'succeeded', ?, ?)",
            (server_id, now, now),
        ).lastrowid
        change_set = db.execute(
            "INSERT INTO change_sets (job_id, server_id, mode, created_at) VALUES (?, ?, 'apply', ?)",
            (source_job, server_id, now),
        ).lastrowid
        db.execute(
            """
            INSERT INTO changes (
                change_set_id, job_id, server_id, library_id, media_type, rating_key,
                title, field, old_value, new_value, old_locked, apply_status, applied, created_at
            ) VALUES (?, ?, ?, 1, 'movie', '42', '标题', 'titleSort', '"旧"', '"新"', 0, 'applied', 1, ?)
            """,
            (change_set, source_job, server_id, now),
        )
        rollback_job = db.execute(
            "INSERT INTO jobs (type, server_id, status, payload, created_at) VALUES ('rollback', ?, 'running', ?, ?)",
            (server_id, json.dumps({"source_job_id": source_job}), now),
        ).lastrowid
        db.commit()
    runner = TaskManager(db_file)
    plex = FakePlex("新")
    runner._run_rollback_job(rollback_job, plex, {"source_job_id": source_job})
    assert plex.applied == [("42", "旧", False)]
    with connect(db_file) as db:
        assert (
            db.execute("SELECT rollback_status FROM changes WHERE job_id = ?", (source_job,)).fetchone()[
                "rollback_status"
            ]
            == "rolled_back"
        )


def test_manual_tmdb_override_has_priority(tmp_path):
    db_file = tmp_path / "media_server_manager.db"
    init_db(db_file)
    server_id = seed_server(db_file)
    now = utcnow()
    with connect(db_file) as db:
        db.execute(
            """
            INSERT INTO episode_match_overrides (
                server_id, library_id, plex_rating_key, tmdb_id, created_at, updated_at
            ) VALUES (?, 7, 'show-1', 999, ?, ?)
            """,
            (server_id, now, now),
        )
        db.commit()
    runner = TaskManager(db_file)

    class NoNetworkTmdb:
        def search_tv(self, *_args, **_kwargs):
            raise AssertionError("manual override should avoid search")

    result = runner._match_tmdb_show(
        NoNetworkTmdb(), {"ratingKey": "show-1", "title": "Example"}, server_id, 7
    )
    assert result == {"status": "matched", "tmdb_id": 999, "match_source": "manual_override"}


def test_tmdb_unique_ambiguous_and_future_episode_handling(tmp_path):
    db_file = tmp_path / "media_server_manager.db"
    init_db(db_file)
    server_id = seed_server(db_file)
    runner = TaskManager(db_file)

    class FakeTmdb:
        results = []

        def find_tv_by_external_id(self, external_id, source):
            if source == "tvdb" and external_id == "123":
                return {"id": 10, "name": "External match"}
            return None

        def search_tv(self, _title, _year=None):
            return self.results

    tmdb = FakeTmdb()
    external = runner._match_tmdb_show(
        tmdb,
        {"ratingKey": "external", "title": "Example", "Guid": [{"id": "tvdb://123"}]},
        server_id,
        7,
    )
    assert external["tmdb_id"] == 10
    assert external["match_source"] == "tvdb_find"

    tmdb.results = [{"id": 20, "name": "Unique"}]
    unique = runner._match_tmdb_show(
        tmdb, {"ratingKey": "unique", "title": "Example", "year": 2020}, server_id, 7
    )
    assert unique["status"] == "matched"
    assert unique["tmdb_id"] == 20

    tmdb.results = [
        {"id": 30, "name": "First", "first_air_date": "2020-01-01"},
        {"id": 31, "name": "Second", "first_air_date": "2021-01-01"},
    ]
    ambiguous = runner._match_tmdb_show(tmdb, {"ratingKey": "ambiguous", "title": "Example"}, server_id, 7)
    assert ambiguous["status"] == "ambiguous"
    assert [item["id"] for item in ambiguous["candidates"]] == [30, 31]

    assert runner._is_future_episode((date.today() + timedelta(days=1)).isoformat())
    assert not runner._is_future_episode(date.today().isoformat())
    assert not runner._is_future_episode("")
    assert not runner._is_future_episode("invalid")


def test_localize_change_buffer_flushes_batches_and_final_rows(tmp_path, monkeypatch):
    db_file = tmp_path / "media_server_manager.db"
    init_db(db_file)
    server_id = seed_server(db_file)
    queue = JobQueue(db_file)
    job_id = queue.create_job("localize", server_id, {"mode": "dry_run"})
    with connect(db_file) as db:
        server = db.execute("SELECT * FROM servers WHERE id = ?", (server_id,)).fetchone()

    class FakeLocalizer:
        def __init__(self, *_args, **kwargs):
            self.change = kwargs["change"]

        def count_all_items(self):
            return 51

        def loop_all(self):
            for index in range(51):
                self.change(
                    {
                        "library_id": 7,
                        "media_type": "movie",
                        "rating_key": str(index),
                        "title": f"Title {index}",
                        "field": "titleSort",
                        "old_value": "旧",
                        "new_value": "JIU",
                        "old_locked": False,
                        "applied": False,
                    }
                )

        def loop_all_collections(self):
            return None

    monkeypatch.setattr(tasks_module, "PlexServer", FakeLocalizer)
    runner = TaskManager(db_file)
    runner._run_localize_job(job_id, server, {"mode": "dry_run", "scope": {}})
    with connect(db_file) as db:
        rows = db.execute("SELECT * FROM changes WHERE job_id = ? ORDER BY id", (job_id,)).fetchall()
    assert len(rows) == 51
    assert rows[-1]["rating_key"] == "50"

    with pytest.raises(ValueError, match="本地化模式无效"):
        runner._run_localize_job(job_id, server, {"mode": "invalid"})
