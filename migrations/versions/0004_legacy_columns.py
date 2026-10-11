"""Promote columns that were added by the last hand-written schema upgrade."""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0004_legacy_columns"
down_revision = "0003_complete_legacy_tables"
branch_labels = None
depends_on = None


def _add_column(table: str, column: sa.Column) -> None:
    bind = op.get_bind()
    names = {item["name"] for item in inspect(bind).get_columns(table)}
    if column.name not in names:
        op.add_column(table, column)


def upgrade() -> None:
    _add_column("media_libraries", sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"))
    _add_column(
        "media_libraries",
        sa.Column("auto_recheck_new_episodes", sa.Integer(), nullable=False, server_default="0"),
    )
    _add_column("media_libraries", sa.Column("sort_key", sa.Text(), nullable=False, server_default=""))
    _add_column(
        "media_libraries", sa.Column("sort_direction", sa.Text(), nullable=False, server_default="asc")
    )
    _add_column(
        "media_library_recheck_requests",
        sa.Column("dispatched_event_at", sa.Text(), nullable=False, server_default=""),
    )
    _add_column("media_library_recheck_requests", sa.Column("missing_count", sa.Integer(), nullable=True))

    for _name, column in {
        "tmdb_media_type": sa.Column("tmdb_media_type", sa.Text(), nullable=False, server_default=""),
        "tmdb_id": sa.Column("tmdb_id", sa.Integer(), nullable=True),
        "imdb_id": sa.Column("imdb_id", sa.Text(), nullable=False, server_default=""),
        "tvdb_id": sa.Column("tvdb_id", sa.Text(), nullable=False, server_default=""),
        "source": sa.Column("source", sa.Text(), nullable=False, server_default="plex"),
        "system_managed": sa.Column("system_managed", sa.Integer(), nullable=False, server_default="0"),
        "resolution": sa.Column("resolution", sa.Text(), nullable=False, server_default=""),
        "animation_kind": sa.Column("animation_kind", sa.Text(), nullable=False, server_default=""),
        "system_genres": sa.Column("system_genres", sa.Text(), nullable=False, server_default="[]"),
    }.items():
        _add_column("media_library_items", column)

    _add_column("tmdb_items", sa.Column("imdb_id", sa.Text(), nullable=False, server_default=""))
    _add_column("tmdb_items", sa.Column("tvdb_id", sa.Text(), nullable=False, server_default=""))

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT server_id, library_id, rating_key, raw_json, tmdb_id, imdb_id, tvdb_id "
            "FROM media_library_items WHERE imdb_id = '' OR tvdb_id = '' OR tmdb_id IS NULL"
        )
    ).mappings()
    for row in rows:
        try:
            payload = json.loads(row["raw_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        ids = payload.get("external_ids") or {}
        if not isinstance(ids, dict) or not ids:
            continue
        values = {
            "tmdb_id": int(ids["tmdb"]) if ids.get("tmdb") and not row["tmdb_id"] else row["tmdb_id"],
            "imdb_id": str(ids.get("imdb") or row["imdb_id"] or ""),
            "tvdb_id": str(ids.get("tvdb") or row["tvdb_id"] or ""),
        }
        bind.execute(
            sa.text(
                "UPDATE media_library_items SET tmdb_id=:tmdb_id, imdb_id=:imdb_id, tvdb_id=:tvdb_id "
                "WHERE server_id=:server_id AND library_id=:library_id AND rating_key=:rating_key"
            ),
            {**values, "server_id": row["server_id"], "library_id": row["library_id"], "rating_key": row["rating_key"]},
        )


def downgrade() -> None:
    # These columns may have been populated by the application after upgrade;
    # dropping them would destroy user data. Restore from a SQLite backup.
    raise RuntimeError("legacy column promotion cannot be downgraded safely; restore a database backup")
