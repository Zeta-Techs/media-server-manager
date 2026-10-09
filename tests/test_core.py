from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from media_server_manager.core import (
    PLEX_PRODUCT,
    PlexServer,
    ServerConfig,
    TaskCancelled,
    convert_to_pinyin,
    has_chinese,
    is_english,
    plex_headers,
)


def test_text_helpers_and_headers():
    assert has_chinese("重庆森林")
    assert not has_chinese("Chungking Express")
    assert not is_english("重庆森林")
    assert is_english("となりのトトロ")
    assert convert_to_pinyin("重庆森林") == "CQSL"
    assert convert_to_pinyin("重庆森林", "full_spell") == "CHONGQINGSENLIN"
    headers = plex_headers("token", "client-id")
    assert headers["X-Plex-Token"] == "token"
    assert headers["X-Plex-Product"] == PLEX_PRODUCT
    assert headers["X-Plex-Client-Identifier"] == "client-id"


def test_list_media_keys_filters_recent_added_items():
    now = datetime.now(timezone.utc)

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "MediaContainer": {
                    "Metadata": [
                        {"ratingKey": "new", "addedAt": int((now - timedelta(days=1)).timestamp())},
                        {"ratingKey": "old", "addedAt": int((now - timedelta(days=9)).timestamp())},
                        {"ratingKey": "unknown"},
                    ]
                }
            }

    class FakeSession:
        def request(self, method, url, **kwargs):
            return FakeResponse()

    plex = PlexServer(
        ServerConfig(address="http://plex.local", token="token"),
        auto_login=False,
        scope={"added_within_days": 3},
    )
    plex.s = FakeSession()
    assert plex.list_media_keys([1, 1], print_counts=False) == ["new"]
    plex.scope = {}
    assert plex.list_media_keys([1, 1], print_counts=False) == ["new", "old", "unknown"]


def test_music_type_filter_builds_plan_for_actual_media_type():
    plex = PlexServer(
        ServerConfig(address="http://plex.local", token="token"),
        auto_login=False,
        scope={"media_types": ["album"], "fields": ["titleSort"]},
    )
    plex.list_library = lambda: [[7, 8, "Music"]]
    calls = []
    plex.list_media_keys = lambda select, print_counts=False: calls.append(tuple(select)) or ["a1"]
    assert plex.count_all_items() == 1
    assert calls == [(7, 9)]


def test_new_album_event_processes_only_album_children():
    plex = PlexServer(ServerConfig(address="http://plex.local", token="token"), auto_login=False)
    plex.list_library = lambda: [[7, 8, "Music"]]
    calls = []
    plex.process_artist = lambda args: calls.append(("artist", args))
    plex.process_album = lambda args: calls.append(("album", args))
    plex.process_track = lambda args: calls.append(("track", args))
    plex.get_children = lambda key: [{"ratingKey": "track-1"}, {"ratingKey": "track-2"}]
    plex.process_new_item({"ratingKey": "album-1", "librarySectionID": "7", "type": "album"})
    assert calls[0] == ("album", ([7, 9], "album-1"))
    assert {entry[1][1] for entry in calls[1:]} == {"track-1", "track-2"}
    assert not any(entry[0] == "artist" for entry in calls)


def test_continue_watching_candidates_and_progress_call():
    calls = []

    class FakeResponse:
        def __init__(self, payload=None):
            self.payload = payload or {}

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class FakeSession:
        def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            if url.endswith("/library/sections/7/all"):
                return FakeResponse(
                    {"MediaContainer": {"Metadata": [{"ratingKey": "show-1", "title": "Example"}]}}
                )
            if url.endswith("/library/metadata/show-1/allLeaves"):
                return FakeResponse(
                    {
                        "MediaContainer": {
                            "Metadata": [
                                {
                                    "ratingKey": "e1",
                                    "parentIndex": 1,
                                    "index": 1,
                                    "title": "One",
                                    "viewCount": 1,
                                    "duration": 1200000,
                                },
                                {
                                    "ratingKey": "e2",
                                    "parentIndex": 1,
                                    "index": 2,
                                    "title": "Two",
                                    "duration": 1200000,
                                },
                            ]
                        }
                    }
                )
            return FakeResponse()

    plex = PlexServer(ServerConfig(address="http://plex.local", token="token"), auto_login=False)
    plex.s = FakeSession()
    candidates = plex.episode_progress_candidates(7)
    assert candidates[0]["episode_rating_key"] == "e2"
    plex.set_playback_progress("e2", 60000)
    assert calls[-1][2]["params"]["state"] == "stopped"


def test_cancel_callback_stops_before_request():
    plex = PlexServer(
        ServerConfig(address="http://plex.local", token="token"),
        auto_login=False,
        cancelled=lambda: True,
    )
    with pytest.raises(TaskCancelled):
        plex.login()
