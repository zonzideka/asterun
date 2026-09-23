"""Grok ACP 适配。协议形状来自旧 backends/grok.py，默认路径与作者项目耦合已去掉。"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from threading import Event, Lock
from typing import Any
from uuid import uuid4

from asterun.backends.base import BackendCall, capability_rows
from asterun.config import BackendConfig
from asterun.contracts import ConnectionDisplay, RunStatus
from asterun.errors import (
    AUTH_REQUIRED, BACKEND_UNAVAILABLE, CAPABILITY_UNSUPPORTED, CONFIG_REQUIRED,
    INVALID_CONFIG, AsterunError,
)
from asterun.grok_profile import (
    CODE_PROFILE, CODE_SANDBOX, PROFILE, build_environment, validate_home, validate_workspace,
)
from asterun.plugins.builtins import GROK_EXECUTION_OPTIONS, validate_grok_execution_options
from asterun.ids import RunId, TaskId
from asterun.transport import LineJsonRpcTransport

LIVE_ENV = "ASTERUN_RUN_GROK"
# 产品代码不再默认指向作者家目录；发现只看配置、GROK_BIN 与 PATH。
DEFAULT_GROK_BIN = None

GROK_DISCOVERY_ENV = {
    "GROK_CLAUDE_MCPS_ENABLED": "false",
    "GROK_CURSOR_MCPS_ENABLED": "false",
    "GROK_CODEX_MCPS_ENABLED": "false",
    "GROK_MANAGED_MCPS_ENABLED": "false",
}
ASTERUN_RECURSION_DENY = "MCPTool(asterun__*)"
GROK_TOOL_ALIASES = {
    "Read": "read_file",
    "Grep": "grep",
    "Glob": "list_dir",
}
FALLBACK_GROK_MODELS = frozenset({"grok-4.6", "grok-4.6-build", "grok-4.5", "grok-composer-2.5-fast"})
FALLBACK_EFFORT_MODELS = frozenset({"grok-4.6", "grok-4.5"})
LEGACY_MODEL_ALIASES = {"grok-build": "grok-4.6"}
DEFAULT_GROK_MODEL = "grok-4.6"
VALID_REASONING_EFFORTS = frozenset({"low", "medium", "high"})
GROK_ACP_METHODS = ("initialize", "session/new", "session/prompt")
DISPATCH_RECEIPT_KEY = "asterun.grok_dispatch_v1"
PRE_PROMPT_STAGES = frozenset({"preflight", "spawn", "initialize", "session/new"})
CODE_TOOLS = (
    "read_file", "list_dir", "grep", "search_replace", "write_file", "run_terminal_cmd",
    "get_command_or_subagent_output", "wait_commands_or_subagents", "kill_command_or_subagent",
)
DEFAULT_CODE_MAX_TURNS = 12
DEFAULT_CODE_TIMEOUT_SECONDS = 900


def build_grok_code_command(grok_bin: str, *, cwd: Path, prompt_file: Path,
                            session_id: str, model: str | None, max_turns: int) -> list[str]:
    """Explicit coding grant, constrained by the native workspace sandbox and tool set."""
    validate_grok_execution_options({"execution_profile": CODE_PROFILE, "max_turns": max_turns}, "grok")
    return [grok_bin, "--cwd", str(cwd), "--sandbox", CODE_SANDBOX,
        "--permission-mode", "dontAsk", "--allow", "Bash", "--allow", "Read",
        "--allow", "Edit", "--allow", "Grep", "--deny", "MCPTool(*)",
        "--tools", ",".join(CODE_TOOLS), "--disallowed-tools", "search_tool,use_tool,Agent",
        "--disable-web-search", "--no-subagents", "--no-plan",
        "--model", resolve_grok_model(model), "--max-turns", str(max_turns),
        "--session-id", session_id, "--prompt-file", str(prompt_file),
        "--output-format", "streaming-json"]


def live_grok_enabled() -> bool:
    return os.environ.get(LIVE_ENV) == "1"


def discover_grok_bin(explicit: str | None = None) -> Path | None:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get("GROK_BIN")
    if env:
        candidates.append(Path(env))
    which = shutil.which("grok")
    if which:
        candidates.append(Path(which))
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            # spawn 使用工作区 cwd；先固定被检查的文件，避免相对路径改指工作区内另一程序。
            return path.resolve()
    return None


def models_cache_path() -> Path | None:
    """只在显式给出 GROK_MODELS_CACHE 时读缓存。导入期不读 ~/.grok。"""
    raw = os.environ.get("GROK_MODELS_CACHE")
    if not raw:
        return None
    return Path(raw)


def _models_cache() -> dict[str, Any]:
    path = models_cache_path()
    if path is None:
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw.get("models", {}) or {}
    except Exception:
        return {}


def known_grok_models() -> set[str]:
    cached = set(_models_cache())
    return cached or set(FALLBACK_GROK_MODELS)


def resolve_grok_model(requested: str | None) -> str:
    # 配置绑定必须决定实际模型；缓存缺失或过时不能静默改派别的模型。
    if requested is None:
        return DEFAULT_GROK_MODEL
    if not isinstance(requested, str) or not requested.strip() or requested != requested.strip() or "\x00" in requested:
        raise AsterunError(INVALID_CONFIG, "Grok model 必须是非空模型 ID，且不能含首尾空白")
    return LEGACY_MODEL_ALIASES.get(requested, requested)


def supports_reasoning_effort(model: str) -> bool:
    info = _models_cache().get(model, {}).get("info", {})
    if info:
        return bool(info.get("supports_reasoning_effort"))
    return model in FALLBACK_EFFORT_MODELS


def resolve_reasoning_effort(requested: str | None, model: str) -> str | None:
    effort = (requested or "").strip().lower()
    if effort in VALID_REASONING_EFFORTS and supports_reasoning_effort(model):
        return effort
    return None


def _grok_tool_ids(tools: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(GROK_TOOL_ALIASES.get(tool, tool) for tool in tools))


def build_grok_agent_command(
    grok_bin: str,
    *,
    bash_deny: list[str] | None = None,
    extra_mcp_deny: list[str] | None = None,
    allowed_tools: tuple[str, ...] = (),
    disallowed_tools: tuple[str, ...] = (),
    rules: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    max_turns: int | None = None,
    worktree: str | None = None,
    sandbox: str | None = None,
) -> list[str]:
    # 当前发布范围固定为单轮纯文本；调用者不能通过旧 helper 参数放宽。
    if allowed_tools or worktree or max_turns not in (None, 1) or sandbox not in (None, "read-only"):
        raise AsterunError(CAPABILITY_UNSUPPORTED, "Grok 当前仅支持 read-only、无工具、单轮文本配置")
    cmd = [grok_bin]
    for pattern in bash_deny or []:
        cmd.extend(["--deny", f"Bash({pattern})"])
    for tool in extra_mcp_deny or []:
        cmd.extend(["--deny", tool if tool.startswith("MCPTool(") else f"MCPTool({tool})"])
    cmd.extend(["--deny", ASTERUN_RECURSION_DENY])
    for tool in disallowed_tools:
        cmd.extend(["--deny", f"{tool}(*)"])
    cmd.extend(["--deny", "*", "--tools", "", "--permission-mode", "dontAsk"])
    if rules:
        cmd.extend(["--rules", rules])
    resolved_model = resolve_grok_model(model)
    cmd.extend(["--model", resolved_model])
    effort = resolve_reasoning_effort(reasoning_effort, resolved_model)
    if effort:
        cmd.extend(["--reasoning-effort", effort])
    cmd.extend([
        "--disable-web-search", "--no-subagents", "--no-plan", "--max-turns", "1",
        "--sandbox", "read-only", "agent", "--no-leader", "stdio",
    ])
    return cmd


def build_grok_subprocess_env(
    base_env: dict[str, str] | None = None, *, home: str | Path,
) -> dict[str, str]:
    return build_environment(home, base_env=base_env)


def build_initialize_params(*, protocol_version: int = 1) -> dict[str, Any]:
    return {"protocolVersion": protocol_version}


def build_session_new_params(*, cwd: str, mcp_servers: list[Any] | None = None) -> dict[str, Any]:
    return {"cwd": cwd, "mcpServers": list(mcp_servers or [])}


def build_session_prompt_params(*, session_id: str, text: str) -> dict[str, Any]:
    return {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": text}],
    }


class GrokAcpTransport:
    """ACP stdio 传输。initialize 成功不等于续接已通过。"""

    def __init__(
        self,
        *,
        grok_bin: str,
        bash_deny: list[str] | None = None,
        extra_mcp_deny: list[str] | None = None,
        allowed_tools: tuple[str, ...] = (),
        model: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.grok_bin = grok_bin
        self.bash_deny = bash_deny or []
        self.extra_mcp_deny = extra_mcp_deny or []
        self.allowed_tools = allowed_tools
        self.model = model
        self.env = None if env is None else dict(env)
        self._transport = LineJsonRpcTransport(include_jsonrpc=True)
        self._pending_acp_requests: dict[str, dict[str, Any]] = {}

    def spawn(self, cwd: str | Path) -> None:
        if self.env is None or not self.env.get("HOME") or not Path(self.env["HOME"]).is_absolute():
            raise AsterunError(CONFIG_REQUIRED, "Grok 传输必须显式提供独立 HOME 环境")
        cmd = build_grok_agent_command(
            self.grok_bin,
            bash_deny=self.bash_deny,
            extra_mcp_deny=self.extra_mcp_deny,
            allowed_tools=self.allowed_tools,
            model=self.model,
        )
        self._transport.spawn(cmd, cwd=cwd, env=self.env)
        time.sleep(0.05)
        if self._transport.proc and self._transport.proc.poll() is not None:
            err = self._transport.proc.stderr.read() if self._transport.proc.stderr else ""
            raise RuntimeError(f"grok exited immediately: {err[:500]}")

    def initialize(self, timeout: float = 20.0) -> dict[str, Any]:
        return self.request("initialize", build_initialize_params(), timeout=timeout)

    def new_session(self, cwd: str | Path, timeout: float = 30.0) -> dict[str, Any]:
        return self.request("session/new", build_session_new_params(cwd=str(cwd)), timeout=timeout)

    def prompt(self, session_id: str, text: str, timeout: float = 180.0) -> dict[str, Any]:
        return self.request(
            "session/prompt",
            build_session_prompt_params(session_id=session_id, text=text),
            timeout=timeout,
        )

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
        return self._transport.request(method, params, timeout)

    def on_notification(self, cb) -> None:
        def track_and_forward(msg: dict[str, Any]) -> None:
            if "id" in msg and "method" in msg:
                self._pending_acp_requests[msg["method"]] = msg
            cb(msg)

        self._transport.on_request(track_and_forward)
        self._transport.on_notification(cb)

    def close(self) -> None:
        self._transport.close()

    @property
    def alive(self) -> bool:
        return self._transport.alive

    @property
    def exit_code(self) -> int | None:
        return self._transport.proc.poll() if self._transport.proc is not None else None


class GrokBackend:
    def __init__(self, config: BackendConfig) -> None:
        validate_grok_execution_options({key: getattr(config, key) for key in GROK_EXECUTION_OPTIONS
                                         if getattr(config, key) is not None}, f"backends.{config.name}")
        self.config = config
        self.calls: list[BackendCall] = []
        self._active_transports = {}
        self._code_lock = Lock()
        self._active_code_handles = set()
        self._closing = False

    @property
    def execution_profile(self) -> str:
        return self.config.execution_profile or PROFILE

    def _sync_native_session(self, session_id):
        if not session_id or not self.config.session_sync_home:
            return
        from asterun.grok_sync import sync_sessions
        try:
            report = sync_sessions(self.config.home, self.config.session_sync_home,
                                   session_id=session_id)
            if report["skipped"] or report["conflicts"]:
                logging.getLogger(__name__).warning("Grok session sync incomplete: %s", report)
        except Exception as error:
            # Export failures must never change dispatch/delivery/acceptance semantics.
            logging.getLogger(__name__).warning("Grok session sync failed: %s", type(error).__name__)

    def close(self):
        with self._code_lock:
            self._closing = True
            handles = tuple(self._active_code_handles)
        for handle in handles:
            try:
                handle.close()
            except Exception:
                # The worker retains an uncertain result and retries cleanup;
                # one failed cleanup must not skip the remaining owned groups.
                pass
        for transport in list(self._active_transports.values()):
            transport.close()

    @property
    def dispatch_count(self) -> int:
        return sum(1 for item in self.calls if item.method == "dispatch")

    def discover_bin(self) -> Path | None:
        return discover_grok_bin(self.config.bin)

    def can_dispatch(self) -> bool:
        if not (self.config.enabled and live_grok_enabled() and self.discover_bin()):
            return False
        try:
            self._prepared_home()
        except AsterunError:
            return False
        return True

    def _prepared_home(self) -> Path:
        if not self.config.home:
            raise AsterunError(
                CONFIG_REQUIRED,
                "Grok 尚未配置独立 home",
                next_action="准备独立 Grok home 并在该目录独立登录，再设置 backends.<name>.home",
            )
        return validate_home(self.config.home, execution_profile=self.execution_profile)

    def unavailable_error(self) -> AsterunError:
        binary = self.discover_bin()
        if binary is None:
            return AsterunError(
                BACKEND_UNAVAILABLE,
                "未发现 Grok 可执行文件",
                next_action="设置 GROK_BIN 或把 grok 放进 PATH；不要使用作者机器上的默认路径",
                details={"is_real_connection": False, "live_flag": live_grok_enabled()},
            )
        if not live_grok_enabled():
            return AsterunError(
                AUTH_REQUIRED,
                "Grok 真实派发默认关闭",
                next_action=f"确认账户可用后设置 {LIVE_ENV}=1；网页登录不能单独证明 CLI 可用",
                details={"binary": str(binary), "is_real_connection": False},
            )
        try:
            self._prepared_home()
        except AsterunError as error:
            return error
        return AsterunError(BACKEND_UNAVAILABLE, "Grok 当前不可派发")

    def inspect(self) -> dict[str, Any]:
        self.calls.append(BackendCall("inspect"))
        binary = self.discover_bin()
        return {
            "backend": self.config.name,
            "kind": "grok",
            "enabled_in_config": self.config.enabled,
            "execution_enabled": self.can_dispatch(),
            "is_real_connection": False,
            "connection_display": str(ConnectionDisplay.NONE),
            "authentication": "not_tested",
            "binary": None if binary is None else str(binary),
            "binary_found": binary is not None,
            "live_flag": live_grok_enabled(),
            "home": self.config.home,
            "model": resolve_grok_model(self.config.model),
            "execution_profile": self.execution_profile,
            "transport": "headless" if self.execution_profile == CODE_PROFILE else "acp",
            "max_turns": (self.config.max_turns or DEFAULT_CODE_MAX_TURNS) if self.execution_profile == CODE_PROFILE else 1,
            "timeout_seconds": self.config.timeout_seconds or (DEFAULT_CODE_TIMEOUT_SECONDS if self.execution_profile == CODE_PROFILE else 180),
            "handshake_is_not_resume": True,
            "native_resume_verified": False,
            "capabilities": [item.to_dict() for item in capability_rows("grok")],
            "methods": [] if self.execution_profile == CODE_PROFILE else list(GROK_ACP_METHODS),
            "message": "inspect 不启动进程；文本使用 ACP，显式编码使用有界 headless。原生续接与取消仍未验证。",
        }

    def verify_connection(self) -> dict[str, Any]:
        self.calls.append(BackendCall("verify_connection"))
        binary = self.discover_bin()
        return {
            "backend": self.config.name,
            "kind": "grok",
            "is_real_connection": False,
            "connection_display": str(ConnectionDisplay.NONE),
            "authentication": "not_tested",
            "binary_found": binary is not None,
            "binary": None if binary is None else str(binary),
            "live_flag": live_grok_enabled(),
            "simulated": False,
            "verified": False,
            "handshake_is_not_resume": True,
            "native_resume_verified": False,
            "message": "默认只做安装发现，不 spawn、不 initialize。握手成功也不能写成续接已通过。",
        }

    def dispatch_stream(self, task_id: TaskId, run_id: RunId, script: str, text: str, *,
                        cwd: Path | None = None, native=None, publish=None, stopping=None) -> dict[str, Any]:
        if self.execution_profile != CODE_PROFILE:
            return self.dispatch(task_id, run_id, script, text, cwd=cwd)
        return self._dispatch_code(task_id, run_id, script, text, cwd=cwd, publish=publish,
                                   stopping=stopping if stopping is not None else Event())

    def _dispatch_code(self, task_id: TaskId, run_id: RunId, script: str, text: str, *,
                       cwd: Path | None, publish=None, stopping=None) -> dict[str, Any]:
        from asterun.backends.base import require_dispatch_cwd
        from asterun.backends.grok_code import CodeProcessHandle, run_code

        self.calls.append(BackendCall("dispatch", {"task_id": task_id.value, "run_id": run_id.value,
                                                   "script": script, "text": text}))
        runner_entered = False
        session_id = ""
        process_handle = None
        try:
            if not self.can_dispatch():
                raise self.unavailable_error()
            cwd = require_dispatch_cwd(cwd)
            validate_workspace(cwd, execution_profile=CODE_PROFILE)
            home = self._prepared_home()
            binary = self.discover_bin()
            assert binary is not None
            # Prompt files are private, temporary, and outside the source workspace.
            with tempfile.TemporaryDirectory(prefix="asterun-grok-code-") as directory:
                prompt_file = Path(directory) / "prompt.txt"
                fd = os.open(prompt_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(text)
                session_id = str(uuid4())
                command = build_grok_code_command(str(binary), cwd=cwd, prompt_file=prompt_file,
                    session_id=session_id, model=self.config.model,
                    max_turns=self.config.max_turns or DEFAULT_CODE_MAX_TURNS)
                env = build_environment(home, execution_profile=CODE_PROFILE)
                with self._code_lock:
                    if self._closing:
                        raise AsterunError(BACKEND_UNAVAILABLE, "Grok 后端正在关闭，未启动编码进程")
                    process_handle = CodeProcessHandle()
                    self._active_code_handles.add(process_handle)
                runner_entered = True
                return run_code(command=command, env=env, cwd=cwd, session_id=session_id,
                    task_id=task_id.value, run_id=run_id.value,
                    timeout_seconds=self.config.timeout_seconds or DEFAULT_CODE_TIMEOUT_SECONDS,
                    publish=publish, stopping=stopping if stopping is not None else Event(),
                    process_handle=process_handle)
        except Exception as error:
            # An unexpected runner/cleanup exception cannot prove the prompt was not sent.
            return {"status": str(RunStatus.PENDING_RECONCILE if runner_entered else RunStatus.FAILED),
                "terminated": not runner_entered,
                "error_code": "REMOTE_STATE_UNKNOWN" if runner_entered else (
                    error.code if isinstance(error, AsterunError) else BACKEND_UNAVAILABLE),
                "summary": "Grok 编码运行结果未确认，需要对账" if runner_entered else "Grok 编码派发准备失败，尚未提交模型轮次", "events": [],
                "native": {"execution_profile": CODE_PROFILE, "native_resumed": False, "session_id": session_id,
                    DISPATCH_RECEIPT_KEY: {"task_id": task_id.value, "run_id": run_id.value,
                        "stage": "prompt" if runner_entered else "preflight", "prompt_attempted": runner_entered,
                        "delivery": "may_have_been_sent" if runner_entered else "not_sent",
                        "failure_type": type(error).__name__, "process_exit_code": None}}}
        finally:
            if process_handle is not None:
                try:
                    process_handle.close()
                except Exception:
                    pass
                finally:
                    with self._code_lock:
                        self._active_code_handles.discard(process_handle)
            self._sync_native_session(session_id)

    def dispatch(self, task_id: TaskId, run_id: RunId, script: str, text: str, *, cwd: Path | None = None) -> dict[str, Any]:
        if self.execution_profile == CODE_PROFILE:
            return self._dispatch_code(task_id, run_id, script, text, cwd=cwd)
        self.calls.append(
            BackendCall("dispatch", {"task_id": task_id.value, "run_id": run_id.value, "script": script, "text": text})
        )
        transport = None
        resolved_model = None
        chunks: list[str] = []
        session_id = ""
        init: dict[str, Any] = {}
        stage = "preflight"
        prompt_attempted = False

        def native_receipt(error: Exception | None = None) -> dict[str, Any]:
            caps = init.get("agentCapabilities") if isinstance(init, dict) else None
            return {
                "session_id": session_id,
                "initialize_keys": sorted(init) if isinstance(init, dict) else [],
                "loadSession_declared": bool(caps.get("loadSession")) if isinstance(caps, dict) else False,
                "handshake_is_not_resume": True, "native_resumed": False,
                "requested_model": self.config.model, "configured_model": resolved_model,
                "execution_profile": "text-only-v1",
                # Only this adapter constructs this receipt; native replies are never merged into it.
                DISPATCH_RECEIPT_KEY: {
                    "task_id": task_id.value, "run_id": run_id.value, "stage": stage,
                    "prompt_attempted": prompt_attempted,
                    "delivery": "not_sent" if not prompt_attempted else "may_have_been_sent",
                    "failure_type": None if error is None else type(error).__name__,
                    "process_exit_code": getattr(transport, "exit_code", None),
                },
            }

        def collect_public_message(message: dict[str, Any]) -> None:
            params = message.get("params") or {}
            if message.get("method") != "session/update" or params.get("sessionId") != session_id:
                return
            update = params.get("update") or {}
            content = update.get("content") or {}
            if update.get("sessionUpdate") == "agent_message_chunk" and content.get("type") == "text":
                chunks.append(str(content.get("text") or ""))

        try:
            if not self.can_dispatch():
                raise self.unavailable_error()
            from asterun.backends.base import require_dispatch_cwd

            cwd = require_dispatch_cwd(cwd)
            validate_workspace(cwd)
            binary = self.discover_bin()
            assert binary is not None
            home = self._prepared_home()
            resolved_model = resolve_grok_model(self.config.model)
            transport = GrokAcpTransport(
                grok_bin=str(binary), model=resolved_model, env=build_environment(home),
            )
            self._active_transports[run_id.value] = transport
            transport.on_notification(collect_public_message)
            stage = "spawn"
            transport.spawn(cwd)
            stage = "initialize"
            init = transport.initialize()
            stage = "session/new"
            session = transport.new_session(cwd)
            if not isinstance(session, dict) or not isinstance(session.get("sessionId"), str) or not session["sessionId"].strip():
                raise RuntimeError("Grok session/new 未返回有效 sessionId")
            session_id = session["sessionId"]
            stage = "prompt"
            # Set before the call: any write/flush/timeout failure from here remains uncertain.
            prompt_attempted = True
            prompt_options = {} if self.config.timeout_seconds is None else {"timeout": self.config.timeout_seconds}
            result = transport.prompt(session_id, text or "Reply with ASTERUN_GROK_OK.", **prompt_options)
            if not isinstance(result, dict) or (
                result.get("stopReason") is not None and not isinstance(result["stopReason"], str)
            ):
                raise RuntimeError("Grok prompt 返回格式无效")
        except Exception as error:
            code = "REMOTE_STATE_UNKNOWN" if prompt_attempted else (
                error.code if isinstance(error, AsterunError) else BACKEND_UNAVAILABLE)
            native = native_receipt(error)
            return {
                "status": str(RunStatus.PENDING_RECONCILE if prompt_attempted else RunStatus.FAILED),
                "terminated": not prompt_attempted, "error_code": code,
                "summary": "Grok 已开始提交轮次，结果未确认，需要对账" if prompt_attempted else
                           f"Grok 在 {stage} 阶段失败，尚未提交模型轮次",
                "events": [{"type": "reconciled", "stage": stage,
                            "prompt_attempted": prompt_attempted, "error_code": code,
                            "failure_type": type(error).__name__,
                            "process_exit_code": native[DISPATCH_RECEIPT_KEY]["process_exit_code"]}],
                "native": native,
            }
        finally:
            try:
                if transport is not None:
                    transport.close()
            except Exception:
                pass  # Cleanup cannot change whether prompt was called or replace a received terminal result.
            finally:
                self._active_transports.pop(run_id.value, None)
                self._sync_native_session(session_id)
        reason = result.get("stopReason")
        if reason == "end_turn":
            status, error_code = RunStatus.SUCCEEDED, None
        elif reason == "cancelled":
            status, error_code = RunStatus.CANCELLED, None
        elif reason in {"max_tokens", "max_turn_requests", "refusal"}:
            status, error_code = RunStatus.FAILED, "GROK_RUN_INCOMPLETE"
        else:
            status, error_code = RunStatus.PENDING_RECONCILE, "REMOTE_STATE_UNKNOWN"
        return {
            "status": str(status),
            "terminated": status != RunStatus.PENDING_RECONCILE,
            "error_code": error_code,
            "events": [
                {"type": "run_started", "text": "grok acp"},
                {"type": "message", "text": "".join(chunks)},
            ],
            "summary": "".join(chunks),
            "native": {**native_receipt(), "stop_reason": reason},
        }

    def request_cancel(self, run_id: RunId, script: str) -> dict[str, Any]:
        self.calls.append(BackendCall("request_cancel", {"run_id": run_id.value, "script": script}))
        raise AsterunError(
            BACKEND_UNAVAILABLE,
            "Grok 取消的现场确认尚未验证",
            next_action="取消请求可记录，但不能把未确认的中断写成已终止",
        )

    def respond_approval(self, run_id: RunId, decision: str) -> dict[str, Any]:
        self.calls.append(BackendCall("respond_approval", {"run_id": run_id.value, "decision": decision}))
        raise AsterunError(CAPABILITY_UNSUPPORTED, "Grok 审批桥接尚未做现场验证")
