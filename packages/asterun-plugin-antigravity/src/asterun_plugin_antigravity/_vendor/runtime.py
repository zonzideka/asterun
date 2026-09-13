"""Antigravity CLI 双向 NDJSON 运行器；没有原生终态就保留未知结果。"""

from __future__ import annotations

import json
import math
import os
import re
import selectors
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .compat import BACKEND_UNAVAILABLE, REMOTE_STATE_UNKNOWN, RunId

DISPATCH_RECEIPT_KEY = "asterun.antigravity_dispatch_v1"
MAX_FRAME_BYTES = 256 * 1024
MAX_STDOUT_BYTES = 4 * 1024 * 1024
MAX_STDERR_BYTES = 256 * 1024
MAX_EVENTS = 8192
MAX_SUMMARY_BYTES = 32 * 1024
CANCEL_GRACE = 0.5
TERMINATE_GRACE = 0.3
PROGRESS_INTERVAL = 1.0
USAGE_KEYS = {"input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens"}
# These are diagnostics, never permission grants. Keep a rolling suffix so a notice
# split across reads cannot turn a denied tool into a successful execution.
_DENIED = re.compile(
    r"soft[- ]denied|permission (?:was )?denied|(?:tool|action|command)[^\n]{0,100}"
    r"(?:requires? approval|requires? permission|not allowed|denied)|"
    r"(?:cannot|can't|unable to)[^\n]{0,80}(?:ask|obtain)[^\n]{0,80}(?:approval|permission)",
    re.IGNORECASE,
)


class _ProtocolError(Exception):
    pass


def _bounded_text(value: str) -> str:
    raw = value.encode("utf-8")
    return raw[:MAX_SUMMARY_BYTES].decode("utf-8", errors="ignore")


def _number(value: Any, *, integer: bool = False) -> bool:
    return not isinstance(value, bool) and isinstance(value, int if integer else (int, float)) and math.isfinite(value) and value >= 0


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _ProtocolError("duplicate_json_key")
        result[key] = value
    return result


@dataclass
class _Run:
    run_id: str
    expected_cwd: Path | None = None
    expected_model: str | None = None
    cancelled: threading.Event = field(default_factory=threading.Event)
    proc: subprocess.Popen | None = None
    prompt_attempted: bool = False
    stage: str = "preflight"
    failure_type: str | None = None
    session_id: str = ""
    init_seen: bool = False
    init_evidence: dict[str, str] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    tool_errors: set[int] = field(default_factory=set)
    step_errors: set[int] = field(default_factory=set)
    active_tools: set[int] = field(default_factory=set)
    step_states: dict[int, str] = field(default_factory=dict)
    step_types: dict[int, str] = field(default_factory=dict)
    soft_denied: bool = False
    text: str = ""
    text_truncated: bool = False
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    events_seen: int = 0

    def native(self) -> dict[str, Any]:
        native: dict[str, Any] = {
            DISPATCH_RECEIPT_KEY: {
                "run_id": self.run_id, "stage": self.stage,
                "prompt_attempted": self.prompt_attempted,
                "delivery": "may_have_been_sent" if self.prompt_attempted else "not_sent",
                "failure_type": self.failure_type,
                "process_started": self.proc is not None,
                "process_exit_code": None if self.proc is None else self.proc.poll(),
                "stdout_bytes": self.stdout_bytes, "stderr_bytes": self.stderr_bytes,
                "events_seen": self.events_seen,
            },
            "tool_error_count": len(self.tool_errors), "step_error_count": len(self.step_errors),
            "soft_denied": self.soft_denied,
            "summary_truncated": self.text_truncated,
        }
        if self.session_id:
            native["backend_session_id"] = self.session_id
        if self.init_evidence:
            native["init_evidence"] = dict(self.init_evidence)
        if self.result is not None:
            native["result_status"] = self.result["status"]
            for name in ("num_turns", "duration_seconds", "usage"):
                if name in self.result:
                    native[name] = self.result[name]
            if "usage" in self.result:
                native["usage_scope"] = "session"
        return native


class AntigravityRuntime:
    def __init__(self, timeout: float = 300.0) -> None:
        if not _number(timeout) or timeout <= 0:
            raise ValueError("timeout 必须是有限正数")
        self.timeout = timeout
        self._lock = threading.Lock()
        self._runs: dict[str, _Run] = {}
        self._cancelled: set[str] = set()
        self._closed = threading.Event()

    def cancel(self, run_id: RunId) -> dict[str, Any]:
        with self._lock:
            self._cancelled.add(run_id.value)
            state = self._runs.get(run_id.value)
            if state is not None:
                state.cancelled.set()
        return {"cancel_requested": True, "terminated": False}

    def close(self) -> None:
        self._closed.set()

    @staticmethod
    def _signal(proc: subprocess.Popen, sig: signal.Signals) -> None:
        try:
            proc.send_signal(sig)
        except ProcessLookupError:
            pass

    @classmethod
    def _cleanup(cls, proc: subprocess.Popen) -> None:
        # The host owns the worker group; final host cleanup covers remaining descendants.
        try:
            try:
                cls._signal(proc, signal.SIGTERM)
            except OSError:
                pass
            try:
                proc.wait(timeout=TERMINATE_GRACE)
            except subprocess.TimeoutExpired:
                pass
            try:
                cls._signal(proc, signal.SIGKILL)
            except OSError:
                pass
            try:
                proc.wait(timeout=TERMINATE_GRACE)
            except subprocess.TimeoutExpired:
                pass
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()

    @staticmethod
    def _session(state: _Run, value: Any, *, allow_empty: bool = False) -> None:
        if value == "" and allow_empty and not state.session_id:
            return
        if not isinstance(value, str) or not value.strip() or len(value) > 256 or any(ord(c) < 32 for c in value):
            raise _ProtocolError("invalid_conversation_id")
        if state.session_id and state.session_id != value:
            raise _ProtocolError("conversation_id_mismatch")
        state.session_id = value

    @classmethod
    def _event(cls, state: _Run, line: bytes) -> bool:
        state.events_seen += 1
        if state.events_seen > MAX_EVENTS:
            raise _ProtocolError("event_limit")
        try:
            item = json.loads(line.decode("utf-8"), object_pairs_hook=_unique_object,
                              parse_constant=lambda _: (_ for _ in ()).throw(_ProtocolError("invalid_number")))
        except (UnicodeDecodeError, ValueError, RecursionError) as error:
            raise _ProtocolError("malformed_json") from error
        if not isinstance(item, dict) or state.result is not None:
            raise _ProtocolError("event_after_result" if state.result is not None else "invalid_event")
        event = item.get("event")
        if event == "init":
            if state.init_seen or state.events_seen != 1 or not isinstance(item.get("init"), dict):
                raise _ProtocolError("invalid_init")
            cls._session(state, item.get("conversation_id"))
            init = item["init"]
            cwd = init.get("cwd")
            if (not isinstance(cwd, str) or not Path(cwd).is_absolute()
                    or Path(cwd).resolve() != state.expected_cwd):
                raise _ProtocolError("workspace_mismatch")
            # Match the profile's effective mode exactly; strict and bypass
            # modes are different policies, never interchangeable fallbacks.
            if init.get("permission_mode") != "request-review":
                raise _ProtocolError("unsafe_permission_mode")
            if state.expected_model is not None and init.get("model") != state.expected_model:
                raise _ProtocolError("model_mismatch")
            state.init_evidence = {"cwd": str(Path(cwd).resolve()), "permission_mode": "request-review"}
            if isinstance(init.get("model"), str) and len(init["model"]) <= 256:
                state.init_evidence["model"] = init["model"]
            state.init_seen = True
            return True
        if event == "step_update":
            step = item.get("step_update")
            if not state.init_seen or not isinstance(step, dict):
                raise _ProtocolError("invalid_step")
            cls._session(state, step.get("conversation_id"))
            index, status, kind = step.get("step_index"), step.get("state"), step.get("step_type")
            if not _number(index, integer=True) or status not in {"ACTIVE", "DONE", "ERROR"} or not isinstance(kind, str):
                raise _ProtocolError("invalid_step")
            if state.step_states.get(index) == "DONE":
                raise _ProtocolError("step_after_done")
            if state.step_states.get(index) == "ERROR":
                raise _ProtocolError("step_after_error")
            if index in state.step_types and state.step_types[index] != kind:
                raise _ProtocolError("step_type_changed")
            state.step_states[index] = status
            state.step_types[index] = kind
            if status == "ERROR":
                state.step_errors.add(index)
            if kind == "tool":
                if status == "ACTIVE":
                    state.active_tools.add(index)
                else:
                    state.active_tools.discard(index)
                if status == "ERROR":
                    state.tool_errors.add(index)
                info = step.get("tool_info")
                if info is not None and not isinstance(info, dict):
                    raise _ProtocolError("invalid_tool_info")
                if isinstance(info, dict) and info.get("error") is not None:
                    if not isinstance(info["error"], dict):
                        raise _ProtocolError("invalid_tool_error")
                    state.tool_errors.add(index)
            if kind == "agent_response":
                delta = step.get("text_delta", "")
                if not isinstance(delta, str):
                    raise _ProtocolError("invalid_text_delta")
                joined = state.text + delta
                state.text = _bounded_text(joined)
                state.text_truncated |= state.text != joined
            return False
        if event != "result" or not isinstance(item.get("result"), dict):
            raise _ProtocolError("unknown_event")
        result = item["result"]
        status = result.get("status")
        if not isinstance(status, str) or not isinstance(result.get("response"), str):
            raise _ProtocolError("invalid_result")
        cls._session(state, result.get("conversation_id"), allow_empty=status in {"ERROR", "INVALID"})
        for name in ("num_turns", "duration_seconds"):
            if name in result and not _number(result[name], integer=name == "num_turns"):
                raise _ProtocolError("invalid_result_counter")
        usage = result.get("usage")
        if usage is not None and (not isinstance(usage, dict) or any(not _number(v, integer=True) for v in usage.values())):
            raise _ProtocolError("invalid_usage")
        # Copy only understood aggregate counters. Never merge arbitrary native data.
        state.result = {"status": status, "response": result["response"]}
        for name in ("num_turns", "duration_seconds"):
            if name in result:
                state.result[name] = result[name]
        if usage is not None:
            state.result["usage"] = {k: v for k, v in usage.items() if k in USAGE_KEYS}
        if result.get("error"):
            if not isinstance(result["error"], str):
                raise _ProtocolError("invalid_result_error")
            state.result["has_error"] = True
        state.text = _bounded_text(result["response"])
        state.text_truncated |= state.text != result["response"]
        return True

    @staticmethod
    def _snapshot(state: _Run, *, status: str = "running", summary: str | None = None,
                  terminated: bool = False, error_code: str | None = None) -> dict[str, Any]:
        result = {"status": status, "terminated": terminated,
                  "summary": state.text if summary is None else summary, "native": state.native()}
        if error_code:
            result["error_code"] = error_code
        return result

    def run(self, run_id: RunId, text: str, *, command: list[str], env: dict[str, str],
            cwd: Path, publish: Callable, stopping: threading.Event,
            expected_model: str | None = None) -> dict[str, Any]:
        state = _Run(run_id.value, expected_cwd=cwd.resolve(), expected_model=expected_model)
        with self._lock:
            if run_id.value in self._runs:
                raise RuntimeError("同一运行不可重复派发")
            self._runs[run_id.value] = state
            if run_id.value in self._cancelled:
                state.cancelled.set()
        try:
            if stopping.is_set() or self._closed.is_set():
                state.failure_type = "core_stopping"
                return self._unknown(state)
            if state.cancelled.is_set():
                state.failure_type = "cancelled_before_spawn"
                return self._snapshot(state, status="cancelled", terminated=True, summary="提交前已取消，未启动原生进程")
            state.stage = "spawn"
            try:
                state.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                              stderr=subprocess.PIPE, cwd=cwd, env=env,
                                              start_new_session=False, bufsize=0)
            except (OSError, ValueError, TypeError) as error:
                state.failure_type = type(error).__name__
                return self._snapshot(state, status="failed", terminated=True,
                                      error_code=BACKEND_UNAVAILABLE, summary="Antigravity 进程启动失败，未提交输入")
            self._exchange(state, text, publish, stopping)
            if state.result is None:
                state.failure_type = "missing_result"
                return self._unknown(state)
            status = state.result["status"]
            if status in {"ERROR", "INVALID"}:
                return self._snapshot(state, status="failed", terminated=True,
                                      error_code="ANTIGRAVITY_RUN_FAILED", summary=state.text or "Antigravity 返回原生失败终态")
            if status in {"CANCELED", "INTERRUPTED"}:
                return self._snapshot(state, status="cancelled", terminated=True, summary=state.text or "Antigravity 已确认中断")
            if status == "SUCCESS" and not state.init_seen:
                state.failure_type = "missing_init"
                return self._unknown(state)
            if (status == "SUCCESS" and state.session_id and state.proc.returncode == 0
                    and not state.active_tools and all(value in {"DONE", "ERROR"} for value in state.step_states.values())):
                if state.tool_errors or state.step_errors or state.soft_denied or state.result.get("has_error"):
                    return self._snapshot(state, status="failed", terminated=True,
                                          error_code="ANTIGRAVITY_RUN_INCOMPLETE", summary=state.text or "Antigravity 步骤执行失败或权限被拒绝")
                return self._snapshot(state, status="succeeded", terminated=True)
            state.failure_type = "contradictory_result" if status == "SUCCESS" else "nonterminal_result"
            return self._unknown(state)
        except _ProtocolError as error:
            state.failure_type = str(error)
            return self._unknown(state)
        except Exception as error:
            state.failure_type = type(error).__name__
            return self._unknown(state)
        finally:
            if state.proc is not None:
                self._cleanup(state.proc)
            with self._lock:
                self._runs.pop(run_id.value, None)
                self._cancelled.discard(run_id.value)

    @classmethod
    def _unknown(cls, state: _Run) -> dict[str, Any]:
        return cls._snapshot(state, status="pending_reconcile", error_code=REMOTE_STATE_UNKNOWN,
                             summary=state.text or "Antigravity 结果未确认，保留原生引用且不自动重发")

    def _exchange(self, state: _Run, text: str, publish: Callable, stopping: threading.Event) -> None:
        proc = state.proc
        assert proc is not None and proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        prompt = (json.dumps({"event": "user", "message": {"content": text}}, ensure_ascii=False) + "\n").encode("utf-8")
        deadline = time.monotonic() + self.timeout
        offset, stdout_buffer, stderr_tail = 0, bytearray(), b""
        cancel_at: float | None = None
        last_publish = time.monotonic()
        last_text = ""
        with selectors.DefaultSelector() as selector:
            for stream, name, events in ((proc.stdin, "stdin", selectors.EVENT_WRITE),
                                         (proc.stdout, "stdout", selectors.EVENT_READ),
                                         (proc.stderr, "stderr", selectors.EVENT_READ)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, events, name)
            while selector.get_map() or proc.poll() is None:
                now = time.monotonic()
                if stopping.is_set() or self._closed.is_set():
                    raise _ProtocolError("core_stopping")
                if now >= deadline:
                    raise _ProtocolError("timeout")
                if state.cancelled.is_set() and cancel_at is None:
                    if not state.prompt_attempted:
                        # No prompt was attempted, but no native cancellation acknowledgement:
                        # returning unknown is conservative for a process already started.
                        raise _ProtocolError("cancel_before_input")
                    self._signal(proc, signal.SIGINT)
                    cancel_at = now
                if cancel_at is not None and now - cancel_at >= CANCEL_GRACE and proc.poll() is None:
                    raise _ProtocolError("cancel_unconfirmed")
                for key, _ in selector.select(min(0.05, max(0, deadline - now))):
                    stream, channel = key.fileobj, key.data
                    if channel == "stdin":
                        if not state.prompt_attempted:
                            state.stage = "prompt"
                            state.prompt_attempted = True
                            publish(self._snapshot(state, status="dispatching"))
                            if stopping.is_set() or self._closed.is_set() or state.cancelled.is_set():
                                raise _ProtocolError("stopped_before_write")
                        try:
                            offset += os.write(stream.fileno(), prompt[offset:offset + 16384])
                        except BlockingIOError:
                            continue
                        if offset == len(prompt):
                            selector.unregister(stream)
                            stream.close()
                            state.stage = "observe"
                        continue
                    try:
                        data = os.read(stream.fileno(), 16384)
                    except BlockingIOError:
                        continue
                    if not data:
                        selector.unregister(stream)
                        stream.close()
                        if channel == "stdout" and stdout_buffer:
                            raise _ProtocolError("unterminated_frame")
                        continue
                    if channel == "stderr":
                        state.stderr_bytes += len(data)
                        if state.stderr_bytes > MAX_STDERR_BYTES:
                            raise _ProtocolError("stderr_limit")
                        diagnostic = stderr_tail + data
                        state.soft_denied |= bool(_DENIED.search(diagnostic.decode("utf-8", errors="replace")))
                        stderr_tail = diagnostic[-512:]
                        continue
                    state.stdout_bytes += len(data)
                    if state.stdout_bytes > MAX_STDOUT_BYTES:
                        raise _ProtocolError("stdout_limit")
                    stdout_buffer.extend(data)
                    while b"\n" in stdout_buffer:
                        line, _, remainder = stdout_buffer.partition(b"\n")
                        stdout_buffer = bytearray(remainder)
                        if not line or len(line) > MAX_FRAME_BYTES:
                            raise _ProtocolError("frame_limit" if line else "empty_frame")
                        immediate = self._event(state, line)
                        if state.result is None and (immediate or (state.text != last_text and time.monotonic() - last_publish >= PROGRESS_INTERVAL)):
                            publish(self._snapshot(state))
                            last_publish, last_text = time.monotonic(), state.text
                    if len(stdout_buffer) > MAX_FRAME_BYTES:
                        raise _ProtocolError("frame_limit")
            proc.wait(timeout=max(0.01, deadline - time.monotonic()))
            state.stage = "result" if state.result is not None else "eof"
