"""有界队列与每后端并发。候选默认值来自调优规划，不是已达指标。"""

from __future__ import annotations

from dataclasses import dataclass, field

from asterun.config import SchedulerConfig
from asterun.errors import RATE_LIMITED, AsterunError
from asterun.ids import RunId, TaskId


@dataclass
class QueueItem:
    backend: str
    task_id: TaskId
    run_id: RunId
    workspace: str | None = None


@dataclass
class Scheduler:
    config: SchedulerConfig
    inflight: dict[str, int] = field(default_factory=dict)
    inflight_runs: set[str] = field(default_factory=set)
    queue: list[QueueItem] = field(default_factory=list)
    workspaces: dict[str, str] = field(default_factory=dict)

    def inflight_for(self, backend: str) -> int:
        return self.inflight.get(backend, 0)

    def can_start(self, backend: str, workspace: str | None = None) -> bool:
        return (self.inflight_for(backend) < self.config.per_backend_concurrency
                and (workspace is None or workspace not in self.workspaces.values()))

    def can_enqueue(self) -> bool:
        return len(self.queue) < self.config.max_queue

    def admit(self, backend: str, workspace: str | None = None) -> str:
        """返回 start 或 queue；满员时抛 RATE_LIMITED。"""
        if self.can_start(backend, workspace):
            return "start"
        if self.can_enqueue():
            return "queue"
        raise AsterunError(
            RATE_LIMITED,
            "等待队列已满，未创建新任务",
            retryable=True,
            next_action="等待运行结束后重试，或提高 scheduler.max_queue（那是候选上限，不是已测容量）",
            details={
                "max_queue": self.config.max_queue,
                "queued": len(self.queue),
                "inflight": self.inflight_for(backend),
                "backend": backend,
            },
        )

    def start(self, backend: str, run_id: RunId | None = None, workspace: str | None = None) -> None:
        self.inflight[backend] = self.inflight_for(backend) + 1
        if run_id is not None:
            self.inflight_runs.add(run_id.value)
            if workspace is not None:
                self.workspaces[run_id.value] = workspace

    def enqueue(self, backend: str, task_id: TaskId, run_id: RunId, workspace: str | None = None) -> None:
        self.queue.append(QueueItem(backend=backend, task_id=task_id, run_id=run_id, workspace=workspace))

    def remove(self, run_id: RunId) -> None:
        self.queue[:] = [item for item in self.queue if item.run_id != run_id]

    def finish(self, backend: str, run_id: RunId | None = None) -> None:
        if run_id is not None and run_id.value not in self.inflight_runs:
            return
        if run_id is not None:
            self.inflight_runs.discard(run_id.value)
            self.workspaces.pop(run_id.value, None)
        current = self.inflight_for(backend)
        if current <= 1:
            self.inflight.pop(backend, None)
        else:
            self.inflight[backend] = current - 1

    def dequeue_ready(self) -> QueueItem | None:
        for index, item in enumerate(self.queue):
            if self.can_start(item.backend, item.workspace):
                return self.queue.pop(index)
        return None

    def snapshot(self) -> dict[str, object]:
        return {
            "max_queue": self.config.max_queue,
            "per_backend_concurrency": self.config.per_backend_concurrency,
            "queued": len(self.queue),
            "inflight": dict(self.inflight),
            "queue": [
                {"backend": item.backend, "task_id": item.task_id.value, "run_id": item.run_id.value}
                for item in self.queue
            ],
            "notes": {
                "tuning_defaults_are_not_measured_slo": True,
            },
        }
