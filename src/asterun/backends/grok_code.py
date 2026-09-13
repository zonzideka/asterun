"""Grok 原生 headless 编码运行；只投影公开事件，不重放未知执行。"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from queue import Empty, Full, Queue
import signal
import subprocess
from threading import Event, Lock, Thread
import time
from typing import Any

RECEIPT_KEY = "asterun.grok_dispatch_v1"
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


class _StreamError(Exception):
    pass


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
             process_handle: CodeProcessHandle | None = None) -> dict[str, Any]:
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
    event_count = 0
    stdout_bytes = stderr_bytes = 0

    def native():
        return {"session_id": session_id, "native_resumed": False,
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
                # stderr may contain credentials or opaque diagnostics. Count only.
                put((name, raw if name == "stdout" else len(raw)))
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
                stderr_bytes += value
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
                raise _StreamError("invalid_stdout_json") from None
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
                emit([_tool(event)])
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
                metadata = _spend(event)
                request_id = _reference(event.get("requestId"))
                if request_id:
                    metadata["request_id"] = request_id
                metadata["stop_reason"] = event["stopReason"]
            elif kind == "error":
                stream_error = True
                metadata.update(_spend(event))
                emit([{"type": "message", "kind": "error", "text": "Grok 报告原生运行错误"}])
            elif kind == "max_turns_reached":
                budget_reached = True
                emit([{"type": "message", "kind": kind, "text": "Grok 达到原生回合上限"}])
            # Plans, command catalogs and future event payloads are not persisted.
        if exit_code != 0:
            raise _StreamError("nonzero_exit")
        if end is None:
            raise _StreamError("missing_end")
        reason = end["stopReason"]
        if reason in {"max_tokens", "max_turn_requests", "refusal"} or budget_reached:
            status, code = "failed", "GROK_RUN_INCOMPLETE"
        elif stream_error:
            raise _StreamError("native_error")
        elif reason == "end_turn":
            status, code = "succeeded", None
        elif reason == "cancelled":
            status, code = "cancelled", None
        else:
            raise _StreamError("unknown_stop_reason")
        stage = "complete"
    except Exception as exc:
        # Do not disclose subprocess diagnostics or callback exception messages.
        error_kind = str(exc) if isinstance(exc, _StreamError) else type(exc).__name__
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
