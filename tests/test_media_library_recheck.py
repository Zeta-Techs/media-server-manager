from __future__ import annotations

import json

from clp_web.db import connect, init_db, new_secret, utcnow
from clp_web.rechecks import enqueue_due_rechecks, request_recheck


def _database(tmp_path):
    path = tmp_path / "clp.db"
    init_db(path)
    now = utcnow()
    with connect(path) as db:
        server_id = db.execute(
            "INSERT INTO servers (name,address,token,webhook_secret,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            ("Plex", "http://plex.local", "token", new_secret(), now, now),
        ).lastrowid
        db.execute(
            "INSERT INTO media_libraries (server_id,library_id,title,plex_type,auto_recheck_new_episodes,updated_at) VALUES (?,?,?,?,?,?)",
            (server_id, 2, "电视剧", 2, 1, now),
        )
        db.commit()
    return path, int(server_id)


def test_episode_webhook_is_debounced_and_enqueued(tmp_path):
    path, server_id = _database(tmp_path)
    episode = {"type": "episode", "librarySectionID": 2, "grandparentRatingKey": "show-1", "ratingKey": "episode-1"}
    with connect(path) as db:
        assert request_recheck(db, server_id, "library.new", episode) == "auto_recheck_queued"
        assert request_recheck(db, server_id, "library.new", {**episode, "ratingKey": "episode-2"}) == "auto_recheck_merged"
        db.commit()
    with connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM media_library_recheck_requests").fetchone()[0] == 1
        request = db.execute("SELECT * FROM media_library_recheck_requests").fetchone()
        assert request["rating_key"] == "show-1"
        db.execute("UPDATE media_library_recheck_requests SET next_run_at=?", ("2000-01-01T00:00:00Z",))
        db.commit()
    enqueue_due_rechecks(path)
    with connect(path) as db:
        job = db.execute("SELECT * FROM jobs WHERE type='media_library_show_recheck'").fetchone()
        assert job is not None
        assert json.loads(job["payload"])["rating_key"] == "show-1"
        assert db.execute("SELECT status FROM media_library_recheck_requests").fetchone()[0] == "running"


def test_disabled_or_non_episode_webhooks_are_ignored(tmp_path):
    path, server_id = _database(tmp_path)
    with connect(path) as db:
        assert request_recheck(db, server_id, "library.new", {"type": "movie", "librarySectionID": 2}) == "not_episode"
        db.execute("UPDATE media_libraries SET auto_recheck_new_episodes=0")
        assert request_recheck(db, server_id, "library.new", {"type": "episode", "librarySectionID": 2, "grandparentRatingKey": "show-1"}) == "auto_recheck_disabled"
        assert db.execute("SELECT COUNT(*) FROM media_library_recheck_requests").fetchone()[0] == 0


def test_missing_show_key_is_reported(tmp_path):
    path, server_id = _database(tmp_path)
    with connect(path) as db:
        assert request_recheck(db, server_id, "library.new", {"type": "episode", "librarySectionID": 2}) == "show_key_missing"
