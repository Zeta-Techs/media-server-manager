from __future__ import annotations

from typing import Any

from sqlalchemy import case, func, insert, select, update
from sqlalchemy.orm import Session

from ..models import Job, MediaLibraries, MediaLibraryItems


class MediaLibraryRepository:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.libraries = MediaLibraries.__table__
        self.items = MediaLibraryItems.__table__

    def get(self, server_id: int, library_id: int) -> dict[str, Any] | None:
        row = self.session.execute(
            select(self.libraries).where(
                self.libraries.c.server_id == server_id,
                self.libraries.c.library_id == library_id,
            )
        ).mappings().first()
        return dict(row) if row else None

    def update_settings(self, server_id: int, library_id: int, values: dict[str, Any]) -> None:
        self.session.execute(
            update(self.libraries)
            .where(self.libraries.c.server_id == server_id, self.libraries.c.library_id == library_id)
            .values(**values)
        )

    def library_ids(self, server_id: int) -> set[int]:
        return {
            int(value)
            for value in self.session.scalars(
                select(self.libraries.c.library_id).where(self.libraries.c.server_id == server_id)
            )
        }

    def reorder(self, server_id: int, order: list[int], updated_at: str) -> None:
        for index, library_id in enumerate(order):
            self.session.execute(
                update(self.libraries)
                .where(self.libraries.c.server_id == server_id, self.libraries.c.library_id == library_id)
                .values(display_order=index, updated_at=updated_at)
            )

    def list_with_jobs(self, server_id: int) -> list[dict[str, Any]]:
        rows = self.session.execute(
            select(self.libraries, Job.status.label("job_status"), Job.stage.label("job_stage"),
                   Job.current_library.label("job_current_library"), Job.processed.label("job_processed"),
                   Job.total.label("job_total"), Job.error.label("job_error"))
            .outerjoin(Job, Job.id == self.libraries.c.sync_job_id)
            .where(self.libraries.c.server_id == server_id)
            .order_by(self.libraries.c.display_order, self.libraries.c.title)
        )
        return [dict(row._mapping) for row in rows]

    def bootstrap(self, server_id: int, libraries: list[tuple[int, int, str]], now: str, animation_check) -> None:
        for library_id, plex_type, title in libraries:
            self.session.execute(
                insert(self.libraries)
                .prefix_with("OR IGNORE")
                .values(
                    server_id=server_id,
                    library_id=library_id,
                    title=title,
                    plex_type=plex_type,
                    auto_animation=1 if animation_check(title, plex_type) else 0,
                    updated_at=now,
                )
            )

    def item_counts(self, server_id: int, library_id: int) -> tuple[int, int]:
        parent_count = int(
            self.session.scalar(
                select(func.count(self.items.c.id)).where(
                    self.items.c.server_id == server_id,
                    self.items.c.library_id == library_id,
                    self.items.c.parent_rating_key == "",
                    self.items.c.plex_type.in_(["movie", "show", "collection"]),
                )
            )
            or 0
        )
        episode_count = int(
            self.session.scalar(
                select(func.count(self.items.c.id)).where(
                    self.items.c.server_id == server_id,
                    self.items.c.library_id == library_id,
                    self.items.c.plex_type == "episode",
                )
            )
            or 0
        )
        return parent_count, episode_count

    def parent_items(self, server_id: int, library_id: int) -> list[dict[str, Any]]:
        rows = self.session.execute(
            select(self.items).where(
                self.items.c.server_id == server_id,
                self.items.c.library_id == library_id,
                self.items.c.parent_rating_key == "",
                self.items.c.plex_type.in_(["movie", "show", "collection"]),
            )
        )
        return [dict(row._mapping) for row in rows]

    def child_counts(self, server_id: int, library_id: int) -> dict[str, dict[str, Any]]:
        query = (
            select(
                self.items.c.parent_rating_key,
                func.count(case((self.items.c.plex_type == "season", self.items.c.season_number))).label(
                    "season_count"
                ),
                func.count(case((self.items.c.plex_type == "episode", 1))).label("episode_count"),
                func.sum(
                    case(
                        (
                            (self.items.c.plex_type == "episode")
                            & (func.json_extract(self.items.c.raw_json, "$.expected") == 1),
                            1,
                        ),
                        else_=0,
                    )
                ).label("missing_count"),
            )
            .where(
                self.items.c.server_id == server_id,
                self.items.c.library_id == library_id,
                self.items.c.parent_rating_key != "",
            )
            .group_by(self.items.c.parent_rating_key)
        )
        return {str(row.parent_rating_key): dict(row._mapping) for row in self.session.execute(query)}

    def has_real_tmdb(self, server_id: int, library_id: int, media_type: str, tmdb_id: int) -> bool:
        return (
            self.session.scalar(
                select(self.items.c.id)
                .where(
                    self.items.c.server_id == server_id,
                    self.items.c.library_id == library_id,
                    self.items.c.tmdb_media_type == media_type,
                    self.items.c.tmdb_id == tmdb_id,
                    self.items.c.system_managed == 0,
                )
                .limit(1)
            )
            is not None
        )

    def all_for_server(self, server_id: int) -> list[dict[str, Any]]:
        return [
            dict(row._mapping)
            for row in self.session.execute(
                select(self.libraries).where(self.libraries.c.server_id == server_id)
            )
        ]

    def searchable_items(self, server_id: int) -> list[dict[str, Any]]:
        return [
            dict(row._mapping)
            for row in self.session.execute(
                select(self.items)
                .where(
                    self.items.c.server_id == server_id,
                    self.items.c.parent_rating_key == "",
                    self.items.c.plex_type.in_(["movie", "show"]),
                )
                .order_by(self.items.c.title)
            )
        ]

    def update_sync_job(self, server_id: int, library_id: int, job_id: int, updated_at: str) -> None:
        self.session.execute(
            update(self.libraries)
            .where(self.libraries.c.server_id == server_id, self.libraries.c.library_id == library_id)
            .values(sync_job_id=job_id, sync_status="queued", sync_error="", updated_at=updated_at)
        )

    def mark_sync_failed(self, server_id: int, library_id: int, error: str, updated_at: str) -> None:
        self.session.execute(
            update(self.libraries)
            .where(self.libraries.c.server_id == server_id, self.libraries.c.library_id == library_id)
            .values(sync_status="failed", sync_error=error, updated_at=updated_at)
        )

    def expected_episode_count(self, server_id: int, library_id: int, parent_rating_key: str) -> int:
        return int(
            self.session.scalar(
                select(func.count(self.items.c.id)).where(
                    self.items.c.server_id == server_id,
                    self.items.c.library_id == library_id,
                    self.items.c.parent_rating_key == parent_rating_key,
                    self.items.c.plex_type == "episode",
                    self.items.c.rating_key.like("tmdb:%"),
                )
            )
            or 0
        )

    def item(self, server_id: int, library_id: int, rating_key: str) -> dict[str, Any] | None:
        row = self.session.execute(
            select(self.items).where(
                self.items.c.server_id == server_id,
                self.items.c.library_id == library_id,
                self.items.c.rating_key == rating_key,
            )
        ).mappings().first()
        return dict(row) if row else None

    def item_by_tmdb(self, server_id: int, library_id: int, media_type: str, tmdb_id: int) -> dict[str, Any] | None:
        row = self.session.execute(
            select(self.items)
            .where(
                self.items.c.server_id == server_id,
                self.items.c.library_id == library_id,
                self.items.c.parent_rating_key == "",
                self.items.c.tmdb_media_type == media_type,
                self.items.c.tmdb_id == tmdb_id,
            )
            .order_by(self.items.c.system_managed.asc(), self.items.c.id.asc())
            .limit(1)
        ).mappings().first()
        return dict(row) if row else None

    def items_by_type(self, server_id: int, library_id: int, plex_type: str) -> list[dict[str, Any]]:
        return [
            dict(row._mapping)
            for row in self.session.execute(
                select(self.items)
                .where(
                    self.items.c.server_id == server_id,
                    self.items.c.library_id == library_id,
                    self.items.c.plex_type == plex_type,
                )
                .order_by(self.items.c.title, self.items.c.id)
            )
        ]

    def children_for_parent(self, server_id: int, library_id: int, parent_rating_key: str, plex_type: str) -> list[dict[str, Any]]:
        """Return cached child rows for a show, ordered for API rendering."""
        order = (self.items.c.season_number, self.items.c.id) if plex_type == "season" else (self.items.c.season_number, self.items.c.episode_number, self.items.c.id)
        return [
            dict(row._mapping)
            for row in self.session.execute(
                select(self.items)
                .where(
                    self.items.c.server_id == server_id,
                    self.items.c.library_id == library_id,
                    self.items.c.parent_rating_key == parent_rating_key,
                    self.items.c.plex_type == plex_type,
                )
                .order_by(*order)
            )
        ]

    def enabled_server(self, server_id: int) -> dict[str, Any] | None:
        """Return an enabled server row for route validation."""
        from ..models import Server

        row = self.session.execute(
            select(Server.__table__).where(Server.__table__.c.id == server_id, Server.__table__.c.enabled == 1)
        ).mappings().first()
        return dict(row) if row else None
