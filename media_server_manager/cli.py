from __future__ import annotations

import argparse
import sys
import time

from media_server_manager_web.db import DB_FILE, connect, init_db
from media_server_manager_web.services import TERMINAL_JOB_STATUSES, JobQueue


def run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="处理 Plex 服务器媒体库项目")
    parser.add_argument("--all", action="store_true", help="处理启用服务器的所有支持项目")
    parser.add_argument("--server-id", type=int, action="append", help="只处理指定服务器，可重复")
    parser.add_argument("--enqueue-only", action="store_true", help="入队后立即退出")
    args = parser.parse_args(argv)
    if not args.all:
        parser.print_help()
        return 0

    init_db(DB_FILE)
    queue = JobQueue(DB_FILE)
    if not args.enqueue_only and not queue.worker_status()["online"]:
        print("任务 Worker 离线；请先启动 media-server-manager-worker，或使用 --enqueue-only。", file=sys.stderr)
        return 2

    with connect(DB_FILE) as db:
        if args.server_id:
            placeholders = ",".join("?" for _ in args.server_id)
            rows = db.execute(
                f"SELECT id, name FROM servers WHERE enabled = 1 AND id IN ({placeholders}) ORDER BY id",
                args.server_id,
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT id, name FROM servers WHERE enabled = 1 ORDER BY id"
            ).fetchall()
    if not rows:
        print("没有找到启用的 Plex 服务器。", file=sys.stderr)
        return 1

    job_ids = []
    scope = {"fields": ["titleSort", "genre", "style", "mood", "collections"]}
    for row in rows:
        job_id = queue.create_job(
            "localize", int(row["id"]), {"mode": "apply", "scope": scope}
        )
        job_ids.append(job_id)
        print(f"已为 {row['name']} 创建任务 #{job_id}")
    if args.enqueue_only:
        return 0

    log_cursors = {job_id: 0 for job_id in job_ids}
    remaining = set(job_ids)
    failed = False
    while remaining:
        for job_id in list(remaining):
            logs = queue.list_logs(job_id, after_id=log_cursors[job_id], limit=500)
            for log in logs:
                log_cursors[job_id] = int(log["id"])
                print(f"#{job_id} {log['message']}")
            job = queue.get_job(job_id)
            if job["status"] in TERMINAL_JOB_STATUSES:
                remaining.remove(job_id)
                failed = failed or job["status"] != "succeeded"
                print(f"任务 #{job_id}：{job['status']}")
        if remaining:
            time.sleep(1)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(run_cli())

