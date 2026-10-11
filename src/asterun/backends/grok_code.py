"""Grok 原生 headless 编码运行；只投影公开事件，不重放未知执行。"""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from queue import Empty, Full, Queue
import signal
import subprocess
from threading import Event, Lock, Thread
import time
from typing import Any

RECEIPT_KEY = "asterun.grok_dispatch_v1"
_BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
_PROC_ROOT = Path("/proc")
MAX_LINE_BYTES = 1024 * 1024
MAX_STREAM_BYTES = 32 * 1024 * 1024
MAX_EVENTS = 20000
MAX_TEXT_CHARS = 65536
MAX_FIELD_CHARS = 16384
MAX_TOOL_EVENTS = 256
_USAGE_FIELDS = frozenset({"input_tokens", "output_tokens", "cache_read_input_tokens",
    "cache_creation_input_tokens", "reasoning_tokens", "total_tokens"})
_MODEL_FIELDS = frozenset({"inputTokens", "outputTokens", "cacheReadInputTokens",
    "cacheCreationInputTokens", "reasoningTokens", "modelCalls", "costUSD"})
_INPUT_FIELDS = frozenset({"command", "description", "path", "file_path", "filePath", "pattern",
    "target_file", "target_directory", "query", "directory", "old_string", "new_string", "content", "text", "start_line", "end_line",
    "line_start", "line_end", "replace_all", "recursive", "timeout", "timeout_ms"})
_OUTPUT_FIELDS = frozenset({"output", "output_for_prompt", "stdout", "stderr", "exit_code",
    "exitCode", "command", "description", "current_dir", "truncated", "timed_out", "signal",
    "total_bytes", "path", "file_path", "content", "text", "success", "error"})
_PERMISSION_REFUSAL = re.compile(r"^User cancelled the execution for tool `([^`]{1,128})")


class _StreamError(Exception):
    pass


def classify_failure(value):
    """只返回白名单分类；原始供应商诊断可能带凭据，绝不保存。"""
    if not isinstance(value, str):
        return None
    # Match protocol diagnostics, not an arbitrary occurrence of a number.
    match = re.search(r"(?:HTTP(?:/\d(?:\.\d)?)?\s+|status(?:\s+code)?[\s:=]+)(402|429|401|403)\b", value, re.I)
    if match:
        code = int(match.group(1))
        return {"kind": {402: "quota_exhausted", 429: "rate_limited", 401: "authentication_failed", 403: "authentication_failed"}[code],
                "status_code": code, "source": "native_cli_diagnostic"}
    return None


def _reference(value: Any) -> str | None:
    if isinstance(value, str) and 0 < len(value) <= 256 and not any(ord(c) < 32 for c in value):
        return value
    return None


def _scalar(value: Any) -> Any:
    if isinstance(value, str):
        return value[:MAX_FIELD_CHARS]
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return value
    return None


def _fields(value: Any, allowed: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in allowed & value.keys():
        item = value[key]
        truncated = False
        if key in {"output", "stdout", "stderr"} and isinstance(item, list):
            # Native terminal output is sometimes a JSON byte array.
            if len(item) <= MAX_LINE_BYTES and all(type(part) is int and 0 <= part <= 255 for part in item):
                truncated = len(item) > MAX_FIELD_CHARS
                item = bytes(item[:MAX_FIELD_CHARS]).decode("utf-8", errors="replace")
        projected = _scalar(item)
        if projected is not None:
            result[key] = projected
            if truncated or (isinstance(item, str) and len(item) > MAX_FIELD_CHARS):
                result["projection_truncated"] = True
    return result


def _numbers(value: Any, fields: frozenset[str]) -> dict[str, int | float]:
    if not isinstance(value, dict):
        return {}
    return {key: number for key, number in value.items() if key in fields
            and type(number) in {int, float} and math.isfinite(number) and number >= 0}


def _spend(event: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    usage = _numbers(event.get("usage"), _USAGE_FIELDS)
    if usage:
        result["usage"] = usage
    for name in ("cost_is_partial", "usage_is_incomplete"):
        if event.get(name) is True:
            result[name] = True
    incomplete = result.get("cost_is_partial") or result.get("usage_is_incomplete")
    rows = event.get("modelUsage")
    if isinstance(rows, dict):
        model_usage = {}
        for model, value in list(rows.items())[:32]:
            if _reference(model):
                row = _numbers(value, _MODEL_FIELDS)
                if incomplete:
                    row.pop("costUSD", None)
                if row:
                    model_usage[model] = row
        if model_usage:
            result["modelUsage"] = model_usage
    for name in ("num_turns", "total_cost_usd", "total_cost_usd_ticks"):
        value = event.get(name)
        if type(value) in {int, float} and math.isfinite(value) and value >= 0:
            if name == "num_turns" or not incomplete:
                result[name] = value
    if result:
        result["usage_source"] = "native_headless_report"
    return result


def _tool(event: dict[str, Any]) -> dict[str, Any]:
    projected: dict[str, Any] = {"type": "message", "kind": event["type"]}
    for name in ("toolCallId", "toolName", "status", "title"):
        if isinstance(event.get(name), str):
            projected[name] = event[name][:256]
    if isinstance(event.get("kind"), str):
        projected["tool_kind"] = event["kind"][:256]
    inputs = _fields(event.get("rawInput", event.get("input")), _INPUT_FIELDS)
    outputs = _fields(event.get("rawOutput", event.get("output")), _OUTPUT_FIELDS)
    if inputs:
        projected["input"] = inputs
    if outputs:
        projected["output"] = outputs
    content = event.get("content")
    if isinstance(content, list):
        public = []
        for block in content[:16]:
            if not isinstance(block, dict):
                continue
            block = block.get("content", block)
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                public.append(block["text"][:MAX_FIELD_CHARS])
        if public:
            projected["text"] = "\n".join(public)[:MAX_FIELD_CHARS]
    return projected


def _stop_process(proc: subprocess.Popen) -> None:
    # The group belongs to this launch, including tools that outlive the CLI.
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        if proc.poll() is None:
            proc.terminate()
    try:
        proc.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        if proc.poll() is None:
            proc.kill()
    try:
        proc.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass


def incomplete_terminal(reason, turn_limit=False, permission_denied=False):
    """A bound native end may prove failure even when the CLI exits with code 1."""
    if reason in {"max_tokens", "max_turn_requests", "max_turns", "max_turns_reached", "refusal"} or (
            turn_limit and reason in {"end_turn", "cancelled"}):
        return {"status": "failed", "terminated": True, "error_code": "GROK_RUN_INCOMPLETE"}
    if reason == "cancelled" and permission_denied:
        return {"status": "failed", "terminated": True, "error_code": "GROK_PERMISSION_DENIED"}
    if reason == "cancelled":
        return {"status": "cancelled", "terminated": True, "error_code": None}
    return None


def reconcile_recorded_terminal(native, *, task_id, run_id):
    """Recover only the legacy nonzero-exit misclassification, from core-held evidence.

    In the legacy reader nonzero_exit was emitted only after both pipes drained
    without malformed JSON; stop_reason was saved only after validating end's
    session binding. Missing/end-after-error/timeout receipts remain unknown.
    """
    if not isinstance(native, dict):
        return None
    receipt = native.get(RECEIPT_KEY)
    if not isinstance(receipt, dict) or any(receipt.get(k) != v for k, v in {
            "task_id": task_id, "run_id": run_id, "stage": "prompt",
            "delivery": "may_have_been_sent", "failure_type": "nonzero_exit"}.items()):
        return None
    if receipt.get("prompt_attempted") is not True or type(receipt.get("process_exit_code")) is not int or receipt["process_exit_code"] != 1:
        return None
    if (native.get("execution_profile") != "workspace-code-v1" or native.get("transport") != "headless"
            or not _reference(native.get("session_id")) or native.get("cleanup_incomplete")):
        return None
    failure = native.get("provider_failure")
    turn_limit = isinstance(failure, dict) and failure.get("kind") == "turn_limit" and failure.get("source") == "native_event"
    result = incomplete_terminal(native.get("stop_reason"), turn_limit)
    if result is None:
        return None
    return {**result, "native": {"recorded_terminal_reconciliation": {
        "source": "persisted_headless_end_and_exit", "task_id": task_id, "run_id": run_id,
        "re_dispatched": False}}}


def host_boot_id() -> str | None:
    """Current boot id, or None when /proc is missing or the value is not a reference."""
    try:
        value = _BOOT_ID_PATH.read_text().strip()
    except (OSError, UnicodeError, ValueError):
        return None
    return _reference(value)


def process_start_ticks(pid: int) -> int | None:
    """proc(5) field 22 (starttime), the 20th field after the last ')' in stat."""
    try:
        text = (_PROC_ROOT / str(pid) / "stat").read_text()
        return int(text.rsplit(")", 1)[1].split()[19])
    except (OSError, UnicodeError, ValueError, IndexError, TypeError):
        return None


def process_provably_gone(record) -> bool:
    """True only when host evidence shows this launch's process group no longer exists."""
    if not isinstance(record, dict):
        return False
    pid = record.get("pid")
    pgid = record.get("pgid")
    if type(pid) is not int or type(pgid) is not int or pid <= 1 or pgid <= 1:
        return False
    boot_id = _reference(record.get("boot_id"))
    if boot_id is None:
        return False
    current = host_boot_id()
    if current is None:
        return False
    if current != boot_id:
        return True
    ticks = process_start_ticks(pid)
    recorded_ticks = record.get("start_ticks")
    leader_gone = ticks is None or (type(recorded_ticks) is int and recorded_ticks != ticks)
    if not leader_gone:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def reconcile_lost_process(native, *, task_id, run_id, cancel_requested=False):
    """End an in-flight prompt when the recorded host process is provably gone.

    A receipt that already stored a failure or an exit code stays on the legacy
    path. Startup must not turn that evidence into a lost-process terminal.
    """
    if not isinstance(native, dict):
        return None
    receipt = native.get(RECEIPT_KEY)
    if not isinstance(receipt, dict) or any(receipt.get(key) != value for key, value in {
            "task_id": task_id, "run_id": run_id, "stage": "prompt"}.items()):
        return None
    if receipt.get("prompt_attempted") is not True or receipt.get("failure_type") is not None or receipt.get("process_exit_code") is not None:
        return None
    if (native.get("execution_profile") != "workspace-code-v1" or native.get("transport") != "headless"
            or not _reference(native.get("session_id")) or not process_provably_gone(native.get("process"))):
        return None
    cancelled = bool(cancel_requested)
    return {
        "status": "cancelled" if cancelled else "failed",
        "error_code": None if cancelled else "GROK_PROCESS_LOST",
        "terminated": True,
        "summary": "Grok 编码进程已不存在（主机重启或进程消失），按已记录进程证据结束；未重派",
        "events": [{"type": "reconciled", "re_dispatched": False, "reason": "host_process_lost"}],
        "native": {"recorded_terminal_reconciliation": {
            "source": "host_process_lost", "task_id": task_id, "run_id": run_id, "re_dispatched": False}},
    }


class CodeProcessHandle:
    """Own one launch so host shutdown cannot miss the spawn/register boundary."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._proc: subprocess.Popen | None = None
        self._closed = False
        self._cleanup_done = False

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def spawn(self, command, **kwargs) -> subprocess.Popen:
        with self._lock:
            if self._closed:
                raise _StreamError("stopped_before_spawn")
            if self._proc is not None:
                raise _StreamError("duplicate_spawn")
            self._proc = subprocess.Popen(command, **kwargs)
            return self._proc

    def close(self) -> None:
        # Keep the lock through TERM/KILL/reap. Concurrent backend and worker
        # cleanup must wait for the actual cleanup, not merely a closed flag.
        with self._lock:
            self._closed = True
            if self._cleanup_done:
                return
            if self._proc is not None:
                _stop_process(self._proc)
            self._cleanup_done = True


def run_code(*, command: list[str], env: dict, cwd: Path, session_id: str,
             task_id: str, run_id: str, timeout_seconds: int, publish, stopping: Event,
             process_handle: CodeProcessHandle | None = None, resumed: bool = False) -> dict[str, Any]:
    """Run one native invocation. An observer deadline never causes a new invocation."""
    proc = None
    process = process_handle if process_handle is not None else CodeProcessHandle()
    threads: list[Thread] = []
    reads_stopped = Event()
    queue: Queue = Queue(maxsize=64)
    stage, attempted, exit_code = "spawn", False, None
    error_kind = None
    text = ""
    text_truncated = False
    tool_count = 0
    captured: list[dict[str, Any]] = []
    dropped_events = 0
    end: dict[str, Any] | None = None
    metadata: dict[str, Any] = {}
    stream_error = False
    budget_reached = False
    permission_refusal = None
    tool_names: dict[str, str] = {}
    event_count = 0
    stdout_bytes = stderr_bytes = 0
    stderr_tail = ""
    protocol_failure = None

    def native():
        return {"session_id": session_id, "native_resumed": bool(resumed and end),
            "resume_requested": resumed,
            "execution_profile": "workspace-code-v1", "transport": "headless",
            "tool_calls_count": tool_count, "summary_truncated": text_truncated,
            "events_truncated": bool(dropped_events), **metadata,
            RECEIPT_KEY: {"task_id": task_id, "run_id": run_id, "stage": stage,
                "prompt_attempted": attempted,
                "delivery": "may_have_been_sent" if attempted else "not_sent",
                "failure_type": error_kind, "process_exit_code": exit_code}}

    def emit(events=(), *, status="running"):
        nonlocal dropped_events
        if publish is not None:
            publish({"status": status, "terminated": False, "native": native(),
                     "summary": text, "events": list(events)})
        else:
            for event in events:
                if len(captured) < MAX_TOOL_EVENTS:
                    captured.append(event)
                else:
                    dropped_events += 1

    def put(item):
        while not reads_stopped.is_set():
            try:
                queue.put(item, timeout=0.05)
                return
            except Full:
                continue

    def read_stream(stream, name):
        try:
            while not reads_stopped.is_set():
                raw = stream.readline(MAX_LINE_BYTES + 1) if name == "stdout" else stream.read(4096)
                if not raw:
                    break
                if len(raw) > MAX_LINE_BYTES:
                    put(("failure", "oversized_line"))
                    return
                # Raw stderr stays only in a bounded transient classification buffer.
                put((name, raw))
        except (OSError, ValueError):
            if not reads_stopped.is_set():
                put(("failure", "stream_read_failed"))
        finally:
            try:
                stream.close()
            except Exception:
                pass
            put(("eof", name))

    try:
        if stopping.is_set():
            raise _StreamError("stopped_before_spawn")
        # This acknowledgement precedes Popen: the predetermined native reference
        # survives even when the first provider request has no stdout response.
        emit(status="dispatching")
        if stopping.is_set():
            raise _StreamError("stopped_before_spawn")
        proc = process.spawn(command, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        attempted, stage = True, "prompt"
        # start_new_session makes this process the group leader, so pgid == pid.
        metadata["process"] = {"pid": proc.pid, "pgid": proc.pid,
            "boot_id": host_boot_id(), "start_ticks": process_start_ticks(proc.pid)}
        deadline = time.monotonic() + timeout_seconds
        for stream, name in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
            thread = Thread(target=read_stream, args=(stream, name), daemon=True)
            threads.append(thread)
            thread.start()
        emit([{"type": "run_started", "text": "grok headless coding"}])
        closed = set()
        exited_at = None
        made_progress = False
        while True:
            # Host acknowledgements can take longer than draining the native
            # process itself. Bound idle pipe retention, not the total time
            # needed to consume and persist its queued tail after exit.
            if made_progress and exited_at is not None:
                exited_at = time.monotonic()
            made_progress = False
            if process.closed:
                raise _StreamError("backend_closed")
            if stopping.is_set():
                raise _StreamError("observation_stopped")
            if time.monotonic() >= deadline:
                raise _StreamError("runtime_timeout")
            exit_code = proc.poll()
            if exit_code is not None:
                exited_at = time.monotonic() if exited_at is None else exited_at
                if closed == {"stdout", "stderr"} and queue.empty():
                    break
                if time.monotonic() - exited_at > 1:
                    raise _StreamError("stream_not_closed")
            try:
                name, value = queue.get(timeout=0.03)
            except Empty:
                continue
            made_progress = True
            if name == "eof":
                closed.add(value)
                continue
            if name == "failure":
                raise _StreamError(value)
            if name == "stderr":
                stderr_bytes += len(value)
                stderr_tail = (stderr_tail + value.decode("utf-8", errors="replace"))[-8192:]
                failure = classify_failure(stderr_tail)
                if failure:
                    metadata["provider_failure"] = failure
                if stderr_bytes > MAX_STREAM_BYTES:
                    raise _StreamError("stderr_limit")
                continue
            stdout_bytes += len(value)
            if stdout_bytes > MAX_STREAM_BYTES:
                raise _StreamError("stdout_limit")
            if not value.strip():
                continue
            try:
                event = json.loads(value)
            except (ValueError, UnicodeError, RecursionError):
                failure = classify_failure(value[:8192].decode("utf-8", errors="replace"))
                if failure:
                    metadata["provider_failure"] = failure
                # CLI failures can have a multi-line non-JSON tail. Drain it
                # under the existing byte/time limits so later HTTP diagnostics
                # survive, but never turn a malformed stream into success.
                protocol_failure = "invalid_stdout_json"
                continue
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise _StreamError("invalid_stdout_event")
            kind = event["type"]
            if kind == "thought":
                continue
            event_count += 1
            if event_count > MAX_EVENTS:
                raise _StreamError("event_limit")
            for key in ("sessionId", "session_id"):
                if key in event and event[key] != session_id:
                    raise _StreamError("session_mismatch")
            if end is not None:
                raise _StreamError("event_after_end")
            if kind == "text":
                chunk = event.get("data")
                if not isinstance(chunk, str):
                    raise _StreamError("invalid_text_event")
                remaining = MAX_TEXT_CHARS - len(text)
                text += chunk[:remaining]
                text_truncated = text_truncated or len(chunk) > remaining
                emit([{"type": "message", "kind": "text", "text": chunk[:MAX_FIELD_CHARS]}])
            elif kind in {"tool_call", "tool_call_update", "tool_result"}:
                if kind == "tool_call":
                    tool_count += 1
                    call_id, call_name = event.get("toolCallId"), event.get("toolName")
                    if isinstance(call_id, str) and isinstance(call_name, str) and call_id:
                        tool_names[call_id[:256]] = call_name[:128]
                projected = _tool(event)
                if kind in {"tool_call_update", "tool_result"} and projected.get("status") == "failed":
                    refusal_text = projected.get("text")
                    matched = _PERMISSION_REFUSAL.match(refusal_text) if isinstance(refusal_text, str) else None
                    if matched:
                        mapped = tool_names.get(projected.get("toolCallId"))
                        tool = (matched.group(1) or (mapped if isinstance(mapped, str) else ""))[:128]
                        permission_refusal = {"kind": "permission_denied", "source": "native_event", "tool": tool}
                emit([projected])
            elif kind == "usage":
                # Prompt totals belong to end, not a sum of potentially repeated
                # model-boundary updates. Signatures and unknown fields stay out.
                usage = _numbers(event.get("usage"), _USAGE_FIELDS)
                message_id = _reference(event.get("messageId"))
                emit([{"type": "message", "kind": "usage", "usage": usage,
                       **({"message_id": message_id} if message_id else {})}])
            elif kind == "end":
                if event.get("sessionId") != session_id or not _reference(event.get("stopReason")):
                    raise _StreamError("invalid_end_binding")
                end = {"stopReason": event["stopReason"]}
                metadata.update(_spend(event))
                request_id = _reference(event.get("requestId"))
                if request_id:
                    metadata["request_id"] = request_id
                metadata["stop_reason"] = event["stopReason"]
            elif kind == "error":
                stream_error = True
                metadata.update(_spend(event))
                failure = classify_failure(str(event.get("message", "")))
                status_code = event.get("status_code", event.get("status"))
                if type(status_code) is int:
                    failure = classify_failure(f"HTTP {status_code}") or failure
                if failure:
                    metadata["provider_failure"] = failure
                emit([{"type": "message", "kind": "error", "text": "Grok 报告原生运行错误"}])
            elif kind == "max_turns_reached":
                budget_reached = True
                metadata["provider_failure"] = {"kind": "turn_limit", "source": "native_event"}
                emit([{"type": "message", "kind": kind, "text": "Grok 达到原生回合上限"}])
            # Plans, command catalogs and future event payloads are not persisted.
        if protocol_failure:
            raise _StreamError(protocol_failure)
        known_incomplete = incomplete_terminal(end["stopReason"], budget_reached, permission_refusal is not None) if end else None
        if exit_code != 0 and not (exit_code == 1 and known_incomplete):
            raise _StreamError("nonzero_exit")
        if end is None:
            raise _StreamError("missing_end")
        reason = end["stopReason"]
        if reason in {"max_tokens", "max_turn_requests", "max_turns", "max_turns_reached", "refusal"} or budget_reached:
            status, code = "failed", "GROK_RUN_INCOMPLETE"
            metadata.setdefault("provider_failure", {"kind": "turn_limit" if reason != "refusal" else "refusal", "source": "native_end"})
        elif stream_error:
            raise _StreamError("native_error")
        elif reason == "end_turn":
            status, code = "succeeded", None
        elif reason == "cancelled" and permission_refusal is not None and not budget_reached:
            status, code = "failed", "GROK_PERMISSION_DENIED"
            metadata.setdefault("provider_failure", permission_refusal)
        elif reason == "cancelled":
            status, code = "cancelled", None
        else:
            raise _StreamError("unknown_stop_reason")
        stage = "complete"
    except Exception as exc:
        # Do not disclose subprocess diagnostics or callback exception messages.
        error_kind = str(exc) if isinstance(exc, _StreamError) else type(exc).__name__
        if error_kind == "runtime_timeout":
            metadata.setdefault("provider_failure", {"kind": "timeout", "source": "local_runtime"})
        elif error_kind in {"missing_end", "invalid_stdout_json", "stream_not_closed", "stream_read_failed"}:
            metadata.setdefault("provider_failure", {"kind": "protocol_incomplete", "source": "local_runtime"})
        status, code = ("pending_reconcile", "REMOTE_STATE_UNKNOWN") if attempted else ("failed", "BACKEND_UNAVAILABLE")
    finally:
        reads_stopped.set()
        if proc is not None:
            try:
                exit_code = proc.poll()
                process.close()
            except Exception:
                # Cleanup cannot replace a verified end or change whether the
                # native process could already have sent a provider request.
                metadata["cleanup_incomplete"] = True
            for thread in threads:
                try:
                    thread.join(timeout=0.5)
                except RuntimeError:
                    metadata["cleanup_incomplete"] = True
            for index, stream in enumerate((proc.stdout, proc.stderr)):
                # Closing a BufferedReader while read() holds its lock can wait
                # forever. A live daemon reader owns cleanup after failed kill.
                if index < len(threads) and threads[index].is_alive():
                    metadata["cleanup_incomplete"] = True
                    continue
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        metadata["cleanup_incomplete"] = True
    summary = text or ("Grok 编码运行结果未确认，需要对账" if status == "pending_reconcile" else
                       "Grok 编码运行未完成" if status == "failed" else "")
    return {"status": status, "terminated": status != "pending_reconcile", "error_code": code,
            "summary": summary, "events": captured, "native": native()}
