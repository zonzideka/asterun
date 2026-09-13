"""有界后台执行；工作线程不访问状态库，进度由核心线程确认落盘。"""

from __future__ import annotations

from dataclasses import dataclass, field
from queue import Empty, Full, Queue
from threading import Event, Thread
from typing import Any, Callable


@dataclass
class Update:
    run_id: Any
    result: dict[str, Any]
    acknowledged: Event = field(default_factory=Event)
    error: BaseException | None = None


class Executor:
    def __init__(self) -> None:
        self.updates: Queue[Update] = Queue(maxsize=64)
        self.workers: dict[str, Thread] = {}
        self.stopping = Event()

    def start(self, run_id, action: Callable) -> None:
        if self.stopping.is_set() or run_id.value in self.workers:
            raise RuntimeError("执行器已关闭或运行已派发")

        def publish(result):
            update = Update(run_id, result)
            while not self.stopping.is_set():
                try:
                    self.updates.put(update, timeout=0.05)
                    break
                except Full:
                    continue
            while not self.stopping.is_set():
                if update.acknowledged.wait(0.05):
                    if update.error is not None:
                        raise update.error
                    return
            raise RuntimeError("核心停止，进度未确认")

        def work():
            try:
                result = action(publish, self.stopping)
            except Exception:
                result = {"status": "pending_reconcile", "terminated": False,
                          "error_code": "REMOTE_STATE_UNKNOWN", "summary": "后端结果未确认，需要对账"}
            try:
                publish(result)
            except Exception:
                pass  # 关闭或持久化失败由核心保存未决状态，不允许重发。

        worker = Thread(target=work, name=f"asterun-{run_id.value}", daemon=True)
        self.workers[run_id.value] = worker
        worker.start()

    def drain(self, apply: Callable) -> None:
        while True:
            try:
                update = self.updates.get_nowait()
            except Empty:
                break
            try:
                apply(update.run_id, update.result)
            except BaseException as error:
                update.error = error
                raise
            finally:
                update.acknowledged.set()
        self.workers = {key: worker for key, worker in self.workers.items() if worker.is_alive()}

    def close(self) -> None:
        self.stopping.set()
        for worker in self.workers.values():
            worker.join(timeout=0.2)
