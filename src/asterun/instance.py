from __future__ import annotations

import json
import os
from pathlib import Path

from asterun.errors import INSTANCE_LOCKED, AsterunError

try:
    import fcntl
except ImportError:  # pragma: no cover - 非 POSIX
    fcntl = None  # type: ignore[assignment]


class InstanceLock:
    """同一状态目录只允许一个运行实例持有执行权。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None

    def acquire(self) -> None:
        if self._handle is not None:
            raise AsterunError(INSTANCE_LOCKED, "此实例已经持有执行锁")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._handle = open(self.path, "a+", encoding="utf-8")
        if fcntl is None:
            self._handle.close()
            self._handle = None
            raise AsterunError(INSTANCE_LOCKED, "当前平台没有 fcntl，无法建立单实例边界")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._handle.close()
            self._handle = None
            raise AsterunError(
                INSTANCE_LOCKED,
                "同一状态目录已有运行实例，第二实例不能抢占执行权",
                next_action="等待现有进程退出，或使用另一个 --state-dir",
                details={"lock": str(self.path)},
            ) from exc
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(json.dumps({"pid": os.getpid()}, ensure_ascii=False) + "\n")
        self._handle.flush()

    def release(self) -> None:
        if self._handle is None:
            return
        if fcntl is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None
