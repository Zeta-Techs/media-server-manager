"""Persistence for bulk imports and synthetic media-library entries."""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, insert, select, update
from sqlalchemy.orm import Session

from ..models import MediaLibraries, MediaLibraryBulkBatches, MediaLibraryBulkRows, MediaLibraryItems


class BulkMediaRepository:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.items = MediaLibraryItems.__table__
        self.rows = MediaLibraryBulkRows.__table__
        self.batches = MediaLibraryBulkBatches.__table__
        self.libraries = MediaLibraries.__table__

    def batch(self, batch_id: int) -> dict[str, Any] | None:
        row = (
            self.session.execute(select(self.batches).where(self.batches.c.id == batch_id)).mappings().first()
        )
        return dict(row) if row is not None else None

    def batch_rows(self, batch_id: int) -> list[dict[str, Any]]:
        query = select(self.rows).where(self.rows.c.batch_id == batch_id).order_by(self.rows.c.line_number)
        return [dict(row) for row in self.session.execute(query).mappings()]

    def update_batch(self, batch_id: int, **values: Any) -> None:
        self.session.execute(update(self.batches).where(self.batches.c.id == batch_id).values(**values))

    def update_row(self, row_id: int, **values: Any) -> None:
        self.session.execute(update(self.rows).where(self.rows.c.id == row_id).values(**values))

    def library(self, server_id: int, library_id: int) -> dict[str, Any] | None:
        table = self.libraries
        row = (
            self.session.execute(
                select(table).where(table.c.server_id == server_id, table.c.library_id == library_id)
            )
            .mappings()
            .first()
        )
        return dict(row) if row is not None else None

    def by_tmdb(
        self, server_id: int, library_id: int, media_type: str, tmdb_id: int
    ) -> dict[str, Any] | None:
        table = self.items
        query = (
            select(table)
            .where(
                table.c.server_id == server_id,
                table.c.library_id == library_id,
                table.c.tmdb_media_type == media_type,
                table.c.tmdb_id == tmdb_id,
            )
            .limit(1)
        )
        row = self.session.execute(query).mappings().first()
        return dict(row) if row is not None else None

    def managed_roots(self) -> list[dict[str, Any]]:
        table = self.items
        query = (
            select(table)
            .where(
                table.c.system_managed == 1,
                table.c.parent_rating_key == "",
                table.c.plex_type.in_(["movie", "show"]),
            )
            .order_by(table.c.id)
        )
        return [dict(row) for row in self.session.execute(query).mappings()]

    def plex_roots(
        self,
        server_id: int,
        library_id: int,
        plex_type: str,
        *,
        exclude_key: str | None = None,
        year: int | None = None,
    ) -> list[dict[str, Any]]:
        table = self.items
        query = (
            select(table)
            .where(
                table.c.server_id == server_id,
                table.c.library_id == library_id,
                table.c.system_managed == 0,
                table.c.parent_rating_key == "",
                table.c.plex_type == plex_type,
            )
            .order_by(table.c.id)
        )
        if exclude_key is not None:
            query = query.where(table.c.rating_key != exclude_key)
        if year is not None:
            query = query.where(table.c.year == year)
        return [dict(row) for row in self.session.execute(query).mappings()]

    def assign_tmdb(
        self,
        server_id: int,
        library_id: int,
        rating_key: str,
        media_type: str,
        tmdb_id: int,
        *,
        only_missing: bool = False,
    ) -> None:
        table = self.items
        query = update(table).where(
            table.c.server_id == server_id, table.c.library_id == library_id, table.c.rating_key == rating_key
        )
        if only_missing:
            query = query.where(table.c.tmdb_id.is_(None) | (table.c.tmdb_id == 0))
        self.session.execute(query.values(tmdb_media_type=media_type, tmdb_id=tmdb_id))

    def delete_children(self, server_id: int, library_id: int, rating_key: str) -> None:
        table = self.items
        self.session.execute(
            delete(table).where(
                table.c.server_id == server_id,
                table.c.library_id == library_id,
                table.c.parent_rating_key == rating_key,
            )
        )

    def delete_item(self, server_id: int, library_id: int, rating_key: str) -> None:
        table = self.items
        self.session.execute(
            delete(table).where(
                table.c.server_id == server_id,
                table.c.library_id == library_id,
                table.c.rating_key == rating_key,
            )
        )

    def add_item(self, **values: Any) -> None:
        self.session.execute(insert(self.items).values(**values))
