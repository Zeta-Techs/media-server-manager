from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from media_server_manager.config import settings  # noqa: E402
from media_server_manager.infrastructure.db import models  # noqa: E402,F401
from media_server_manager.infrastructure.db.session import Base  # noqa: E402

config = context.config
settings.ensure_directories()
configured_url = config.get_main_option("sqlalchemy.url")
if not configured_url or (configured_url == "sqlite:///data/media_server_manager.sqlite3" and settings.database_url):
    config.set_main_option("sqlalchemy.url", settings.database_url)
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _include_object(obj, name, object_type, reflected, compare_to):
    # The compatibility baseline stores several JSON and timestamp fields as
    # TEXT. Revision state and table coverage are checked now; column-level
    # type/index comparison will be enabled as each legacy table is promoted.
    if object_type == "column":
        return False
    if object_type in {"index", "foreign_key_constraint", "unique_constraint", "primary_key_constraint"}:
        return False
    if object_type == "table" and reflected and compare_to is None:
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    try:
        with connectable.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                include_object=_include_object,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        # Explicit disposal matters on Windows where an SQLite handle keeps
        # the migrated database locked after the CLI command returns.
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
