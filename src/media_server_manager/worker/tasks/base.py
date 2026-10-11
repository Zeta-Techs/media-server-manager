"""Application-service backed job handlers.

Handlers are deliberately thin: queue claiming, retries, heartbeats and
transactions remain in ``worker.runtime`` and ``TaskManager`` owns the
application workflow.  This base gives each task type a stable protocol while
the large historical workflows are split into their domain services.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..contracts import JobContext, JobResult


class ApplicationTask:
    def __init__(self, task_manager, job_type: str | Iterable[str]) -> None:
        self.task_manager = task_manager
        self.job_types = frozenset((job_type,) if isinstance(job_type, str) else job_type)
        if not self.job_types:
            raise ValueError("任务处理器至少需要一个任务类型")
        self.job_type = sorted(self.job_types)[0]

    def run(self, context: JobContext) -> JobResult:
        if context.job_id <= 0:
            raise ValueError("任务 ID 无效")
        job = self.task_manager.get_job(context.job_id)
        if str(job.get("type")) not in self.job_types:
            raise ValueError(
                f"任务处理器类型不匹配：{job.get('type')}，期望 {', '.join(sorted(self.job_types))}"
            )
        self.task_manager.run_job(context.job_id)
        return JobResult("succeeded")


__all__ = ["ApplicationTask"]
