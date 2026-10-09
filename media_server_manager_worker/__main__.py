from __future__ import annotations

import argparse
import os
import sys

from media_server_manager_web.db import DB_FILE, init_db

from .worker import Worker, worker_is_healthy


def main(argv: list[str] | None = None) -> int:
    if os.environ.get("MSM_ENABLE_LEGACY_RUNTIME") != "1":
        print("Legacy SQLite Worker is disabled. Start Celery with apps.worker.main:celery_app.", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description="Media Server Manager 持久任务 Worker")
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args(argv)
    if args.healthcheck:
        return 0 if worker_is_healthy(DB_FILE) else 1
    init_db(DB_FILE)
    concurrency = max(1, int(os.environ.get("MSM_WORKER_CONCURRENCY", "2")))
    Worker(DB_FILE, concurrency=concurrency).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
