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

from asterun.errors import BACKEND_UNAVAILABLE, REMOTE_STATE_UNKNOWN
from asterun.ids import RunId

DISPATCH_RECEIPT_KEY = "asterun.antigravity_dispatch_v1"
ANTIGRAVITY_RUN_INCOMPLETE = "ANTIGRAVITY_RUN_INCOMPLETE"
ANTIGRAVITY_PERMISSION_REVIEW_REQUIRED = "ANTIGRAVITY_PERMISSION_REVIEW_REQUIRED"
MAX_FRAME_BYTES = 256 * 1024
MAX_STDOUT_BYTES = 4 * 1024 * 1024
MAX_STDERR_BYTES = 256 * 1024
MAX_EVENTS = 8192
MAX_SUMMARY_BYTES = 32 * 1024
CANCEL_GRACE = 0.5
TERMINATE_GRACE = 0.3
# True when agy runs in its own session/process group that this runtime may
# signal and inspect. The plugin copy sets this to False (the host owns the
# worker group), so it never claims a delegated run's processes are gone.
_OWNS_PROCESS_GROUP = True
PROGRESS_INTERVAL = 1.0
USAGE_KEYS = {"input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens"}
# These are diagnostics, never permission grants. Keep a rolling suffix so a notice
# split across reads cannot turn a denied tool into a successful execution.
# Print mode logs "soft-denying", not only "soft-denied", then closes the turn.
_DENIED = re.compile(
    r"soft[- ]denied|soft[- ]denying|permission (?:was )?denied|(?:tool|action|command)[^\n]{0,100}"
    r"(?:requires? approval|requires? permission|not allowed|denied)|"
    r"(?:cannot|can't|unable to)[^\n]{0,80}(?:ask|obtain)[^\n]{0,80}(?:approval|permission)",
    re.IGNORECASE,
)
# 普通 OS “permission denied” 只算宽泛信号；复核码只认软拒绝或审批措辞。
_REVIEW_EVIDENCE = re.compile(r"soft[- ]den(?:ied|ying)|requires? approval", re.IGNORECASE)
# Profile deny rules are a hard refusal only for agy's whole native sentence.
# A substring, an extra field, or a non-string value is an ordinary tool error.
# Never copy the tool error text into a public result.
_POLICY_DENIED = re.compile(
    r'permission check failed for (unsandboxed|command|read_file|write_file|read_url|execute_url|mcp) "(?:[^"\\\r\n]|\\[^\r\n]){0,4096}": Permission denied for \1\([^\r\n]{0,4096}\)\. Matches user-configured deny rule\.'
)
_POLICY_DENIED_KEYS = frozenset({"type", "message"})
_KNOWN_TOOLS = frozenset({
    "run_command", "read_file", "write_file", "view_file", "list_dir", "grep_search",
    "find_by_name", "codebase_search", "read_url_content", "view_content_chunk",
    "search_web", "read_url", "execute_url", "mcp", "replace_file_content",
    "multi_replace_file_content", "write_to_file", "ReadFile", "WriteFile",
    "RunCommand", "ReadUrl", "ExecuteUrl", "Mcp", "ViewFile", "ListDir",
    "GrepSearch", "FindByName",
})
_SOFT_DENIED_TOOL = re.compile(
    r'soft-denying tool confirmation "([A-Za-z_][A-Za-z0-9_]{0,63})"'
)
# agy 真实额度错误格式出现后在此补充
AGY_QUOTA_PATTERNS: tuple[re.Pattern[str], ...] = ()
_AGY_RATE_LIMIT = re.compile(r"\b429\b|RESOURCE_EXHAUSTED")
_AGY_QUOTA_WORD = re.compile(r"quota", re.IGNORECASE)
_PROVIDER_FAILURE_KEYS = ("kind", "status_code", "source", "provider")


def classify_agy_error(status, text) -> dict | None:
    """只在 ERROR 终态做白名单分类。返回值只有四个字段，不含供应商原文。"""
    if status != "ERROR" or not isinstance(text, str):
        return None
    limited = _AGY_RATE_LIMIT.search(text) is not None
    quota_hit = _AGY_QUOTA_WORD.search(text) is not None or any(
        pattern.search(text) for pattern in AGY_QUOTA_PATTERNS
    )
    if quota_hit and (limited or any(pattern.search(text) for pattern in AGY_QUOTA_PATTERNS)):
        kind = "quota_exhausted"
    elif limited:
        kind = "rate_limited"
    else:
        return None
    return {
        "kind": kind,
        "status_code": 429,
        "source": "native_result",
        "provider": "antigravity",
    }


def _provider_failure(classified: dict) -> dict[str, Any]:
    return {key: classified[key] for key in _PROVIDER_FAILURE_KEYS}


class _ProtocolError(Exception):
    pass


def _public_tool_name(value: Any) -> str:
    if isinstance(value, str) and value in _KNOWN_TOOLS:
        return value
    return "other"


def _is_policy_denial(error_info: Any) -> bool:
    """True only for agy's native deny sentence in a {type?, message} object."""
    if not isinstance(error_info, dict) or not set(error_info).issubset(_POLICY_DENIED_KEYS):
        return False
    message = error_info.get("message")
    if not isinstance(message, str) or _POLICY_DENIED.fullmatch(message) is None:
        return False
    if "type" in error_info and not isinstance(error_info["type"], str):
        return False
    return True


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
    subagents: set[int] = field(default_factory=set)
    step_tools: dict[int, str] = field(default_factory=dict)
    cleaned: bool = False
    step_states: dict[int, str] = field(default_factory=dict)
    step_types: dict[int, str] = field(default_factory=dict)
    soft_denied: bool = False
    review_evidence: bool = False
    policy_denials: set[int] = field(default_factory=set)
    policy_denied_tools: set[str] = field(default_factory=set)
    soft_denied_tools: set[str] = field(default_factory=set)
    text: str = ""
    text_truncated: bool = False
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    events_seen: int = 0
    provider_failure: dict[str, Any] | None = None

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
            "unexplained_error_count": len((self.tool_errors | self.step_errors) - self.policy_denials),
            "soft_denied": self.soft_denied, "subagent_count": len(self.subagents),
            "summary_truncated": self.text_truncated,
            "policy_denied_count": len(self.policy_denials),
            "policy_denied_tools": sorted(self.policy_denied_tools),
            "soft_denied_tools": sorted(self.soft_denied_tools),
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
        # 缺字段或非 dict 时不输出，避免 native() 抛错把已确认终态打成未决。
        failure = self.provider_failure
        try:
            if isinstance(failure, dict) and all(key in failure for key in _PROVIDER_FAILURE_KEYS):
                native["provider_failure"] = _provider_failure(failure)
        except Exception:
            pass
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
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass

    @classmethod
    def _cleanup(cls, proc: subprocess.Popen) -> None:
        # Always address the group: a child may still own stdout after agy exits.
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
                # CLI 1.4+ re-labels an invoke_subagent tool step as "subagent"
                # once the delegate starts. Only that one transition is accepted.
                if not (state.step_types[index] == "tool" and kind == "subagent"
                        and state.step_tools.get(index) == "invoke_subagent"
                        and step.get("tool_name") == "invoke_subagent"):
                    raise _ProtocolError("step_type_changed")
            state.step_states[index] = status
            state.step_types[index] = kind
            if kind == "tool" and isinstance(step.get("tool_name"), str):
                if state.step_tools.setdefault(index, step["tool_name"]) != step["tool_name"]:
                    raise _ProtocolError("step_tool_changed")
            if status == "ERROR":
                state.step_errors.add(index)
            if kind == "subagent":
                state.active_tools.discard(index)
                state.subagents.add(index)
                if status == "ERROR":
                    state.tool_errors.add(index)
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
                    error_info = info["error"]
                    if _is_policy_denial(error_info):
                        state.policy_denials.add(index)
                        named = state.step_tools[index] if index in state.step_tools else info.get("name")
                        state.policy_denied_tools.add(_public_tool_name(named))
            if kind == "tool" and (state.step_tools.get(index) == "invoke_subagent"
                                   or (isinstance(step.get("tool_info"), dict)
                                       and step["tool_info"].get("name") == "invoke_subagent")):
                # Delegation is detected at its first frame; the run stops here.
                state.subagents.add(index)
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
        error = result.get("error")
        if error:
            if not isinstance(error, str):
                raise _ProtocolError("invalid_result_error")
            state.result["has_error"] = True
        # 分类失败只跳过 provider_failure，不改已确认的结果终态。
        try:
            raw_error = result.get("error")
            raw_response = result.get("response")
            error_text = raw_error if isinstance(raw_error, str) else ""
            response_text = raw_response if isinstance(raw_response, str) else ""
            classified = classify_agy_error(status, error_text + "\n" + response_text)
            if classified is not None:
                state.provider_failure = _provider_failure(classified)
        except Exception:
            state.provider_failure = None
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
                                              start_new_session=True, bufsize=0)
            except (OSError, ValueError, TypeError) as error:
                state.failure_type = type(error).__name__
                return self._snapshot(state, status="failed", terminated=True,
                                      error_code=BACKEND_UNAVAILABLE, summary="Antigravity 进程启动失败，未提交输入")
            self._exchange(state, text, publish, stopping)
            if state.subagents:
                return self._stop_delegated(state)
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
                if state.soft_denied and (state.review_evidence or state.soft_denied_tools):
                    names = ", ".join(sorted(state.soft_denied_tools)) or "未知工具"
                    summary = (
                        f"Antigravity 遇到需要人工审批的工具调用（{names}），无人值守模式下被软拒绝，"
                        "原生 CLI 随即结束本轮，未产出完整结果。请把所需文件放进已授权的读取目录，或调整提示避免该调用后重试。"
                    )
                    # 软拒绝直接结束本轮，是提前终止的原因；其它错误只计数，错误码仍为 REVIEW_REQUIRED。
                    other = (state.tool_errors | state.step_errors) - state.policy_denials
                    if other:
                        summary += f"另有 {len(other)} 个与权限无关的工具或步骤错误，修正授权后仍可能失败。"
                    if state.text:
                        summary += "\n\n" + state.text
                    state.failure_type = "soft_denied_review"
                    return self._snapshot(state, status="failed", terminated=True,
                                          error_code=ANTIGRAVITY_PERMISSION_REVIEW_REQUIRED, summary=summary)
                if state.soft_denied or state.result.get("has_error") or (state.tool_errors | state.step_errors) - state.policy_denials:
                    return self._snapshot(state, status="failed", terminated=True,
                                          error_code=ANTIGRAVITY_RUN_INCOMPLETE,
                                          summary=state.text or "Antigravity 步骤执行失败或权限被拒绝")
                if state.policy_denials and not state.text.strip():
                    state.failure_type = "no_final_response"
                    return self._snapshot(state, status="failed", terminated=True,
                                          error_code=ANTIGRAVITY_RUN_INCOMPLETE,
                                          summary="Antigravity 的工具调用被权限策略拒绝后没有产出最终结果")
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
            if state.proc is not None and not state.cleaned:
                self._cleanup(state.proc)
            with self._lock:
                self._runs.pop(run_id.value, None)
                self._cancelled.discard(run_id.value)

    @staticmethod
    def _group_gone(proc: subprocess.Popen) -> bool:
        if not _OWNS_PROCESS_GROUP:
            return False
        # Killed group members are reaped asynchronously; allow a short window.
        deadline = time.monotonic() + 1.0
        while True:
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                return True
            except PermissionError:
                return False
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)

    def _stop_delegated(self, state: _Run) -> dict[str, Any]:
        # Delegated work is never accepted. End the whole process group now and
        # report a terminal failure only once the group is confirmed gone, so
        # the scheduler slot is released immediately instead of after a timeout.
        assert state.proc is not None
        self._cleanup(state.proc)
        # Never signal this PGID again: once gone, the number may be reused.
        state.cleaned = True
        if state.proc.poll() is None or not self._group_gone(state.proc):
            state.failure_type = "subagent_process_alive"
            return self._unknown(state)
        state.failure_type = "subagent_delegated"
        return self._snapshot(state, status="failed", terminated=True,
                              error_code=ANTIGRAVITY_RUN_INCOMPLETE,
                              summary="Antigravity 派出了子代理；Asterun 不接受委派结果，已结束进程。请要求不使用子代理后重试")

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
                        decoded = diagnostic.decode("utf-8", errors="replace")
                        state.soft_denied |= bool(_DENIED.search(decoded))
                        state.review_evidence |= bool(_REVIEW_EVIDENCE.search(decoded))
                        state.soft_denied_tools.update(
                            _public_tool_name(name) for name in _SOFT_DENIED_TOOL.findall(decoded))
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
                        if state.subagents:
                            # agy 1.4 may never send a result while a delegate
                            # runs, or keep running after it; stop at once.
                            state.stage = "result" if state.result is not None else "subagent"
                            return
                        if state.result is None and (immediate or (state.text != last_text and time.monotonic() - last_publish >= PROGRESS_INTERVAL)):
                            publish(self._snapshot(state))
                            last_publish, last_text = time.monotonic(), state.text
                    if len(stdout_buffer) > MAX_FRAME_BYTES:
                        raise _ProtocolError("frame_limit")
            proc.wait(timeout=max(0.01, deadline - time.monotonic()))
            state.stage = "result" if state.result is not None else "eof"
