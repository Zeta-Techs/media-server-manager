from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

import requests

from ...config import Settings, settings


class MediaCache:
    """Filesystem cache for large provider assets.

    Metadata belongs in the database; this adapter owns only the binary file
    lifecycle and returns a checksum for the caller to persist.
    """

    def __init__(self, app_settings: Settings = settings) -> None:
        self.root = app_settings.data_dir / "cache" / "tmdb"
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, media_type: str, provider_id: int, image_type: str, suffix: str = ".jpg") -> Path:
        return self.root / media_type / str(provider_id) / f"{image_type}{suffix}"

    def fetch(self, source_path: str, target: Path) -> str:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            response = requests.get(f"https://image.tmdb.org/t/p/w780{source_path}", timeout=30)
            response.raise_for_status()
            fd, temp_name = tempfile.mkstemp(prefix=".download-", dir=target.parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(response.content)
                Path(temp_name).replace(target)
            except Exception:
                Path(temp_name).unlink(missing_ok=True)
                raise
        return hashlib.sha256(target.read_bytes()).hexdigest()
