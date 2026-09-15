"""有界只读观察器。不会创建、修复、批准、取消或重新提交任何任务。"""

from __future__ import annotations

from copy import deepcopy
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


COMPACT_SUMMARY_CHARS = 1000


def _fields(value: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    return {name: deepcopy(value[name]) for name in names if name in value}


def _bounded_summary(value: dict[str, Any], result: dict[str, Any]) -> None:
    summary = value.get("summary")
    if not isinstance(summary, str):
        return
    result["summary"] = summary[:COMPACT_SUMMARY_CHARS]
    result["summary_truncated"] = bool(value.get("summary_truncated")) or len(summary) > COMPACT_SUMMARY_CHARS
    # 服务端可能已投影；再次投影时保留原文长度和截断事实。
    prior_length = value.get("summary_chars")
    result["summary_chars"] = max(len(summary), prior_length) if type(prior_length) is int else len(summary)


def compact_task_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """保留状态判断和续查引用，不返回任务正文、完整原生日志或审批执行内容。"""
    result: dict[str, Any] = {"compact": True}
    task = snapshot.get("task")
    if isinstance(task, dict):
        result["task"] = _fields(task, (
            "id", "workspace", "backend", "script", "acceptance", "delivery", "created_at",
            "current_run_id", "conversation_id", "account_ref", "model_id", "billing_pool_ref",
            "runtime_ref", "config_revision", "paused", "orchestration_owner", "evidence_stale",
            "input_hash", "evidence_input_hash", "repair_count", "review_status", "run_count", "capability",
            "external_require_review",
        ))
        if isinstance(task.get("external_evaluation"), dict):
            result["task"]["external_evaluation"] = _fields(task["external_evaluation"], (
                "source", "expected_run_id", "expected_revision", "expected_input_hash", "target_hash",
            ))
        if isinstance(task.get("findings"), list):
            result["task"]["findings_count"] = len(task["findings"])
        elif "findings_count" in task:
            result["task"]["findings_count"] = task["findings_count"]
    elif "task" in snapshot:
        result["task"] = task
    run = snapshot.get("run")
    if isinstance(run, dict):
        short_run = _fields(run, (
            "id", "task_id", "backend", "status", "created_at", "cancel_requested", "terminated",
            "error_code", "role", "conversation_id", "target_hash",
        ))
        _bounded_summary(run, short_run)
        native = run.get("native")
        if isinstance(native, dict):
            short_run["native"] = _fields(native, (
                "session_id", "thread_id", "turn_id", "backend_session_id", "backend_turn_id",
                "native_resumed", "native_checked", "execution_profile", "transport", "stop_reason",
                "tool_calls_count", "num_turns", "summary_truncated", "events_truncated", "usage",
                "modelUsage", "total_cost_usd", "total_cost_usd_ticks", "usage_source",
                "usage_is_incomplete", "cost_is_partial", "token_usage", "usage_scope",
            ))
        if "output" in run or "output_available" in run:
            short_run["output_available"] = run.get("output_available", run.get("output") is not None)
        result["run"] = short_run
    elif "run" in snapshot:
        result["run"] = run
    session = snapshot.get("session")
    if isinstance(session, dict):
        result["session"] = _fields(session, (
            "conversation_id", "backend", "workspace", "account_ref", "runtime_ref", "created_at",
            "updated_at", "backend_session_id", "latest_turn_id", "task_id", "config_revision",
        ))
    elif "session" in snapshot:
        result["session"] = session
    approval = snapshot.get("approval")
    if isinstance(approval, dict):
        result["approval"] = _fields(approval, (
            "id", "task_id", "run_id", "state", "created_at", "expires_at", "target_hash",
            "decision_source", "response_status", "native_confirmed",
        ))
        _bounded_summary(approval, result["approval"])
    elif "approval" in snapshot:
        result["approval"] = approval
    notes = snapshot.get("notes")
    if isinstance(notes, dict):
        result["notes"] = _fields(notes, ("run_success_is_not_acceptance", "fake_is_not_real_connection"))
    result["details"] = {"method": "task.get", "compact": False}
    if isinstance(task, dict) and isinstance(task.get("id"), str):
        result["details"]["task_id"] = task["id"]
    return result


def _compact_event_page(page: dict[str, Any]) -> dict[str, Any]:
    result = _fields(page, ("task_id", "run_id", "cursor", "next_cursor"))
    result["compact"] = True
    result["events"] = []
    for event in page["events"]:
        if not isinstance(event, dict):
            raise AsterunError(INVALID_REQUEST, "观察事件不是有效对象")
        short_event = _fields(event, ("seq", "type", "source", "timestamp", "schema_version"))
        payload = event.get("payload")
        if isinstance(payload, dict):
            short_event["payload"] = _fields(payload, (
                "kind", "status", "toolName", "toolCallId", "tool_kind", "usage", "error_code",
                "approval_id", "run_id", "task_id",
            ))
        result["events"].append(short_event)
    return result


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
               page_size: int = 100, on_events: Callable[[dict[str, Any]], None] | None = None,
               compact: bool = False, include_events: bool = True) -> Envelope:
    if type(compact) is not bool or type(include_events) is not bool:
        raise AsterunError(INVALID_REQUEST, "compact 和 include-events 必须是布尔值")
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
        data = {
            "reason": reason, "last_snapshot": compact_task_snapshot(latest) if compact and latest is not None else latest,
            "run_id": run_id, "cursor": cursor,
            "requests": count, "readonly": True,
            "next_action": "继续观察时复用 task_id、run_id 与 cursor；不创建新的任务或幂等键",
        }
        if compact:
            data["compact"] = True
        if not include_events:
            data["events_skipped"] = True
        return Envelope(ok=error is None, ids={"task_id": task_id}, error=error, data=data)

    def request(method: str, payload: dict[str, Any]) -> Envelope:
        nonlocal count
        if time.monotonic() >= deadline:
            raise TimeoutError("观察总时限已到")
        count += 1
        return _read(path, method, payload, min(deadline, time.monotonic() + request_timeout))

    try:
        while time.monotonic() < deadline:
            snapshot = request("task.get", {"task_id": task_id, **({"compact": True} if compact else {})})
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
            while include_events and time.monotonic() < deadline:
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
                    on_events(_compact_event_page(data) if compact else data)
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
