"""Canonical Worker process entry point."""

from __future__ import annotations

import argparse
import sys

from media_server_manager.config import settings
from media_server_manager.infrastructure.db.migrations import upgrade_head
from media_server_manager.infrastructure.db.runtime import init_db
from media_server_manager.worker.runtime import Worker, worker_is_healthy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Media Server Manager 持久任务 Worker")
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args(argv)
    database = settings.database_path
    if args.healthcheck:
        return 0 if worker_is_healthy(database) else 1
    settings.ensure_directories()
    upgrade_head(database)
    init_db(database)
    Worker(database, concurrency=settings.worker_concurrency).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
