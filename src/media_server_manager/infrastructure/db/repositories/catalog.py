from __future__ import annotations

from typing import Any

from sqlalchemy import func, literal, or_, select
from sqlalchemy.orm import Session

from ..models import MediaMatchResults, TmdbItems


class CatalogRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def movie_and_tv_items(self) -> list[dict[str, Any]]:
        table = TmdbItems.__table__
        return [dict(row._mapping) for row in self.session.execute(
            select(table).where(table.c.media_type.in_(["movie", "tv"])).order_by(table.c.release_date.desc(), table.c.id.desc())
        )]

    def list_items(self, media_type: str = "", query: str = "", page: int = 1, size: int = 24,
                   server_id: int | None = None, status: str = "") -> tuple[list[dict[str, Any]], int]:
        items = TmdbItems.__table__
        matches = MediaMatchResults.__table__
        stmt = select(items, literal(None).label("match_status"), literal(None).label("match_source"), literal(None).label("plex_rating_key")).select_from(items)
        conditions = []
        if media_type:
            conditions.append(items.c.media_type == media_type)
        if query:
            pattern = f"%{query}%"
            conditions.append(or_(items.c.title.like(pattern), items.c.original_title.like(pattern)))
        if server_id is not None:
            stmt = select(items, matches.c.status.label("match_status"), matches.c.match_source, matches.c.plex_rating_key).select_from(items)
            stmt = stmt.outerjoin(matches, (matches.c.media_type == items.c.media_type) &
                (matches.c.tmdb_id == items.c.tmdb_id) & (matches.c.server_id == server_id) &
                (func.coalesce(matches.c.season_number, -1) == func.coalesce(items.c.season_number, -1)) &
                (func.coalesce(matches.c.episode_number, -1) == func.coalesce(items.c.episode_number, -1)))
            if status:
                conditions.append(matches.c.status == status)
        if conditions:
            stmt = stmt.where(*conditions)
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = int(self.session.scalar(count_stmt) or 0)
        rows = self.session.execute(stmt.order_by(items.c.release_date.desc(), items.c.title).offset((page - 1) * size).limit(size))
        return [dict(row._mapping) for row in rows], total

    def detail(self, media_type: str, tmdb_id: int) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        items = TmdbItems.__table__
        row = self.session.execute(select(items).where(items.c.media_type == media_type, items.c.tmdb_id == tmdb_id)
                                   .order_by(items.c.season_number, items.c.episode_number).limit(1)).mappings().first()
        children = self.session.execute(select(items).where(items.c.parent_tmdb_id == tmdb_id)
                                         .order_by(items.c.season_number, items.c.episode_number)).mappings()
        return (dict(row) if row else None), [dict(item) for item in children]

    def stats(self) -> tuple[dict[str, int], dict[str, int]]:
        items = TmdbItems.__table__
        matches = MediaMatchResults.__table__
        item_counts = {str(row._mapping["media_type"]): int(row._mapping["count"]) for row in self.session.execute(select(items.c.media_type, func.count().label("count")).group_by(items.c.media_type))}
        match_counts = {str(row._mapping["status"]): int(row._mapping["count"]) for row in self.session.execute(select(matches.c.status, func.count().label("count")).group_by(matches.c.status))}
        return item_counts, match_counts
