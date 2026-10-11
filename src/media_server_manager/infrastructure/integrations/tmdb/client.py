from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class TMDBError(RuntimeError):
    def __init__(self, message: str, code: str = "tmdb_error", status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class TMDBClient:
    def __init__(self, api_key: str, timeout: int = 20) -> None:
        self.api_key = (api_key or "").strip()
        if not self.api_key:
            raise ValueError("请先配置 TMDB API Read Access Token")
        self.timeout = timeout
        self.base_url = "https://api.themoviedb.org/3"
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json", "Authorization": f"Bearer {self.api_key}"})
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            status=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))

    def _get(self, path: str, **params: Any) -> Dict[str, Any]:
        query = {key: value for key, value in params.items() if value not in (None, "")}
        response = self.session.get(f"{self.base_url}{path}", params=query, timeout=self.timeout)
        if response.status_code == 401:
            raise TMDBError("TMDB Token 无效或无权限", "tmdb_unauthorized", 401)
        if response.status_code == 404:
            raise TMDBError("TMDB 未找到对应资源", "tmdb_not_found", 404)
        if response.status_code == 429:
            raise TMDBError("TMDB 请求过于频繁，请稍后重试", "tmdb_rate_limited", 429)
        if response.status_code >= 500:
            raise TMDBError("TMDB 服务暂时不可用", "tmdb_unavailable", response.status_code)
        response.raise_for_status()
        return response.json()

    def discover(self, media_type: str, page: int = 1, **filters: Any) -> Dict[str, Any]:
        if media_type not in {"movie", "tv"}:
            raise ValueError("仅支持 movie 或 tv")
        return self._get(f"/discover/{media_type}", page=page, **filters)

    def details(self, media_type: str, tmdb_id: int, language: str = "zh-CN") -> Dict[str, Any]:
        append = "credits,external_ids,images,translations,watch/providers"
        return self._get(f"/{media_type}/{int(tmdb_id)}", language=language, append_to_response=append)

    def movie_details(self, tmdb_id: int) -> Dict[str, Any]:
        return self.details("movie", tmdb_id)

    def search_movie(
        self, title: str, year: Optional[int] = None, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        data = self._get("/search/movie", query=title, year=year)
        results = data.get("results") or []
        return results[:limit] if limit else results

    def episode_details(
        self, series_id: int, season_number: int, episode_number: int, language: str = "zh-CN"
    ) -> Dict[str, Any]:
        return self._get(
            f"/tv/{series_id}/season/{season_number}/episode/{episode_number}",
            language=language,
            append_to_response="images,external_ids",
        )

    def configuration(self) -> Dict[str, Any]:
        return self._get("/configuration")

    def image_url(self, path: str | None, size: str = "w500") -> str:
        return f"https://image.tmdb.org/t/p/{size}/{path.lstrip('/')}" if path else ""

    def find_tv_by_external_id(self, external_id: str, source: str) -> Optional[Dict[str, Any]]:
        source_map = {"tvdb": "tvdb_id", "imdb": "imdb_id"}
        external_source = source_map.get(source)
        if not external_source:
            return None
        data = self._get(f"/find/{external_id}", external_source=external_source)
        results = data.get("tv_results") or []
        return results[0] if results else None

    def find_by_external_id(self, external_id: str) -> Dict[str, Any]:
        value = str(external_id or "").strip()
        if value.lower().startswith("tt"):
            source = "imdb_id"
        elif value.isdigit():
            source = "tvdb_id"
        else:
            return {}
        return self._get(f"/find/{value}", external_source=source)

    def search_tv(
        self, title: str, year: Optional[int] = None, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        data = self._get("/search/tv", query=title, first_air_date_year=year)
        results = data.get("results") or []
        return results[:limit] if limit else results

    def search(
        self, media_type: str, query: str, year: Optional[int] = None, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        if media_type == "movie":
            return self.search_movie(query, year, limit)
        if media_type == "tv":
            return self.search_tv(query, year, limit)
        raise ValueError("仅支持 movie 或 tv")

    def tv_details(self, series_id: int) -> Dict[str, Any]:
        return self._get(f"/tv/{series_id}")

    def season_details(self, series_id: int, season_number: int) -> Dict[str, Any]:
        return self._get(f"/tv/{series_id}/season/{season_number}")
