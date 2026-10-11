"""Canonical Web process entry point."""

from __future__ import annotations

from waitress import serve

from media_server_manager.api.app_factory import create_app
from media_server_manager.config import settings
from media_server_manager.infrastructure.db.migrations import upgrade_head
from media_server_manager.infrastructure.db.runtime import init_db


def main() -> int:
    settings.ensure_directories()
    database = settings.database_path
    upgrade_head(database)
    init_db(database)
    serve(create_app(), host=settings.web_host, port=settings.web_port, threads=settings.web_threads)
    return 0
