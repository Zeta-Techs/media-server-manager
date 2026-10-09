from __future__ import annotations

import concurrent.futures
import logging
import os
import re
import string
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

import pypinyin
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("MSM_CONFIG_DIR", str(BASE_DIR / "config"))).expanduser()
TEMPLATE_TAGS_FILE = Path(__file__).resolve().with_name("default_tags.json")

TYPE = {"movie": 1, "show": 2, "artist": 8, "album": 9, "track": 10, "photo": 99}
TYPE_NAME = {value: key for key, value in TYPE.items()}
PLEX_PRODUCT = "Media Server Manager"
DEFAULT_CLIENT_IDENTIFIER = "media-server-manager"

PUNCTUATION_TABLE = str.maketrans(
    "",
    "",
    "：（），！？。；·-／,…!?.:;～~・“”《》〈〉()<>\u0022" + string.whitespace,
)

ProgressCallback = Callable[[Dict[str, Any]], None]
LogCallback = Callable[[str], None]
ChangeCallback = Callable[[Dict[str, Any]], None]
CancelCallback = Callable[[], bool]


class TaskCancelled(RuntimeError):
    pass


@dataclass
class ServerConfig:
    address: str
    token: str
    name: str = ""
    skip_libraries: List[str] = field(default_factory=list)
    pinyin_mode: str = "first_letter"
    client_identifier: str = DEFAULT_CLIENT_IDENTIFIER


def plex_headers(token: str = "", client_identifier: str = DEFAULT_CLIENT_IDENTIFIER) -> Dict[str, str]:
    headers = {
        "Accept": "application/json",
        "X-Plex-Product": PLEX_PRODUCT,
        "X-Plex-Client-Identifier": client_identifier or DEFAULT_CLIENT_IDENTIFIER,
    }
    if token:
        headers["X-Plex-Token"] = token
    return headers


def extract_external_ids(metadata: Dict[str, Any]) -> Dict[str, str]:
    ids: Dict[str, str] = {}
    candidates: List[str] = []
    for key in ("guid", "grandparentGuid", "parentGuid"):
        value = metadata.get(key)
        if value:
            candidates.append(str(value))
    for item in metadata.get("Guid", []) or []:
        value = item.get("id") if isinstance(item, dict) else item
        if value:
            candidates.append(str(value))
    patterns = (
        ("tmdb", re.compile(r"(?:tmdb|themoviedb)://(\d+)", re.IGNORECASE)),
        ("tvdb", re.compile(r"tvdb://(\d+)", re.IGNORECASE)),
        ("imdb", re.compile(r"imdb://(tt\d+)", re.IGNORECASE)),
    )
    for value in candidates:
        for name, pattern in patterns:
            match = pattern.search(value)
            if match and name not in ids:
                ids[name] = match.group(1)
    return ids


def split_skip_libraries(value: str) -> List[str]:
    if not value:
        return []
    parts = value.replace(";", "；").split("；")
    return [part.strip() for part in parts if part.strip()]


def join_skip_libraries(values: Iterable[str]) -> str:
    return "；".join([value.strip() for value in values if value and value.strip()])


def has_chinese(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text or "")


def is_english(text: str) -> bool:
    text = (text or "").replace("・", "")
    if not has_chinese(text):
        return True
    return any("\u3040" <= char <= "\u30ff" for char in text)


def convert_to_pinyin(text: str, mode: str = "first_letter") -> str:
    style = pypinyin.NORMAL if mode == "full_spell" else pypinyin.FIRST_LETTER
    parts = pypinyin.pinyin(text or "", style=style)
    return "".join(str(item[0]).upper() for item in parts).translate(PUNCTUATION_TABLE)


class PlexServer:
    def __init__(
        self,
        server: ServerConfig,
        tags: Optional[Dict[str, str]] = None,
        log: Optional[LogCallback] = None,
        progress: Optional[ProgressCallback] = None,
        change: Optional[ChangeCallback] = None,
        cancelled: Optional[CancelCallback] = None,
        dry_run: bool = False,
        scope: Optional[Dict[str, Any]] = None,
        timeout: int = 30,
        auto_login: bool = True,
    ) -> None:
        self.server = server
        self.host = server.address.rstrip("/")
        self.token = server.token
        self.skip_libraries = server.skip_libraries
        self.pinyin_mode = server.pinyin_mode or "first_letter"
        self.client_identifier = server.client_identifier or DEFAULT_CLIENT_IDENTIFIER
        self.tags = tags if tags is not None else {}
        self.log_callback = log
        self.progress_callback = progress
        self.change_callback = change
        self.cancel_callback = cancelled
        self.dry_run = dry_run
        self.scope = scope or {}
        self.timeout = timeout
        self._headers = plex_headers(self.token, self.client_identifier)
        self._thread_local = threading.local()
        self._owner_thread = threading.get_ident()
        self.s = self._new_session()
        self._prepared_items: Optional[List[Dict[str, Any]]] = None
        self._prepared_collections: Optional[List[Dict[str, Any]]] = None
        if auto_login:
            friendly_name = self.login()
            self._log(f"已成功连接到服务器：{friendly_name}")

    def _new_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update(self._headers)
        retry = Retry(
            total=2,
            connect=2,
            read=2,
            status=2,
            backoff_factor=0.25,
            status_forcelist=(429, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
            raise_on_status=False,
        )
        session.mount("http://", HTTPAdapter(max_retries=retry))
        session.mount("https://", HTTPAdapter(max_retries=retry))
        return session

    def _session(self) -> requests.Session:
        if threading.get_ident() == self._owner_thread:
            return self.s
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = self._new_session()
            self._thread_local.session = session
        return session

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        self._check_cancelled()
        kwargs.setdefault("timeout", (5, self.timeout))
        response = self._session().request(method, url, **kwargs)
        response.raise_for_status()
        return response

    def _check_cancelled(self) -> None:
        if self.cancel_callback and self.cancel_callback():
            raise TaskCancelled("任务已取消")

    def _log(self, message: str) -> None:
        if self.log_callback:
            self.log_callback(message)
        else:
            logging.getLogger(__name__).info(message)

    def _progress(self, **payload: Any) -> None:
        if self.progress_callback:
            self.progress_callback(payload)

    def _scope_values(self, name: str) -> List[str]:
        values = self.scope.get(name) or []
        if isinstance(values, str):
            values = [values]
        return [str(value) for value in values]

    def _library_allowed(self, library: List[Any]) -> bool:
        library_ids = self._scope_values("library_ids")
        if library_ids and str(library[0]) not in library_ids:
            return False
        return True

    def _type_allowed(self, plex_type: int) -> bool:
        media_types = self._scope_values("media_types")
        if not media_types:
            return True
        names = {str(TYPE_NAME.get(plex_type, plex_type)), str(plex_type)}
        return bool(names.intersection(media_types))

    def _field_allowed(self, field: str) -> bool:
        fields = self._scope_values("fields")
        return not fields or field in fields

    def _added_within_days(self) -> Optional[int]:
        value = self.scope.get("added_within_days")
        if value in (None, ""):
            return None
        try:
            days = int(str(value))
        except (TypeError, ValueError):
            return None
        return days if days > 0 else None

    def _media_item_allowed(self, item: Dict[str, Any]) -> bool:
        days = self._added_within_days()
        if not days:
            return True
        added_at = item.get("addedAt")
        if added_at in (None, ""):
            return False
        try:
            added_at_seconds = int(str(added_at))
        except (TypeError, ValueError):
            return False
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        return datetime.fromtimestamp(added_at_seconds, tz=timezone.utc) >= cutoff

    def _record_change(
        self,
        select: List[Any],
        rating_key: str,
        title: str,
        field: str,
        old_value: Any,
        new_value: Any,
        applied: bool,
        old_locked: Optional[bool] = None,
    ) -> None:
        if self.change_callback:
            self.change_callback(
                {
                    "library_id": select[0],
                    "media_type": TYPE_NAME.get(select[1], str(select[1])),
                    "rating_key": rating_key,
                    "title": title,
                    "field": field,
                    "old_value": old_value,
                    "new_value": new_value,
                    "applied": applied,
                    "old_locked": old_locked,
                }
            )

    @staticmethod
    def field_locked(metadata: Dict[str, Any], field: str) -> Optional[bool]:
        normalized = field.lower()
        for item in metadata.get("Field", []) or []:
            if str(item.get("name") or "").lower() == normalized:
                return bool(item.get("locked"))
        return None

    def read_field_state(self, rating_key: str, field: str) -> tuple[Any, Optional[bool]]:
        metadata = self.get_metadata(rating_key)
        if field == "titleSort":
            value: Any = metadata.get("titleSort", "")
        elif field in {"genre", "style", "mood"}:
            value = [
                item.get("tag") for item in metadata.get(field.capitalize(), []) or [] if item.get("tag")
            ]
        else:
            raise ValueError(f"不支持的变更字段：{field}")
        return value, self.field_locked(metadata, field)

    def login(self) -> str:
        try:
            data = self._request("GET", self.host).json()
            return data["MediaContainer"]["friendlyName"]
        except TaskCancelled:
            raise
        except Exception as exc:
            raise RuntimeError("服务器连接失败，请检查 Plex 地址、Token 或网络设置。") from exc

    def list_library(self) -> List[List[Any]]:
        data = self._request("GET", f"{self.host}/library/sections/").json()
        directories = data.get("MediaContainer", {}).get("Directory", [])
        libraries = []
        for item in directories:
            plex_type = item.get("type")
            if plex_type in TYPE:
                libraries.append([int(item["key"]), TYPE[plex_type], item["title"]])
        return libraries

    def list_library_items(self, library_id: int, plex_type: int) -> List[Dict[str, Any]]:
        """Return all top-level items in a Plex library.

        The management UI needs a dense inventory view, so this intentionally
        uses Plex's full library endpoint instead of the paged presentation
        endpoints used by the overview.
        """
        data = self._request(
            "GET",
            f"{self.host}/library/sections/{int(library_id)}/all",
            params={"type": int(plex_type), "X-Plex-Container-Start": 0, "X-Plex-Container-Size": 100000},
        ).json()
        return data.get("MediaContainer", {}).get("Metadata", []) or []

    def list_library_collections(self, library_id: int) -> List[Dict[str, Any]]:
        """Return collections exposed by a Plex library."""
        data = self._request(
            "GET",
            f"{self.host}/library/sections/{int(library_id)}/collections",
            params={"X-Plex-Container-Start": 0, "X-Plex-Container-Size": 100000},
        ).json()
        return data.get("MediaContainer", {}).get("Metadata", []) or []

    def list_media_keys(self, select: List[Any], print_counts: bool = True) -> List[str]:
        response = self._request(
            "GET", f"{self.host}/library/sections/{select[0]}/all?type={select[1]}"
        ).json()
        datas = response.get("MediaContainer", {}).get("Metadata", []) or []
        datas = [data for data in datas if self._media_item_allowed(data)]
        media_keys = [data["ratingKey"] for data in datas]
        if print_counts:
            label = {8: "艺人数", 9: "专辑数", 10: "曲目数"}.get(select[1], "项目数")
            suffix = f"（最近 {self._added_within_days()} 天）" if self._added_within_days() else ""
            self._log(f"{label}{suffix}：{len(media_keys)}")
        return media_keys

    def get_metadata(self, rating_key: str) -> Dict[str, Any]:
        data = self._request("GET", f"{self.host}/library/metadata/{rating_key}").json()
        return data["MediaContainer"]["Metadata"][0]

    def list_recent_media(self, limit: int = 12) -> List[Dict[str, Any]]:
        """Return a small, presentation-ready slice of the recently added library."""
        size = max(1, min(int(limit), 50))
        data = self._request(
            "GET",
            f"{self.host}/library/recentlyAdded",
            params={"X-Plex-Container-Start": 0, "X-Plex-Container-Size": size},
        ).json()
        items: List[Dict[str, Any]] = []
        for item in data.get("MediaContainer", {}).get("Metadata", []) or []:
            rating_key = str(item.get("ratingKey") or "")
            thumb = str(item.get("thumb") or "")
            title = str(item.get("title") or "").strip()
            if not rating_key or not thumb or not title:
                continue
            items.append(
                {
                    "rating_key": rating_key,
                    "title": title,
                    "year": item.get("year"),
                    "type": str(item.get("type") or "media"),
                    "thumb": thumb,
                    "added_at": item.get("addedAt"),
                }
            )
        return items

    def fetch_media_image(self, path: str) -> requests.Response:
        """Fetch a validated relative thumbnail path using the server-side Token."""
        return self._request("GET", f"{self.host}{path}")

    def get_children(self, rating_key: str) -> List[Dict[str, Any]]:
        data = self._request("GET", f"{self.host}/library/metadata/{rating_key}/children").json()
        return data.get("MediaContainer", {}).get("Metadata", []) or []

    def list_show_items(self, library_id: int) -> List[Dict[str, Any]]:
        data = self._request(
            "GET", f"{self.host}/library/sections/{library_id}/all", params={"type": TYPE["show"]}
        ).json()
        return data.get("MediaContainer", {}).get("Metadata", []) or []

    def list_show_episodes(self, show_rating_key: str) -> List[Dict[str, Any]]:
        try:
            data = self._request("GET", f"{self.host}/library/metadata/{show_rating_key}/allLeaves").json()
            episodes = data.get("MediaContainer", {}).get("Metadata", []) or []
            if episodes:
                return episodes
        except TaskCancelled:
            raise
        except Exception:
            pass
        fallback_episodes: List[Dict[str, Any]] = []
        for season in self.get_children(show_rating_key):
            season_key = season.get("ratingKey")
            if not season_key:
                continue
            try:
                fallback_episodes.extend(self.get_children(str(season_key)))
            except TaskCancelled:
                raise
            except Exception as exc:
                self._log(f"读取季子级失败：{season.get('title') or season_key}：{exc}")
        return fallback_episodes

    def collect_show_episode_numbers(self, show_rating_key: str) -> Dict[tuple[int, int], Dict[str, Any]]:
        collected: Dict[tuple[int, int], Dict[str, Any]] = {}
        for episode in self.list_show_episodes(show_rating_key):
            season = episode.get("parentIndex")
            number = episode.get("index")
            if season is None or number is None:
                continue
            try:
                key = (int(season), int(number))
            except (TypeError, ValueError):
                continue
            collected[key] = {
                "title": episode.get("title") or "",
                "rating_key": episode.get("ratingKey") or "",
            }
        return collected

    def episode_progress_candidates(
        self, library_id: int, default_offset_ms: int = 60000
    ) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        for show in self.list_show_items(library_id):
            episodes = self.list_show_episodes(str(show.get("ratingKey") or ""))
            parsed: List[Dict[str, Any]] = []
            for episode in episodes:
                if episode.get("parentIndex") is None or episode.get("index") is None:
                    continue
                try:
                    season_number = int(episode.get("parentIndex") or 0)
                    episode_number = int(episode.get("index") or 0)
                except (TypeError, ValueError):
                    continue
                parsed.append(
                    {
                        "raw": episode,
                        "season": season_number,
                        "episode": episode_number,
                        "view_count": int(episode.get("viewCount") or 0),
                        "view_offset": int(episode.get("viewOffset") or 0),
                        "duration": int(episode.get("duration") or 0),
                    }
                )
            parsed.sort(key=lambda item: (item["season"], item["episode"]))
            last_watched_index = None
            for index, item in enumerate(parsed):
                if item["view_count"] > 0:
                    last_watched_index = index
            if last_watched_index is None or last_watched_index + 1 >= len(parsed):
                continue
            target = parsed[last_watched_index + 1]
            if target["view_offset"] > 0:
                continue
            raw = target["raw"]
            duration = target["duration"]
            planned_offset = default_offset_ms
            if duration > 0:
                planned_offset = min(default_offset_ms, max(1000, int(duration * 0.05)))
            candidates.append(
                {
                    "show_title": show.get("title") or "",
                    "show_rating_key": str(show.get("ratingKey") or ""),
                    "episode_rating_key": str(raw.get("ratingKey") or ""),
                    "season": target["season"],
                    "episode": target["episode"],
                    "episode_title": raw.get("title") or "",
                    "duration": duration,
                    "planned_offset": planned_offset,
                    "current_offset": target["view_offset"],
                    "view_count": target["view_count"],
                }
            )
        return candidates

    def set_playback_progress(self, rating_key: str, offset_ms: int) -> None:
        self._request(
            "GET",
            f"{self.host}/:/progress",
            params={
                "key": rating_key,
                "identifier": "com.plexapp.plugins.library",
                "time": int(offset_ms),
                "state": "stopped",
            },
        )

    def put_title_sort(self, select: List[Any], rating_key: str, sort_title: str, lock: int) -> None:
        self._request(
            "PUT",
            f"{self.host}/library/metadata/{rating_key}",
            params={
                "type": select[1],
                "id": rating_key,
                "includeExternalMedia": 1,
                "titleSort.value": sort_title,
                "titleSort.locked": lock,
            },
        )

    def _put_tag_field_values(
        self,
        select: List[Any],
        rating_key: str,
        tags: List[str],
        field: str,
        lock: int = 1,
    ) -> None:
        params: Dict[str, Any] = {
            "type": select[1],
            "id": rating_key,
            f"{field}.locked": lock,
        }
        params.update({f"{field}[{i}].tag.tag": tag for i, tag in enumerate(tags)})
        self._request("PUT", f"{self.host}/library/metadata/{rating_key}", params=params)

    def _put_tag_field(
        self, select: List[Any], rating_key: str, source_tag: str, new_tag: str, field: str
    ) -> None:
        metadata = self.get_metadata(rating_key)
        current_tags = [item.get("tag") for item in metadata.get(field.capitalize(), [])]
        current_tags = [tag for tag in current_tags if tag and tag != source_tag]
        current_tags.append(new_tag)
        self._put_tag_field_values(select, rating_key, current_tags, field)

    def put_genres(self, select: List[Any], rating_key: str, tag: str, addtag: str) -> None:
        self._put_tag_field(select, rating_key, tag, addtag, "genre")

    def put_styles(self, select: List[Any], rating_key: str, tag: str, addtag: str) -> None:
        self._put_tag_field(select, rating_key, tag, addtag, "style")

    def put_mood(self, select: List[Any], rating_key: str, tag: str, addtag: str) -> None:
        self._put_tag_field(select, rating_key, tag, addtag, "mood")

    def _mark_change(self) -> None:
        self._progress(changes_delta=1)

    def _process_title_sort(
        self,
        select: List[Any],
        rating_key: str,
        title: str,
        title_sort: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self._field_allowed("titleSort"):
            return
        if not is_english(title) and (has_chinese(title_sort) or title_sort == ""):
            new_sort = convert_to_pinyin(title, self.pinyin_mode)
            if self.dry_run:
                self._log(f"[预览] {title} → {new_sort}")
            else:
                self.put_title_sort(select, rating_key, new_sort, 1)
                self._log(f"{title} → {new_sort}")
            self._record_change(
                select,
                rating_key,
                title,
                "titleSort",
                title_sort,
                new_sort,
                not self.dry_run,
                self.field_locked(metadata or {}, "titleSort"),
            )
            self._mark_change()

    def _process_tag_field(
        self, select: List[Any], rating_key: str, title: str, metadata: Dict[str, Any], field: str
    ) -> None:
        if not self._field_allowed(field):
            return
        plex_field = field.capitalize()
        old_tags = [item.get("tag") for item in metadata.get(plex_field, []) if item.get("tag")]
        replacements = {tag: self.tags[tag] for tag in old_tags if tag in self.tags}
        if not replacements:
            return
        new_tags = [tag for tag in old_tags if tag not in replacements]
        for new_tag in replacements.values():
            if new_tag not in new_tags:
                new_tags.append(new_tag)
        if self.dry_run:
            for source, target in replacements.items():
                self._log(f"[预览] {title}：{source} → {target}")
        else:
            self._put_tag_field_values(select, rating_key, new_tags, field)
            for source, target in replacements.items():
                self._log(f"{title}：{source} → {target}")
        self._record_change(
            select,
            rating_key,
            title,
            field,
            old_tags,
            new_tags,
            not self.dry_run,
            self.field_locked(metadata, field),
        )
        self._progress(changes_delta=len(replacements))

    def _process_tags(
        self, select: List[Any], rating_key: str, title: str, metadata: Dict[str, Any], fields: List[str]
    ) -> None:
        for field_name in ("genre", "style", "mood"):
            if field_name in fields:
                self._process_tag_field(select, rating_key, title, metadata, field_name)

    def process_item(self, args: Any) -> None:
        select, rating_key = args
        metadata = self.get_metadata(rating_key)
        title = metadata["title"]
        self._process_title_sort(select, rating_key, title, metadata.get("titleSort", ""), metadata)
        if select[1] != TYPE["track"]:
            self._process_tags(select, rating_key, title, metadata, ["genre", "style", "mood"])

    def process_artist(self, args: Any) -> None:
        select, rating_key = args
        metadata = self.get_metadata(rating_key)
        title = metadata["title"]
        self._process_title_sort(select, rating_key, title, metadata.get("titleSort", ""), metadata)
        self._process_tags(select, rating_key, title, metadata, ["genre", "style", "mood"])

    def process_album(self, args: Any) -> None:
        select, rating_key = args
        metadata = self.get_metadata(rating_key)
        title = metadata["title"]
        self._process_title_sort(select, rating_key, title, metadata.get("titleSort", ""), metadata)
        self._process_tags(select, rating_key, title, metadata, ["genre", "style", "mood"])

    def process_track(self, args: Any) -> None:
        select, rating_key = args
        metadata = self.get_metadata(rating_key)
        title = metadata["title"]
        self._process_title_sort(select, rating_key, title, metadata.get("titleSort", ""), metadata)
        self._process_tags(select, rating_key, title, metadata, ["genre", "mood"])

    def _run_items(self, worker: Callable[[Any], None], items: List[Any]) -> None:
        if not items:
            return
        configured_workers = max(1, int(os.environ.get("MSM_ITEM_WORKERS", "4")))
        max_workers = min(configured_workers, max(1, len(items)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(worker, item) for item in items]
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except TaskCancelled:
                    for pending in futures:
                        pending.cancel()
                    raise
                except Exception as exc:
                    self._log(f"处理项目失败：{exc}")
                    self._progress(errors_delta=1)
                finally:
                    self._progress(processed_delta=1)

    def _library_selects(self, library: List[Any]) -> List[List[Any]]:
        types: tuple[int, ...]
        if library[1] == TYPE["artist"]:
            types = (TYPE["artist"], TYPE["album"], TYPE["track"])
        else:
            types = (library[1],)
        return [[library[0], plex_type] for plex_type in types if self._type_allowed(plex_type)]

    def _prepare_work_plan(self) -> None:
        if self._prepared_items is not None and self._prepared_collections is not None:
            return
        items: List[Dict[str, Any]] = []
        collections: List[Dict[str, Any]] = []
        for library in self.list_library():
            self._check_cancelled()
            if library[1] == TYPE["photo"] or library[2] in self.skip_libraries:
                continue
            if not self._library_allowed(library):
                continue
            for select in self._library_selects(library):
                keys = self.list_media_keys(select, print_counts=False)
                items.append({"library": library, "select": select, "keys": keys})
            if self._field_allowed("collections"):
                response = self._request(
                    "GET", f"{self.host}/library/sections/{library[0]}/collections"
                ).json()
                collections.append(
                    {
                        "library": library,
                        "select": library[:2],
                        "items": response.get("MediaContainer", {}).get("Metadata", []) or [],
                    }
                )
        self._prepared_items = items
        self._prepared_collections = collections

    def count_all_items(self) -> int:
        self._prepare_work_plan()
        return sum(len(item["keys"]) for item in self._prepared_items or []) + sum(
            len(item["items"]) for item in self._prepared_collections or []
        )

    def loop_all(self) -> None:
        self._prepare_work_plan()
        workers = {
            TYPE["artist"]: self.process_artist,
            TYPE["album"]: self.process_album,
            TYPE["track"]: self.process_track,
        }
        for item in self._prepared_items or []:
            self._check_cancelled()
            library = item["library"]
            select = item["select"]
            keys = item["keys"]
            self._progress(stage="items", library=library[2])
            type_label = TYPE_NAME.get(select[1], str(select[1]))
            self._log(f"处理库：{library[2]} · {type_label} · 项目数：{len(keys)}")
            worker = workers.get(select[1], self.process_item)
            self._run_items(worker, [(select, key) for key in keys])
        self._log("")

    def put_collection_title_sort(
        self, select: List[Any], rating_key: str, sort_title: str, lock: int
    ) -> None:
        self.put_title_sort(select, rating_key, sort_title, lock)

    def loop_all_collections(self) -> None:
        if not self._field_allowed("collections"):
            return
        self._prepare_work_plan()
        for item in self._prepared_collections or []:
            self._check_cancelled()
            library = item["library"]
            select = item["select"]
            self._progress(stage="collections", library=library[2])
            self._log(f"处理库：{library[2]}")
            collections = item["items"]
            self._log(f"合集数：{len(collections)}")
            for collection in collections:
                self._check_cancelled()
                try:
                    rating_key = collection["ratingKey"]
                    title = collection["title"]
                    title_sort = collection.get("titleSort", "")
                    self._process_title_sort(select, rating_key, title, title_sort, collection)
                except TaskCancelled:
                    raise
                except Exception as exc:
                    self._log(f"处理合集失败：{exc}")
                    self._progress(errors_delta=1)
                finally:
                    self._progress(processed_delta=1)
            self._log("")
        self._prepared_items = None
        self._prepared_collections = None

    def process_new_collections(self, library_section_id: int, new_collections: List[str]) -> None:
        library = next((lib for lib in self.list_library() if lib[0] == library_section_id), None)
        if not library:
            return
        select = library[:2]
        response = self._request(
            "GET", f"{self.host}/library/sections/{select[0]}/collections?sort=titleSort:desc"
        ).json()
        collections = response.get("MediaContainer", {}).get("Metadata", []) or []
        for collection in collections:
            if collection.get("guid") not in new_collections:
                continue
            title = collection["title"]
            self._process_title_sort(select, collection["ratingKey"], title, collection.get("titleSort", ""))

    def process_new_item(self, metadata: Dict[str, Any]) -> None:
        rating_key = metadata["ratingKey"]
        library_section_id = int(metadata["librarySectionID"])
        media_type = str(metadata["type"])
        if media_type == "episode":
            self._log("跳过剧集事件。")
            return
        library = next((lib for lib in self.list_library() if lib[0] == library_section_id), None)
        if library is None:
            self._log("未找到事件对应的媒体库。")
            return
        library_title = library[2]
        if library_title in self.skip_libraries:
            self._log(f"跳过媒体库：{library_title}")
            return
        self._progress(stage="webhook", library=library_title, total=1)
        plex_type = TYPE.get(media_type, library[1])
        select = [library_section_id, plex_type]
        if media_type == "artist":
            self.process_artist((select, rating_key))
            albums = self.get_children(str(rating_key))
            album_items = [
                ([library_section_id, TYPE["album"]], str(album["ratingKey"]))
                for album in albums
                if album.get("ratingKey")
            ]
            self._run_items(self.process_album, album_items)
            for _, album_key in album_items:
                tracks = self.get_children(album_key)
                self._run_items(
                    self.process_track,
                    [
                        ([library_section_id, TYPE["track"]], str(track["ratingKey"]))
                        for track in tracks
                        if track.get("ratingKey")
                    ],
                )
        elif media_type == "album":
            self.process_album((select, rating_key))
            tracks = self.get_children(str(rating_key))
            self._run_items(
                self.process_track,
                [
                    ([library_section_id, TYPE["track"]], str(track["ratingKey"]))
                    for track in tracks
                    if track.get("ratingKey")
                ],
            )
        elif media_type == "track":
            self.process_track((select, rating_key))
        else:
            self.process_item((select, rating_key))
        self._progress(processed_delta=1)
        if "Collection" in metadata:
            new_collections = [collection["guid"] for collection in metadata["Collection"]]
            self.process_new_collections(library_section_id, new_collections)

    def apply_change_value(
        self,
        change: Dict[str, Any],
        value: Any,
        locked: Optional[bool] = True,
    ) -> None:
        media_type = change.get("media_type")
        plex_type = TYPE[media_type] if media_type in TYPE else int(media_type or 1)
        select = [int(change["library_id"]), plex_type]
        rating_key = str(change["rating_key"])
        field = change["field"]
        lock = 1 if locked is not False else 0
        if field == "titleSort":
            self.put_title_sort(select, rating_key, str(value or ""), lock)
            return
        if field in {"genre", "style", "mood"}:
            tags = value if isinstance(value, list) else []
            self._put_tag_field_values(select, rating_key, [str(tag) for tag in tags], field, lock=lock)
            return
        raise ValueError(f"不支持回滚字段：{field}")

    def apply_recorded_change(self, change: Dict[str, Any]) -> None:
        self.apply_change_value(change, change.get("old_value"), change.get("old_locked"))

    def scan_unmapped_tags(self) -> Dict[str, Dict[str, int]]:
        suggestions: Dict[str, Dict[str, int]] = {"genre": {}, "style": {}, "mood": {}}
        for library in self.list_library():
            if library[1] == TYPE["photo"] or library[2] in self.skip_libraries:
                continue
            if not self._library_allowed(library):
                continue
            selects = (
                [[library[0], TYPE["artist"]], [library[0], TYPE["album"]], [library[0], TYPE["track"]]]
                if library[1] == TYPE["artist"]
                else [library[:2]]
            )
            for select in selects:
                for rating_key in self.list_media_keys(select, print_counts=False):
                    try:
                        metadata = self.get_metadata(rating_key)
                    except TaskCancelled:
                        raise
                    except Exception as exc:
                        self._log(f"扫描标签失败：{exc}")
                        continue
                    for field_name, plex_field in (("genre", "Genre"), ("style", "Style"), ("mood", "Mood")):
                        for item in metadata.get(plex_field, []) or []:
                            tag = item.get("tag")
                            if tag and tag not in self.tags:
                                suggestions[field_name][tag] = suggestions[field_name].get(tag, 0) + 1
                    self._progress(processed_delta=1)
        return suggestions

    def scan_library(self, library_id: int) -> None:
        self._request("GET", f"{self.host}/library/sections/{library_id}/refresh")

    def refresh_metadata(self, rating_key: str) -> None:
        self._request("PUT", f"{self.host}/library/metadata/{rating_key}/refresh")

    def analyze_metadata(self, rating_key: str) -> None:
        self._request("PUT", f"{self.host}/library/metadata/{rating_key}/analyze")

    def activities(self) -> Dict[str, Any]:
        return self._request("GET", f"{self.host}/activities").json()
