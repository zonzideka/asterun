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


COMPACT_SUMMARY_CHARS = 1000
COMPACT_FIELD_BYTES = 1024
COMPACT_USAGE_BYTES = 8 * 1024
COMPACT_MODEL_LIMIT = 8
COMPACT_EVENT_PAGE_BYTES = 64 * 1024
_COUNTERS = (
    "input_tokens", "output_tokens", "total_tokens", "reasoning_tokens", "reasoning_output_tokens",
    "cached_input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "cache_write_tokens",
    "cache_write_input_tokens", "prompt_tokens", "completion_tokens", "inputTokens", "outputTokens",
    "totalTokens", "reasoningOutputTokens", "cachedInputTokens", "cacheReadInputTokens",
    "cacheCreationInputTokens", "cacheWriteInputTokens", "costUSD", "cost_usd", "total_cost_usd",
    "costInUsd", "modelContextWindow", "contextWindow", "maxOutputTokens", "webSearchRequests",
    "cached_tokens", "audio_tokens", "accepted_prediction_tokens", "rejected_prediction_tokens",
)


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))


def _small_scalar(value: Any) -> bool:
    if value is None or type(value) is bool:
        return True
    if type(value) is int:
        return value.bit_length() <= 64
    if type(value) is float:
        return math.isfinite(value)
    return isinstance(value, str) and len(value.encode("utf-8")) <= COMPACT_FIELD_BYTES


def _fields(value: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    # 标识超限时省略并标记，不能截成另一个可用于恢复的原生标识。
    result = {name: value[name] for name in names if name in value and _small_scalar(value[name])}
    omitted = {name for name in names if name in value and name not in result}
    prior = value.get("omitted_fields")
    if isinstance(prior, list):
        omitted.update(name for name in names if name in prior)
    if omitted:
        result["omitted_fields"] = sorted(omitted)
    return result


def _counter_fields(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result = {name: value[name] for name in _COUNTERS if name in value
              and type(value[name]) in {int, float} and _small_scalar(value[name]) and value[name] >= 0}
    for name in ("input_tokens_details", "output_tokens_details", "prompt_tokens_details",
                 "completion_tokens_details"):
        if isinstance(value.get(name), dict):
            result[name] = {key: value[name][key] for key in _COUNTERS if key in value[name]
                           and type(value[name][key]) in {int, float}
                           and _small_scalar(value[name][key]) and value[name][key] >= 0}
    return result


def _compact_usage(native: dict[str, Any]) -> dict[str, Any]:
    """只投影已知计数字段；供应商通知、模型名称和分项数量不能撑大观察响应。"""
    result: dict[str, Any] = {}
    for name in ("usage", "token_usage", "modelUsage"):
        if name not in native:
            continue
        raw = native[name]
        if name == "usage":
            result[name] = _counter_fields(raw)
        elif name == "token_usage":
            short = _fields(raw, ("threadId", "turnId")) if isinstance(raw, dict) else {}
            short.update(_counter_fields(raw))
            if isinstance(raw, dict) and isinstance(raw.get("tokenUsage"), dict):
                counters = raw["tokenUsage"]
                short["tokenUsage"] = _counter_fields(counters)
                for period in ("total", "last"):
                    if period in counters:
                        short["tokenUsage"][period] = _counter_fields(counters[period])
            result[name] = short
        else:
            models = {}
            if isinstance(raw, dict):
                for model, counters in raw.items():
                    if len(models) >= COMPACT_MODEL_LIMIT:
                        break
                    if isinstance(model, str) and _small_scalar(model) and isinstance(counters, dict):
                        models[model] = _counter_fields(counters)
            result[name] = models
    if not result and "usage_truncated" not in native:
        return result
    # 预留投影元数据；极端合法计数形状也不能挤掉任务状态或原生恢复引用。
    while _size(result) > COMPACT_USAGE_BYTES - 512:
        for name in ("modelUsage", "usage", "token_usage"):
            if result.get(name):
                result[name].pop(next(reversed(result[name])))
                break
        else:
            break
    if "modelUsage" in result:
        count = len(native["modelUsage"]) if isinstance(native.get("modelUsage"), dict) else 0
        prior_count = native.get("model_usage_count")
        count = max(count, prior_count) if type(prior_count) is int and _small_scalar(prior_count) and prior_count >= 0 else count
        result["model_usage_count"] = count
        result["model_usage_returned"] = len(result["modelUsage"])
    result["usage_truncated"] = native.get("usage_truncated") is True or any(
        result.get(name) != native[name] for name in ("usage", "modelUsage", "token_usage") if name in native)
    result["usage_byte_limit"] = COMPACT_USAGE_BYTES
    result["model_usage_limit"] = COMPACT_MODEL_LIMIT
    return result


def _bounded_summary(value: dict[str, Any], result: dict[str, Any]) -> None:
    summary = value.get("summary")
    if not isinstance(summary, str):
        return
    result["summary"] = summary[:COMPACT_SUMMARY_CHARS]
    result["summary_truncated"] = bool(value.get("summary_truncated")) or len(summary) > COMPACT_SUMMARY_CHARS
    # 服务端可能已投影；再次投影时保留原文长度和截断事实。
    prior_length = value.get("summary_chars")
    result["summary_chars"] = (max(len(summary), prior_length)
                               if type(prior_length) is int and _small_scalar(prior_length) else len(summary))


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
        elif type(task.get("findings_count")) is int and _small_scalar(task["findings_count"]) and task["findings_count"] >= 0:
            result["task"]["findings_count"] = task["findings_count"]
    elif "task" in snapshot:
        result["task"] = task if _small_scalar(task) else None
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
                "mode", "resume_supported", "claude_session_id",
                "native_resumed", "native_checked", "execution_profile", "transport", "stop_reason",
                "tool_calls_count", "num_turns", "summary_truncated", "events_truncated",
                "total_cost_usd", "total_cost_usd_ticks", "usage_source",
                "usage_is_incomplete", "cost_is_partial", "usage_scope", "cost_reported", "cost_source",
            ))
            short_run["native"].update(_compact_usage(native))
        if "output" in run or "output_available" in run:
            short_run["output_available"] = (run["output_available"] if type(run.get("output_available")) is bool
                                             else run.get("output") is not None)
        result["run"] = short_run
    elif "run" in snapshot:
        result["run"] = run if _small_scalar(run) else None
    session = snapshot.get("session")
    if isinstance(session, dict):
        result["session"] = _fields(session, (
            "conversation_id", "backend", "workspace", "account_ref", "runtime_ref", "created_at",
            "updated_at", "backend_session_id", "latest_turn_id", "task_id", "config_revision",
        ))
    elif "session" in snapshot:
        result["session"] = session if _small_scalar(session) else None
    approval = snapshot.get("approval")
    if isinstance(approval, dict):
        result["approval"] = _fields(approval, (
            "id", "task_id", "run_id", "state", "created_at", "expires_at", "target_hash",
            "decision_source", "response_status", "native_confirmed",
        ))
        _bounded_summary(approval, result["approval"])
    elif "approval" in snapshot:
        result["approval"] = approval if _small_scalar(approval) else None
    notes = snapshot.get("notes")
    if isinstance(notes, dict):
        result["notes"] = _fields(notes, ("run_success_is_not_acceptance", "fake_is_not_real_connection"))
    result["details"] = {"method": "task.get", "compact": False}
    if isinstance(task, dict) and isinstance(result["task"].get("id"), str):
        result["details"]["task_id"] = result["task"]["id"]
    return result


def compact_event_page(page: dict[str, Any]) -> dict[str, Any]:
    result = _fields(page, ("task_id", "run_id", "cursor", "next_cursor"))
    result["compact"] = True
    result["events"] = []
    result["has_more"] = page.get("has_more") is True
    result["event_page_byte_limit"] = COMPACT_EVENT_PAGE_BYTES
    result["events_truncated"] = page.get("events_truncated") is True
    result["next_cursor"] = result.get("cursor", 0)
    for event in page["events"]:
        if not isinstance(event, dict):
            raise AsterunError(INVALID_REQUEST, "观察事件不是有效对象")
        short_event = _fields(event, ("seq", "type", "source", "timestamp", "schema_version"))
        payload = event.get("payload")
        if isinstance(payload, dict):
            short_event["payload"] = _fields(payload, (
                "kind", "status", "toolName", "toolCallId", "tool_kind", "error_code",
                "approval_id", "run_id", "task_id",
            ))
            short_event["payload"].update(_compact_usage(payload))
        if type(short_event.get("seq")) is not int:
            raise AsterunError(INVALID_REQUEST, "观察事件缺少有效序号")
        candidate = {**result, "events": [*result["events"], short_event], "next_cursor": short_event["seq"]}
        if _size(candidate) > COMPACT_EVENT_PAGE_BYTES:
            result["has_more"] = True
            result["events_truncated"] = True
            break
        result["events_truncated"] = result["events_truncated"] or short_event != event
        result["events"].append(short_event)
        result["next_cursor"] = short_event["seq"]
    return result


# 保留旧的内部调用名；服务端与客户端使用同一投影。
_compact_event_page = compact_event_page


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
                page = request("task.events", {"task_id": task_id, "cursor": cursor, "page_size": page_size,
                                               **({"compact": True} if compact else {})})
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
                if len(events) < page_size and not data.get("has_more", False):
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
