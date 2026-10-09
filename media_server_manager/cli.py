from __future__ import annotations

import argparse
import os
import sys
import time
from uuid import UUID

from sqlalchemy import text

from packages.application.job_orchestration import JobOrchestrationService
from packages.domain.tenancy.context import TenantContext
from packages.infrastructure.db.repositories.jobs import JobRepository
from packages.infrastructure.db.repositories.servers import ServerRepository
from packages.infrastructure.db.session import SessionFactory

TERMINAL_JOB_STATUSES = {"succeeded", "failed", "cancelled", "interrupted"}


def _tenant_id(value: str | None) -> UUID:
    raw = value or os.getenv("MSM_TENANT_ID")
    if not raw:
        raise ValueError("必须通过 --tenant-id 或 MSM_TENANT_ID 指定租户")
    try:
        return UUID(raw)
    except ValueError as exc:
        raise ValueError("租户 ID 必须是 UUID") from exc


def run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="处理 Plex 服务器媒体库项目")
    parser.add_argument("--all", action="store_true", help="处理启用服务器的所有支持项目")
    parser.add_argument("--server-id", type=int, action="append", help="只处理指定服务器，可重复")
    parser.add_argument("--tenant-id", help="租户 UUID，也可使用 MSM_TENANT_ID")
    parser.add_argument("--enqueue-only", action="store_true", help="入队后立即退出")
    args = parser.parse_args(argv)
    if not args.all:
        parser.print_help()
        return 0

    try:
        tenant_id = _tenant_id(args.tenant_id)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    context = TenantContext(tenant_id=tenant_id, user_id="cli", role="tenant_admin")
    scope = {"fields": ["titleSort", "genre", "style", "mood", "collections"]}
    with SessionFactory() as session:
        if session.bind and session.bind.dialect.name == "postgresql":
            session.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_id)})
        servers = ServerRepository(session).list_by_tenant(tenant_id)
        servers = [server for server in servers if server.enabled and (not args.server_id or server.id in args.server_id)]
        if not servers:
            print("没有找到指定租户下启用的 Plex 服务器。", file=sys.stderr)
            return 1
        service = JobOrchestrationService(session)
        jobs = []
        for server in servers:
            job = service.create(context, "localize", server.id, {"mode": "apply", "scope": scope}, f"cli_{time.time_ns()}", None)
            jobs.append((job.id, server.name))
        session.commit()
        for job_id, name in jobs:
            print(f"已为 {name} 创建任务 #{job_id}")
        if args.enqueue_only:
            return 0
        cursors = {job_id: 0 for job_id, _ in jobs}
        remaining = {job_id for job_id, _ in jobs}
        failed = False
        while remaining:
            repository = JobRepository(session)
            for job_id, _ in jobs:
                if job_id not in remaining:
                    continue
                for log in repository.list_logs(tenant_id, job_id, 500):
                    if log.id > cursors[job_id]:
                        cursors[job_id] = log.id
                        print(f"#{job_id} {log.message}")
                job = repository.get_by_id(tenant_id, job_id)
                if job and job.status in TERMINAL_JOB_STATUSES:
                    remaining.remove(job_id)
                    failed = failed or job.status != "succeeded"
                    print(f"任务 #{job_id}：{job.status}")
            if remaining:
                session.rollback()
                time.sleep(1)
        return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
