"""有界只读观察器。不会创建、修复、批准、取消或重新提交任何任务。"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import socket
import stat
import time
from typing import Any, Callable

from asterun.contracts import Envelope
from asterun.control_protocol import loads
from asterun.errors import AsterunError, BACKEND_UNAVAILABLE, INVALID_REQUEST, REMOTE_STATE_UNKNOWN
from asterun.service import MAX_MESSAGE


def _read(path: Path, method: str, payload: dict[str, Any], deadline: float) -> Envelope:
    """每个 connect/send/recv 都使用同一截止时间；慢速分片不能重置超时。"""
    if method not in {"task.get", "task.events"}:
        raise AsterunError(INVALID_REQUEST, "观察器仅允许 task.get 和 task.events")
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise AsterunError(INVALID_REQUEST, "核心 socket 类型、所有者或权限不匹配")
    message = json.dumps({"method": method, "payload": payload, "entry": "cli"},
                         ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
    if len(message) > MAX_MESSAGE:
        raise AsterunError(INVALID_REQUEST, "观察请求超过本机 IPC 大小上限")

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("观察请求到期")
        return value

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(remaining())
        conn.connect(str(path))
        conn.settimeout(remaining())
        conn.sendall(message)
        response = bytearray()
        while b"\n" not in response:
            conn.settimeout(remaining())
            chunk = conn.recv(min(65536, MAX_MESSAGE + 1 - len(response)))
            if not chunk:
                raise OSError("观察响应未完成")
            response.extend(chunk)
            if len(response) > MAX_MESSAGE:
                raise AsterunError(INVALID_REQUEST, "观察响应超过本机 IPC 大小上限")
    data = loads(bytes(response.partition(b"\n")[0]))
    if not isinstance(data, dict) or type(data.get("ok")) is not bool:
        raise AsterunError(INVALID_REQUEST, "观察响应不是有效信封")
    return Envelope(**data)


def watch_task(path: Path, task_id: str, *, timeout: float = 60, request_timeout: float = 5,
               interval: float = 0.5, cursor: int = 0, run_id: str | None = None,
               page_size: int = 100, on_events: Callable[[dict[str, Any]], None] | None = None) -> Envelope:
    if any(not math.isfinite(value) or value <= 0 for value in (timeout, request_timeout, interval)):
        raise AsterunError(INVALID_REQUEST, "timeout、request-timeout 和 interval 必须是有限正数")
    if type(cursor) is not int or cursor < 0 or not 1 <= page_size <= 500:
        raise AsterunError(INVALID_REQUEST, "cursor 必须非负，page-size 必须为 1 到 500")
    if cursor and not run_id:
        raise AsterunError(INVALID_REQUEST, "非零 cursor 必须同时指定其所属 --run-id")
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] | None = None
    count = 0
    delay = min(interval, 5.0)

    def finish(reason: str, error: dict[str, Any] | None = None) -> Envelope:
        return Envelope(ok=error is None, ids={"task_id": task_id}, error=error, data={
            "reason": reason, "last_snapshot": latest, "run_id": run_id, "cursor": cursor,
            "requests": count, "readonly": True,
            "next_action": "继续观察时复用 task_id、run_id 与 cursor；不创建新的任务或幂等键",
        })

    def request(method: str, payload: dict[str, Any]) -> Envelope:
        nonlocal count
        if time.monotonic() >= deadline:
            raise TimeoutError("观察总时限已到")
        count += 1
        return _read(path, method, payload, min(deadline, time.monotonic() + request_timeout))

    try:
        while time.monotonic() < deadline:
            snapshot = request("task.get", {"task_id": task_id})
            if not snapshot.ok:
                return finish("read_error", snapshot.error)
            if not isinstance(snapshot.data, dict) or not isinstance(snapshot.data.get("run"), dict):
                return finish("unknown", AsterunError(REMOTE_STATE_UNKNOWN, "任务没有可确认的运行快照").to_dict())
            latest = snapshot.data
            current = latest["run"]
            current_id = current.get("id")
            if not isinstance(current_id, str) or not current_id:
                return finish("unknown", AsterunError(REMOTE_STATE_UNKNOWN, "运行快照缺少运行 ID").to_dict())
            if run_id != current_id:
                run_id, cursor = current_id, 0
            status = current.get("status")
            reason = {
                "succeeded": "terminal", "failed": "terminal", "cancelled": "terminal",
                "waiting_input": "waiting_input", "waiting_auth": "waiting_auth", "paused": "paused",
                "pending_reconcile": "unknown",
            }.get(status)
            if status not in {"queued", "dispatching", "running"} and reason is None:
                reason = "unknown"
            approval = latest.get("approval")
            if isinstance(approval, dict) and approval.get("state") == "pending":
                reason = "waiting_input"
            elif isinstance(approval, dict) and approval.get("state") == "forwarding":
                reason = "unknown"
            changed = False
            # 消费有限事件页；即使持续有新事件，每次请求前也检查固定总时限。
            while time.monotonic() < deadline:
                page = request("task.events", {"task_id": task_id, "cursor": cursor, "page_size": page_size})
                if not page.ok:
                    return finish("read_error", page.error)
                data = page.data
                if not isinstance(data, dict) or not isinstance(data.get("events"), list):
                    raise AsterunError(INVALID_REQUEST, "观察事件页无效")
                if data.get("run_id") != run_id:
                    # 当前 run 在两次读取之间改变；旧 run 的游标不可应用于新 run。
                    run_id, cursor = None, 0
                    reason = None
                    break
                next_cursor = data.get("next_cursor")
                if type(next_cursor) is not int or next_cursor < cursor:
                    raise AsterunError(INVALID_REQUEST, "观察事件游标无效")
                events = data["events"]
                if events and next_cursor <= cursor:
                    raise AsterunError(INVALID_REQUEST, "观察事件页未推进游标")
                if events and on_events:
                    on_events(data)
                cursor = next_cursor
                changed = changed or bool(events)
                if len(events) < page_size:
                    break
            if reason == "unknown":
                return finish(reason, AsterunError(REMOTE_STATE_UNKNOWN, "运行状态未决；请核对原运行，不重新派发").to_dict())
            if reason:
                return finish(reason)
            delay = min(interval, 5.0) if changed else min(delay * 1.5, 5.0)
            time.sleep(min(delay, max(0, deadline - time.monotonic())))
        return finish("deadline")
    except KeyboardInterrupt:
        return finish("interrupted")
    except TimeoutError:
        if time.monotonic() >= deadline:
            return finish("deadline")
        return finish("read_error", AsterunError(BACKEND_UNAVAILABLE,
                      "只读观察请求超时；保留最后快照，未重试或重新提交").to_dict())
    except AsterunError as error:
        return finish("read_error", error.to_dict())
    except (OSError, ValueError, TypeError, UnicodeError):
        return finish("read_error", AsterunError(BACKEND_UNAVAILABLE,
                      "未取得可信观察响应；保留最后快照，未重试或重新提交").to_dict())
