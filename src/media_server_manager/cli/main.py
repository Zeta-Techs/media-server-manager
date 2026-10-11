from __future__ import annotations

import argparse
import sys
import time

from media_server_manager.application.job_orchestration.queue import TERMINAL_JOB_STATUSES, JobQueue
from media_server_manager.application.server_management.service import ServerManagementService
from media_server_manager.config import settings
from media_server_manager.infrastructure.db.runtime import init_db
from media_server_manager.infrastructure.db.session import Database


def run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="处理 Plex 服务器媒体库项目")
    parser.add_argument("--all", action="store_true", help="处理启用服务器的所有支持项目")
    parser.add_argument("--server-id", type=int, action="append", help="只处理指定服务器，可重复")
    parser.add_argument("--enqueue-only", action="store_true", help="入队后立即退出")
    args = parser.parse_args(argv)
    if not args.all:
        parser.print_help()
        return 0

    database = settings.database_path
    init_db(database)
    queue = JobQueue(database)
    if not args.enqueue_only and not queue.worker_status()["online"]:
        print("任务 Worker 离线；请先启动 media-server-manager-worker，或使用 --enqueue-only。", file=sys.stderr)
        return 2

    with Database.for_path(database).session() as db:
        servers = ServerManagementService(db).enabled_servers()
        selected = set(args.server_id or [])
        rows = [server for server in servers if not selected or server.id in selected]
    if not rows:
        print("没有找到启用的 Plex 服务器。", file=sys.stderr)
        return 1

    scope = {"fields": ["titleSort", "genre", "style", "mood", "collections"]}
    job_ids = []
    for row in rows:
        job_id = queue.create_job("localize", int(row.id), {"mode": "apply", "scope": scope})
        job_ids.append(job_id)
        print(f"已为 {row.name} 创建任务 #{job_id}")
    if args.enqueue_only:
        return 0

    cursors = {job_id: 0 for job_id in job_ids}
    remaining = set(job_ids)
    failed = False
    while remaining:
        for job_id in list(remaining):
            for log in queue.list_logs(job_id, after_id=cursors[job_id], limit=500):
                cursors[job_id] = int(log["id"])
                print(f"#{job_id} {log['message']}")
            job = queue.get_job(job_id)
            if job["status"] in TERMINAL_JOB_STATUSES:
                remaining.remove(job_id)
                failed = failed or job["status"] != "succeeded"
                print(f"任务 #{job_id}：{job['status']}")
        if remaining:
            time.sleep(1)
    return 1 if failed else 0


def main() -> int:
    return run_cli()
