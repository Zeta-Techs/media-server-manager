from __future__ import annotations

import logging
import threading
from typing import Any

import pytest

from clp.core import (
    TYPE,
    PlexServer,
    ServerConfig,
    TaskCancelled,
    extract_external_ids,
    join_skip_libraries,
    split_skip_libraries,
)


class FakeResponse:
    def __init__(self, payload: dict[str, Any] | None = None):
        self.payload = payload or {}

    def json(self):
        return self.payload

    def raise_for_status(self):
        return None


def make_plex(**kwargs: Any) -> PlexServer:
    return PlexServer(
        ServerConfig(address="http://plex.local/", token="token"),
        auto_login=False,
        **kwargs,
    )


def test_external_ids_and_skip_library_helpers():
    metadata = {
        "guid": "plex://movie/tmdb://123",
        "grandparentGuid": "tvdb://456",
        "parentGuid": "imdb://tt789",
        "Guid": [{"id": "themoviedb://999"}, "tvdb://111", {}, None],
    }
    assert extract_external_ids(metadata) == {"tmdb": "123", "tvdb": "456", "imdb": "tt789"}
    assert extract_external_ids({}) == {}
    assert split_skip_libraries("") == []
    assert split_skip_libraries(" Movies ; Music； ; Shows ") == ["Movies", "Music", "Shows"]
    assert join_skip_libraries([" Movies ", "", "Music", None]) == "Movies；Music"


def test_init_callbacks_thread_session_and_scope_helpers(monkeypatch, caplog):
    logs: list[str] = []
    progress: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    monkeypatch.setattr(PlexServer, "login", lambda _self: "Living Room")
    plex = PlexServer(
        ServerConfig(address="http://plex.local", token="token"),
        log=logs.append,
        progress=progress.append,
        change=changes.append,
        scope={
            "library_ids": "7",
            "media_types": ["movie"],
            "fields": ["genre"],
            "added_within_days": "bad",
        },
    )
    assert logs == ["已成功连接到服务器：Living Room"]
    plex._progress(stage="ready")
    plex._record_change([7, TYPE["movie"]], "1", "标题", "genre", [], ["动作"], True, False)
    assert progress == [{"stage": "ready"}]
    assert changes[0]["media_type"] == "movie"
    assert plex._library_allowed([7, TYPE["movie"], "Movies"])
    assert not plex._library_allowed([8, TYPE["movie"], "Other"])
    assert plex._type_allowed(TYPE["movie"])
    assert not plex._type_allowed(TYPE["show"])
    assert plex._field_allowed("genre")
    assert not plex._field_allowed("mood")
    assert plex._added_within_days() is None
    assert plex._media_item_allowed({"addedAt": "not-a-timestamp"})

    owner_session = plex._session()
    worker_sessions: list[Any] = []

    def get_session() -> None:
        worker_sessions.append(plex._session())
        worker_sessions.append(plex._session())

    thread = threading.Thread(target=get_session)
    thread.start()
    thread.join()
    assert worker_sessions[0] is worker_sessions[1]
    assert worker_sessions[0] is not owner_session

    silent = make_plex()
    caplog.set_level(logging.INFO)
    silent._log("fallback logger")
    assert "fallback logger" in caplog.text


def test_field_state_login_and_basic_reads(monkeypatch):
    plex = make_plex()
    metadata = {
        "titleSort": "CS",
        "Genre": [{"tag": "Action"}, {"tag": ""}],
        "Field": [{"name": "titleSort", "locked": True}, {"name": "genre", "locked": 0}],
    }
    monkeypatch.setattr(plex, "get_metadata", lambda _key: metadata)
    assert plex.read_field_state("1", "titleSort") == ("CS", True)
    assert plex.read_field_state("1", "genre") == (["Action"], False)
    with pytest.raises(ValueError, match="不支持"):
        plex.read_field_state("1", "summary")
    assert PlexServer.field_locked({}, "genre") is None

    monkeypatch.setattr(
        plex,
        "_request",
        lambda _method, _url, **_kwargs: FakeResponse(
            {"MediaContainer": {"friendlyName": "Mock Plex"}}
        ),
    )
    assert plex.login() == "Mock Plex"
    monkeypatch.setattr(plex, "_request", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError()))
    with pytest.raises(RuntimeError, match="服务器连接失败"):
        plex.login()


def test_library_metadata_and_show_episode_fallback(monkeypatch):
    plex = make_plex()

    def request(_method: str, url: str, **kwargs: Any) -> FakeResponse:
        if url.endswith("/library/sections/"):
            return FakeResponse(
                {
                    "MediaContainer": {
                        "Directory": [
                            {"key": "7", "type": "movie", "title": "Movies"},
                            {"key": "8", "type": "photo", "title": "Photos"},
                            {"key": "9", "type": "unknown", "title": "Ignore"},
                        ]
                    }
                }
            )
        if url.endswith("/library/metadata/m1"):
            return FakeResponse({"MediaContainer": {"Metadata": [{"ratingKey": "m1"}]}})
        if url.endswith("/library/metadata/m1/children"):
            return FakeResponse({"MediaContainer": {"Metadata": [{"ratingKey": "child"}]}})
        if url.endswith("/library/sections/7/all") and kwargs.get("params") == {"type": 2}:
            return FakeResponse({"MediaContainer": {"Metadata": [{"ratingKey": "show"}]}})
        return FakeResponse()

    monkeypatch.setattr(plex, "_request", request)
    assert plex.list_library() == [[7, 1, "Movies"], [8, 99, "Photos"]]
    assert plex.get_metadata("m1")["ratingKey"] == "m1"
    assert plex.get_children("m1")[0]["ratingKey"] == "child"
    assert plex.list_show_items(7)[0]["ratingKey"] == "show"

    logs: list[str] = []
    fallback = make_plex(log=logs.append)
    monkeypatch.setattr(
        fallback,
        "_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("allLeaves unavailable")),
    )
    children = {
        "show": [{"title": "No key"}, {"ratingKey": "s1"}, {"ratingKey": "s2", "title": "Season 2"}],
        "s1": [{"parentIndex": 1, "index": 1, "title": "One", "ratingKey": "e1"}],
    }

    def get_children(key: str) -> list[dict[str, Any]]:
        if key == "s2":
            raise OSError("broken season")
        return children.get(key, [])

    monkeypatch.setattr(fallback, "get_children", get_children)
    assert fallback.list_show_episodes("show")[0]["ratingKey"] == "e1"
    assert "读取季子级失败" in logs[0]
    monkeypatch.setattr(
        fallback,
        "list_show_episodes",
        lambda _key: [
            {"parentIndex": 1, "index": 1, "title": "One", "ratingKey": "e1"},
            {"parentIndex": None, "index": 2},
            {"parentIndex": "bad", "index": 3},
        ],
    )
    assert fallback.collect_show_episode_numbers("show") == {
        (1, 1): {"title": "One", "rating_key": "e1"}
    }


def test_episode_candidate_rejection_branches(monkeypatch):
    plex = make_plex()
    monkeypatch.setattr(
        plex,
        "list_show_items",
        lambda _library_id: [
            {"ratingKey": "missing", "title": "Missing fields"},
            {"ratingKey": "invalid", "title": "Invalid"},
            {"ratingKey": "unwatched", "title": "Unwatched"},
            {"ratingKey": "complete", "title": "Complete"},
            {"ratingKey": "started", "title": "Already started"},
        ],
    )
    episodes = {
        "missing": [{"parentIndex": None, "index": 1}],
        "invalid": [{"parentIndex": "bad", "index": 1}],
        "unwatched": [{"parentIndex": 1, "index": 1}],
        "complete": [{"parentIndex": 1, "index": 1, "viewCount": 1}],
        "started": [
            {"parentIndex": 1, "index": 1, "viewCount": 1},
            {"parentIndex": 1, "index": 2, "viewOffset": 500},
        ],
    }
    monkeypatch.setattr(plex, "list_show_episodes", lambda key: episodes[key])
    assert plex.episode_progress_candidates(7) == []


def test_write_helpers_and_tag_replacement(monkeypatch):
    plex = make_plex()
    requests: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setattr(
        plex,
        "_request",
        lambda method, url, **kwargs: requests.append((method, url, kwargs)) or FakeResponse(),
    )
    plex.put_title_sort([7, 1], "m1", "BT", 1)
    plex._put_tag_field_values([7, 1], "m1", ["A", "B"], "genre", lock=0)
    assert requests[0][0] == "PUT"
    assert requests[1][2]["params"]["genre[1].tag.tag"] == "B"
    assert requests[1][2]["params"]["genre.locked"] == 0

    monkeypatch.setattr(
        plex,
        "get_metadata",
        lambda _key: {
            "Genre": [{"tag": "Action"}, {"tag": "Keep"}],
            "Style": [{"tag": "Rock"}],
            "Mood": [{"tag": "Happy"}],
        },
    )
    plex.put_genres([7, 1], "m1", "Action", "动作")
    plex.put_styles([7, 1], "m1", "Rock", "摇滚")
    plex.put_mood([7, 1], "m1", "Happy", "快乐")
    assert requests[-1][2]["params"]["mood[0].tag.tag"] == "快乐"


def test_title_and_tag_processing_dry_run_and_apply(monkeypatch):
    logs: list[str] = []
    progress: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    dry = make_plex(
        tags={"Action": "动作", "Duplicate": "动作"},
        log=logs.append,
        progress=progress.append,
        change=changes.append,
        dry_run=True,
    )
    metadata = {
        "title": "重庆森林",
        "titleSort": "",
        "Genre": [{"tag": "Action"}, {"tag": "Duplicate"}, {"tag": "Keep"}],
        "Style": [],
        "Mood": [],
        "Field": [{"name": "titleSort", "locked": 0}, {"name": "genre", "locked": 1}],
    }
    dry._process_title_sort([7, 1], "m1", "重庆森林", "", metadata)
    dry._process_tag_field([7, 1], "m1", "重庆森林", metadata, "genre")
    dry._process_tag_field([7, 1], "m1", "重庆森林", metadata, "style")
    assert any("[预览]" in line for line in logs)
    assert changes[0]["applied"] is False
    assert changes[1]["new_value"] == ["Keep", "动作"]
    assert {tuple(item.items()) for item in progress}

    applied = make_plex(tags={"Action": "动作"}, log=logs.append, progress=progress.append)
    writes: list[tuple[Any, ...]] = []
    monkeypatch.setattr(applied, "put_title_sort", lambda *args: writes.append(args))
    monkeypatch.setattr(applied, "_put_tag_field_values", lambda *args, **kwargs: writes.append(args))
    applied._process_title_sort([7, 1], "m1", "重庆森林", "", metadata)
    applied._process_tag_field([7, 1], "m1", "重庆森林", metadata, "genre")
    assert len(writes) == 2

    scoped = make_plex(scope={"fields": ["mood"]})
    scoped._process_title_sort([7, 1], "m1", "重庆森林", "", metadata)
    scoped._process_tag_field([7, 1], "m1", "重庆森林", metadata, "genre")


def test_media_processors_and_run_items(monkeypatch):
    progress: list[dict[str, Any]] = []
    logs: list[str] = []
    plex = make_plex(tags={"Action": "动作"}, progress=progress.append, log=logs.append)
    metadata = {
        "title": "English",
        "titleSort": "English",
        "Genre": [{"tag": "Action"}],
        "Style": [],
        "Mood": [],
    }
    monkeypatch.setattr(plex, "get_metadata", lambda _key: metadata)
    monkeypatch.setattr(plex, "_put_tag_field_values", lambda *_args, **_kwargs: None)
    plex.process_item(([7, TYPE["movie"]], "m"))
    plex.process_item(([7, TYPE["track"]], "t"))
    plex.process_artist(([7, TYPE["artist"]], "a"))
    plex.process_album(([7, TYPE["album"]], "b"))
    plex.process_track(([7, TYPE["track"]], "t"))

    plex._run_items(lambda _item: None, [])
    plex._run_items(lambda item: (_ for _ in ()).throw(ValueError(str(item))), ["bad"])
    assert any("处理项目失败" in line for line in logs)
    with pytest.raises(TaskCancelled):
        plex._run_items(lambda _item: (_ for _ in ()).throw(TaskCancelled()), ["stop"])


def test_work_plan_loops_and_collections(monkeypatch):
    logs: list[str] = []
    progress: list[dict[str, Any]] = []
    plex = PlexServer(
        ServerConfig(
            address="http://plex.local",
            token="token",
            skip_libraries=["Skipped"],
        ),
        auto_login=False,
        log=logs.append,
        progress=progress.append,
        scope={"library_ids": ["7", "8"]},
    )
    monkeypatch.setattr(
        plex,
        "list_library",
        lambda: [
            [7, TYPE["movie"], "Movies"],
            [8, TYPE["artist"], "Music"],
            [9, TYPE["photo"], "Photos"],
            [10, TYPE["movie"], "Skipped"],
            [11, TYPE["movie"], "Out of scope"],
        ],
    )
    monkeypatch.setattr(plex, "list_media_keys", lambda select, print_counts=False: [f"{select[0]}-{select[1]}"])
    monkeypatch.setattr(
        plex,
        "_request",
        lambda *_args, **_kwargs: FakeResponse(
            {
                "MediaContainer": {
                    "Metadata": [
                        {"ratingKey": "c1", "title": "中文合集", "titleSort": ""},
                        {"ratingKey": "broken"},
                    ]
                }
            }
        ),
    )
    processed: list[str] = []
    monkeypatch.setattr(plex, "process_item", lambda args: processed.append(args[1]))
    monkeypatch.setattr(plex, "process_artist", lambda args: processed.append(args[1]))
    monkeypatch.setattr(plex, "process_album", lambda args: processed.append(args[1]))
    monkeypatch.setattr(plex, "process_track", lambda args: processed.append(args[1]))
    monkeypatch.setattr(plex, "put_title_sort", lambda *_args: None)

    assert plex.count_all_items() == 8
    assert plex.count_all_items() == 8
    plex.loop_all()
    plex.put_collection_title_sort([7, 1], "c1", "ZW", 1)
    plex.loop_all_collections()
    assert set(processed) == {"7-1", "8-8", "8-9", "8-10"}
    assert any(item.get("errors_delta") == 1 for item in progress)
    assert plex._prepared_items is None

    no_collections = make_plex(scope={"fields": ["genre"]})
    no_collections.loop_all_collections()


def test_new_collection_and_new_item_dispatch(monkeypatch):
    calls: list[tuple[str, Any]] = []
    logs: list[str] = []
    plex = make_plex(log=logs.append, progress=lambda payload: calls.append(("progress", payload)))
    monkeypatch.setattr(plex, "list_library", lambda: [[7, TYPE["artist"], "Music"]])
    monkeypatch.setattr(
        plex,
        "_request",
        lambda *_args, **_kwargs: FakeResponse(
            {
                "MediaContainer": {
                    "Metadata": [
                        {"guid": "match", "ratingKey": "c1", "title": "合集", "titleSort": ""},
                        {"guid": "other", "ratingKey": "c2", "title": "Other"},
                    ]
                }
            }
        ),
    )
    monkeypatch.setattr(plex, "_process_title_sort", lambda *args: calls.append(("collection", args[1])))
    plex.process_new_collections(99, ["match"])
    plex.process_new_collections(7, ["match"])
    assert ("collection", "c1") in calls

    monkeypatch.setattr(plex, "process_artist", lambda args: calls.append(("artist", args[1])))
    monkeypatch.setattr(plex, "process_album", lambda args: calls.append(("album", args[1])))
    monkeypatch.setattr(plex, "process_track", lambda args: calls.append(("track", args[1])))
    monkeypatch.setattr(plex, "process_item", lambda args: calls.append(("item", args[1])))
    children = {
        "artist-1": [{"ratingKey": "album-1"}, {}],
        "album-1": [{"ratingKey": "track-1"}, {}],
    }
    monkeypatch.setattr(plex, "get_children", lambda key: children.get(key, []))
    plex.process_new_item({"ratingKey": "artist-1", "librarySectionID": "7", "type": "artist"})
    plex.process_new_item({"ratingKey": "track-2", "librarySectionID": "7", "type": "track"})
    plex.process_new_item(
        {
            "ratingKey": "movie-1",
            "librarySectionID": "7",
            "type": "movie",
            "Collection": [{"guid": "match"}],
        }
    )
    assert {entry for entry in calls if entry[0] in {"artist", "album", "track", "item"}} >= {
        ("artist", "artist-1"),
        ("album", "album-1"),
        ("track", "track-1"),
        ("track", "track-2"),
        ("item", "movie-1"),
    }

    plex.process_new_item({"ratingKey": "e1", "librarySectionID": "7", "type": "episode"})
    monkeypatch.setattr(plex, "list_library", lambda: [])
    plex.process_new_item({"ratingKey": "m", "librarySectionID": "7", "type": "movie"})
    plex.skip_libraries = ["Music"]
    monkeypatch.setattr(plex, "list_library", lambda: [[7, TYPE["artist"], "Music"]])
    plex.process_new_item({"ratingKey": "m", "librarySectionID": "7", "type": "movie"})
    assert any("跳过剧集" in line for line in logs)
    assert any("未找到" in line for line in logs)
    assert any("跳过媒体库" in line for line in logs)


def test_apply_changes_scan_tags_and_maintenance(monkeypatch):
    plex = make_plex(tags={"Mapped": "已映射"}, progress=lambda _payload: None)
    writes: list[tuple[str, Any]] = []
    monkeypatch.setattr(plex, "put_title_sort", lambda select, key, value, lock: writes.append(("title", (select, key, value, lock))))
    monkeypatch.setattr(
        plex,
        "_put_tag_field_values",
        lambda select, key, tags, field, lock=1: writes.append((field, (select, key, tags, lock))),
    )
    title_change = {"library_id": 7, "media_type": "movie", "rating_key": "m", "field": "titleSort"}
    plex.apply_change_value(title_change, "BT", locked=False)
    plex.apply_recorded_change({**title_change, "old_value": "OLD", "old_locked": True})
    plex.apply_change_value(
        {"library_id": 7, "media_type": "1", "rating_key": "m", "field": "genre"},
        ["动作"],
    )
    with pytest.raises(ValueError, match="不支持回滚字段"):
        plex.apply_change_value({**title_change, "field": "summary"}, "x")
    assert writes[0][1][-1] == 0

    monkeypatch.setattr(
        plex,
        "list_library",
        lambda: [
            [7, TYPE["artist"], "Music"],
            [8, TYPE["photo"], "Photos"],
            [9, TYPE["movie"], "Skipped"],
        ],
    )
    plex.skip_libraries = ["Skipped"]
    monkeypatch.setattr(plex, "list_media_keys", lambda select, print_counts=False: [f"{select[1]}", "bad"])

    def metadata(key: str) -> dict[str, Any]:
        if key == "bad":
            raise OSError("unreadable")
        return {
            "Genre": [{"tag": "Action"}, {"tag": "Mapped"}],
            "Style": [{"tag": "Rock"}],
            "Mood": [{"tag": "Happy"}],
        }

    monkeypatch.setattr(plex, "get_metadata", metadata)
    suggestions = plex.scan_unmapped_tags()
    assert suggestions["genre"] == {"Action": 3}
    assert suggestions["style"] == {"Rock": 3}
    assert suggestions["mood"] == {"Happy": 3}

    requests: list[tuple[str, str]] = []
    monkeypatch.setattr(
        plex,
        "_request",
        lambda method, url, **_kwargs: requests.append((method, url))
        or FakeResponse({"MediaContainer": {"Activity": []}}),
    )
    plex.scan_library(7)
    plex.refresh_metadata("m")
    plex.analyze_metadata("m")
    assert plex.activities() == {"MediaContainer": {"Activity": []}}
    assert [method for method, _url in requests] == ["GET", "PUT", "PUT", "GET"]
