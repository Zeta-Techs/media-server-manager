from __future__ import annotations

import re
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List

import pypinyin

from .config import settings

if TYPE_CHECKING:
    from .infrastructure.integrations.plex.client import PlexServer  # noqa: F401

BASE_DIR = Path(__file__).resolve().parents[2]
CONFIG_DIR = settings.data_dir
DATA_DIR = settings.data_dir
TEMPLATE_TAGS_FILE = Path(__file__).resolve().parent / "seeds" / "default_tags.json"

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





def __getattr__(name: str):
    if name == "PlexServer":
        from .infrastructure.integrations.plex.client import PlexServer

        return PlexServer
    raise AttributeError(name)
