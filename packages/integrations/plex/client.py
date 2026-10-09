from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class PlexConnectionError(ConnectionError):
    pass


class PlexAuthenticationError(RuntimeError):
    pass


class PlexRateLimitError(PlexConnectionError):
    pass


@dataclass(frozen=True, slots=True)
class PlexServerInfo:
    name: str
    version: str
    product: str
    platform: str


class PlexClient:
    def __init__(self, address: str, token: str, *, timeout: tuple[float, float] = (5.0, 30.0)):
        normalized = address.strip().rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("server address must be a valid http or https URL")
        if not token.strip():
            raise ValueError("server token is required")
        self.address = normalized + "/"
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json", "X-Plex-Product": "Media Server Manager", "X-Plex-Token": token})
        retry = Retry(total=2, connect=2, read=2, status=2, backoff_factor=0.25, status_forcelist=(429, 502, 503, 504), allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}), raise_on_status=False, respect_retry_after_header=True)
        self.session.mount("http://", HTTPAdapter(max_retries=retry))
        self.session.mount("https://", HTTPAdapter(max_retries=retry))

    def _get(self, path: str) -> dict[str, Any]:
        try:
            response = self.session.get(urljoin(self.address, path.lstrip("/")), timeout=self.timeout)
        except requests.RequestException as exc:
            raise PlexConnectionError(str(exc)) from exc
        if response.status_code in {401, 403}:
            raise PlexAuthenticationError("Plex token rejected")
        if response.status_code == 429:
            raise PlexRateLimitError("Plex rate limit exceeded")
        try:
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise PlexConnectionError(f"invalid Plex response: {exc}") from exc
        return payload if isinstance(payload, dict) else {}

    def test_connection(self) -> PlexServerInfo:
        payload = self._get("/")
        container = payload.get("MediaContainer") or payload
        return PlexServerInfo(name=str(container.get("friendlyName") or container.get("name") or "Plex"), version=str(container.get("version") or ""), product=str(container.get("product") or "Plex Media Server"), platform=str(container.get("platform") or ""))

    def list_libraries(self) -> list[dict[str, Any]]:
        payload = self._get("/library/sections")
        return list((payload.get("MediaContainer") or {}).get("Directory") or [])

    def scan_library(self, library_id: int) -> dict[str, Any]:
        return self._get(f"/library/metadata/{library_id}")
