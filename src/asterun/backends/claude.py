"""Claude CLI 一次性适配。协议形状来自旧 backends/claude.py，去掉旧角色档案与作者项目耦合。"""

from __future__ import annotations

import json
import math
import os
import shutil
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from asterun.backends.base import BackendCall, capability_rows
from asterun.config import BackendConfig
from asterun.contracts import ConnectionDisplay, RunStatus
from asterun.errors import AUTH_REQUIRED, BACKEND_UNAVAILABLE, CAPABILITY_UNSUPPORTED, AsterunError
from asterun.ids import RunId, TaskId

LIVE_ENV = "ASTERUN_RUN_CLAUDE"
DEFAULT_CLAUDE_BIN = None
DEPTH_ENV = "ASTERUN_DEPTH"
PARENT_ENV = "ASTERUN_PARENT_RUN_ID"
_SCRUB_PREFIXES = ("CODEX_", "CLAUDE_CODE_", "NODE_REPL_")
_SCRUB_KEEP = {"CLAUDE_CODE_OAUTH_TOKEN"}

Runner = Callable[[list[str], str, dict[str, str], float], tuple[str, str, int]]


def live_claude_enabled() -> bool:
    return os.environ.get(LIVE_ENV) == "1"


def discover_claude_bin(explicit: str | None = None) -> Path | None:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get("CLAUDE_BIN")
    if env:
        candidates.append(Path(env))
    which = shutil.which("claude")
    if which:
        candidates.append(Path(which))
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


@dataclass
class ClaudeSession:
    session_id: str
    cwd: str
    started_at: str
    status: str = "running"
    stop_reason: str = ""
    result_text: str = ""
    is_error: bool = False
    total_cost_usd: float = 0.0
    cost_reported: bool = field(default=False, kw_only=True)
    num_turns: int = 0
    provider_session_id: str = ""
    last_error: str = ""
    proc: Any = None
    _complete: threading.Event = field(default_factory=threading.Event)

    def to_status_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "mode": "claude",
            "status": self.status,
            "stop_reason": self.stop_reason,
            "started_at": self.started_at,
            "is_error": self.is_error,
            "message": self.result_text,
            "message_preview": self.result_text[:500],
            "total_cost_usd": self.total_cost_usd,
            "cost_reported": self.cost_reported,
            "cost_source": "claude_cli_result" if self.cost_reported else "unknown",
            "num_turns": self.num_turns,
            "claude_session_id": self.provider_session_id,
            "last_error": self.last_error,
            "resume_supported": False,
        }

    def cancel(self) -> bool:
        self.status = "cancelled"
        self.stop_reason = "user_cancelled"
        proc = self.proc
        killed = False
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                killed = True
            except (ProcessLookupError, PermissionError, OSError):
                pass
        return killed


def build_command(
    claude_bin: str,
    prompt: str,
    *,
    allowed_tools: tuple[str, ...] = (),
    disallowed_tools: tuple[str, ...] = (),
    permission_mode: str | None = None,
    append_system_prompt: str | None = None,
    model: str | None = None,
    snapshot_only: bool = False,
) -> list[str]:
    cmd = [claude_bin, "-p", prompt, "--output-format", "json"]
    if permission_mode:
        cmd += ["--permission-mode", permission_mode]
    if append_system_prompt:
        cmd += ["--append-system-prompt", append_system_prompt]
    if model:
        cmd += ["--model", model]
    if disallowed_tools:
        cmd += ["--disallowed-tools", *disallowed_tools]
    if allowed_tools:
        cmd += ["--allowed-tools", *allowed_tools]
    if snapshot_only:
        cmd += ["--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--disallowed-tools", "*", "--permission-mode", "dontAsk",
                "--setting-sources", "", "--settings", '{"disableAllHooks":true}',
                "--disable-slash-commands", "--no-chrome", "--no-session-persistence"]
    return cmd


def build_env(
    *,
    depth: int = 1,
    parent_run_id: str = "",
    base_env: dict[str, str] | None = None,
    extra_scrub: tuple[str, ...] = ("ANTHROPIC_API_KEY",),
) -> dict[str, str]:
    env = dict(os.environ if base_env is None else base_env)
    for var in extra_scrub:
        env.pop(var, None)
    for key in [item for item in env if item.startswith(_SCRUB_PREFIXES) and item not in _SCRUB_KEEP]:
        env.pop(key, None)
    env[DEPTH_ENV] = str(depth)
    if parent_run_id:
        env[PARENT_ENV] = parent_run_id
    return env


def parse_result(stdout: str) -> dict[str, Any]:
    data = json.loads(stdout)
    cost = data.get("total_cost_usd")
    try:
        parsed_cost = float(cost) if type(cost) in (int, float) else float("nan")
    except OverflowError:
        parsed_cost = float("nan")
    cost_reported = math.isfinite(parsed_cost) and parsed_cost >= 0
    return {
        "is_error": bool(data.get("is_error", False)),
        "result": data.get("result", "") or "",
        "session_id": data.get("session_id", "") or "",
        "total_cost_usd": parsed_cost if cost_reported else 0.0,
        "cost_reported": cost_reported,
        "num_turns": int(data.get("num_turns", 0) or 0),
        "stop_reason": data.get("stop_reason", "") or "",
    }


def apply_result(session: ClaudeSession, parsed: dict[str, Any]) -> None:
    session.result_text = parsed.get("result", "")
    session.provider_session_id = parsed.get("session_id", "")
    session.total_cost_usd = parsed.get("total_cost_usd", 0.0)
    session.cost_reported = parsed.get("cost_reported") is True
    session.num_turns = parsed.get("num_turns", 0)
    session.stop_reason = parsed.get("stop_reason", "")
    session.is_error = parsed.get("is_error", False)
    session.status = "failed" if session.is_error else "completed"


def _terminate_proc(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except Exception:
            pass


def run_claude_blocking(
    claude_bin: str,
    session: ClaudeSession,
    prompt: str,
    cwd: str,
    *,
    allowed_tools: tuple[str, ...] = (),
    disallowed_tools: tuple[str, ...] = (),
    permission_mode: str | None = None,
    append_system_prompt: str | None = None,
    model: str | None = None,
    depth: int = 1,
    timeout: float = 300.0,
    runner: Runner | None = None,
    parent_run_id: str = "",
    base_env: dict[str, str] | None = None,
    snapshot_only: bool = False,
) -> None:
    cmd = build_command(
        claude_bin,
        prompt,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        permission_mode=permission_mode,
        append_system_prompt=append_system_prompt,
        model=model,
        snapshot_only=snapshot_only,
    )
    env = build_env(depth=depth, parent_run_id=parent_run_id, base_env=base_env)
    try:
        if session.status == "cancelled":
            return
        if runner is None:
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            session.proc = proc
            if session.status == "cancelled":
                _terminate_proc(proc)
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
                returncode = proc.returncode
            except subprocess.TimeoutExpired:
                _terminate_proc(proc)
                try:
                    stdout, stderr = proc.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    stdout, stderr = proc.communicate(timeout=2)
                if session.status != "cancelled":
                    session.status = "pending_reconcile"
                    session.stop_reason = "timeout"
                    session.last_error = f"timeout after {timeout}s"
                return
        else:
            stdout, stderr, returncode = runner(cmd, str(cwd), env, timeout)
        if session.status in {"cancelled", "pending_reconcile"}:
            return
        if not (stdout or "").strip():
            session.status = "failed"
            session.last_error = ((stderr or "").strip() or "empty output")[:500]
            return
        apply_result(session, parse_result(stdout))
        if returncode != 0 and session.status == "completed":
            session.status = "failed"
            session.last_error = f"Claude 进程退出码：{returncode}"
    except Exception as exc:
        if session.status != "cancelled":
            session.status = "failed"
            session.last_error = str(exc)
    finally:
        session._complete.set()


class ClaudeBackend:
    def __init__(self, config: BackendConfig) -> None:
        self.config = config
        self.calls: list[BackendCall] = []
        self.runner: Runner | None = None
        self._sessions = {}
        self._cancelled = set()

    def close(self):
        for session in list(self._sessions.values()):
            if session.proc is not None and session.proc.poll() is None:
                session.status = "pending_reconcile"
                _terminate_proc(session.proc)
                try:
                    session.proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    os.killpg(session.proc.pid, signal.SIGKILL)
                    session.proc.wait(timeout=1)

    @property
    def dispatch_count(self) -> int:
        return sum(1 for item in self.calls if item.method == "dispatch")

    def discover_bin(self) -> Path | None:
        return discover_claude_bin(self.config.bin)

    def can_dispatch(self) -> bool:
        return bool(self.config.enabled and live_claude_enabled() and self.discover_bin())

    def unavailable_error(self) -> AsterunError:
        binary = self.discover_bin()
        if binary is None:
            return AsterunError(
                BACKEND_UNAVAILABLE,
                "未发现 Claude 可执行文件",
                next_action="设置 CLAUDE_BIN 或把 claude 放进 PATH；不要使用作者机器上的默认路径",
                details={"is_real_connection": False, "live_flag": live_claude_enabled()},
            )
        if not live_claude_enabled():
            return AsterunError(
                AUTH_REQUIRED,
                "Claude 真实派发默认关闭",
                next_action=f"确认账户可用后设置 {LIVE_ENV}=1；网页登录不能单独证明 CLI 可用",
                details={"binary": str(binary), "is_real_connection": False},
            )
        return AsterunError(BACKEND_UNAVAILABLE, "Claude 当前不可派发")

    def inspect(self) -> dict[str, Any]:
        self.calls.append(BackendCall("inspect"))
        binary = self.discover_bin()
        return {
            "backend": self.config.name,
            "kind": "claude",
            "enabled_in_config": self.config.enabled,
            "execution_enabled": self.can_dispatch(),
            "is_real_connection": False,
            "connection_display": str(ConnectionDisplay.NONE),
            "authentication": "not_tested",
            "binary": None if binary is None else str(binary),
            "binary_found": binary is not None,
            "live_flag": live_claude_enabled(),
            "one_shot": True,
            "resume_supported": False,
            "capabilities": [item.to_dict() for item in capability_rows("claude")],
            "message": "已迁入 claude -p 一次性协议；inspect 不启动进程。续接明确不支持。",
        }

    def verify_connection(self) -> dict[str, Any]:
        self.calls.append(BackendCall("verify_connection"))
        binary = self.discover_bin()
        return {
            "backend": self.config.name,
            "kind": "claude",
            "is_real_connection": False,
            "connection_display": str(ConnectionDisplay.NONE),
            "authentication": "not_tested",
            "binary_found": binary is not None,
            "binary": None if binary is None else str(binary),
            "live_flag": live_claude_enabled(),
            "simulated": False,
            "verified": False,
            "one_shot": True,
            "resume_supported": False,
            "message": "默认只做安装发现，不 spawn。Claude 保持一次性能力，不能写成可续接。",
        }

    def dispatch(self, task_id: TaskId, run_id: RunId, script: str, text: str, *, cwd: Path | None = None) -> dict[str, Any]:
        return self._dispatch(task_id, run_id, script, text, cwd=cwd)

    def dispatch_review(self, task_id, run_id, script, text, *, cwd=None):
        from tempfile import TemporaryDirectory
        from asterun.backends.base import require_dispatch_cwd

        require_dispatch_cwd(cwd)
        # 只传快照；原工作区不是子进程 cwd，不加载其中的设置、hooks 或 CLAUDE.md。
        with TemporaryDirectory(prefix="asterun-review-") as directory:
            return self._dispatch(task_id, run_id, script, text, cwd=Path(directory), snapshot_only=True)

    def _dispatch(self, task_id, run_id, script, text, *, cwd=None, snapshot_only=False):
        self.calls.append(
            BackendCall("dispatch_review" if snapshot_only else "dispatch",
                        {"task_id": task_id.value, "run_id": run_id.value, "script": script, "text": text})
        )
        if not self.can_dispatch():
            raise self.unavailable_error()
        from asterun.backends.base import require_dispatch_cwd

        cwd = require_dispatch_cwd(cwd)
        binary = self.discover_bin()
        assert binary is not None
        session = ClaudeSession(
            session_id=run_id.value,
            cwd=str(cwd),
            started_at="",
        )
        self._sessions[run_id.value] = session
        try:
            if run_id.value in self._cancelled:
                session.status = "cancelled"
            run_claude_blocking(
                str(binary), session, text or "Reply with ASTERUN_CLAUDE_OK.", str(cwd),
                allowed_tools=() if snapshot_only else ("Read", "Grep", "Glob"), runner=self.runner,
                snapshot_only=snapshot_only,
            )
        finally:
            self._sessions.pop(run_id.value, None)
            self._cancelled.discard(run_id.value)
        native = session.to_status_dict()
        native["backend_session_id"] = session.provider_session_id or None
        if session.status == "cancelled":
            return {"status": "cancelled", "terminated": True, "summary": "Claude 进程已结束",
                    "native": native}
        if session.status == "completed":
            return {
                "status": str(RunStatus.SUCCEEDED),
                "terminated": True,
                "events": [
                    {"type": "run_started", "text": "claude -p"},
                    {"type": "run_succeeded", "text": session.result_text},
                ],
                "summary": session.result_text,
                "native": native,
            }
        if session.status == "pending_reconcile":
            return {"status": str(RunStatus.PENDING_RECONCILE), "terminated": False,
                    "error_code": "REMOTE_STATE_UNKNOWN", "summary": "Claude 请求超时，远端结果待对账",
                    "native": native, "events": []}
        return {
            "status": str(RunStatus.FAILED),
            "terminated": True,
            "error_code": "CLAUDE_RUN_FAILED",
            "events": [{"type": "run_failed", "text": session.last_error or session.result_text}],
            "summary": session.last_error or session.result_text,
            "native": native,
        }

    def request_cancel(self, run_id: RunId, script: str) -> dict[str, Any]:
        self.calls.append(BackendCall("request_cancel", {"run_id": run_id.value, "script": script}))
        self._cancelled.add(run_id.value)
        session = self._sessions.get(run_id.value)
        if session is not None:
            session.cancel()
        return {"cancel_requested": True, "terminated": False}

    def respond_approval(self, run_id: RunId, decision: str) -> dict[str, Any]:
        self.calls.append(BackendCall("respond_approval", {"run_id": run_id.value, "decision": decision}))
        raise AsterunError(CAPABILITY_UNSUPPORTED, "Claude 一次性适配没有审批桥接")
