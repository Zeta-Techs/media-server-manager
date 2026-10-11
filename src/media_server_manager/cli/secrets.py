from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import inspect, text

from media_server_manager.infrastructure.db.session import Database
from media_server_manager.infrastructure.security import SecretBox

_FIELDS = {
    "servers": ("token", "webhook_secret"),
    "oauth_flows": ("token",),
    "notification_channels": ("url",),
}
_SETTING_WORDS = ("key", "secret", "token", "credential", "password")


def rotate_secrets(database: Path, old_key: str | bytes, new_key: str | bytes) -> dict[str, Any]:
    """Rotate encrypted columns in one SQLAlchemy transaction."""

    old_box = SecretBox(old_key)
    new_box = SecretBox(new_key)
    changed = 0
    database = Path(database).expanduser().resolve()
    with Database.for_path(database).transaction() as uow:
        inspector = inspect(uow.session.bind)
        for table, columns in _FIELDS.items():
            if not inspector.has_table(table):
                continue
            for column in columns:
                quoted_table = '"' + table.replace('"', '""') + '"'
                quoted_column = '"' + column.replace('"', '""') + '"'
                rows = uow.session.execute(
                    text(f"SELECT rowid AS _rowid, {quoted_column} FROM {quoted_table} WHERE {quoted_column} IS NOT NULL")
                ).mappings()
                for row in rows:
                    value = str(row[column])
                    uow.session.execute(
                        text(f"UPDATE {quoted_table} SET {quoted_column} = :value WHERE rowid = :rowid"),
                        {"value": new_box.encrypt(old_box.decrypt(value)), "rowid": row["_rowid"]},
                    )
                    changed += 1
        if inspector.has_table("settings"):
            rows = uow.session.execute(text("SELECT key, value FROM settings WHERE value IS NOT NULL")).mappings()
            for row in rows:
                key = str(row["key"]).lower()
                if not any(word in key for word in _SETTING_WORDS):
                    continue
                uow.session.execute(
                    text("UPDATE settings SET value = :value WHERE key = :key"),
                    {"value": new_box.encrypt(old_box.decrypt(str(row["value"]))), "key": row["key"]},
                )
                changed += 1
    return {"database": str(database), "rotated_values": changed}
