"""Codex App Server 适配。协议形状来自旧 backends/codex.py，默认路径与 TR 耦合已去掉。"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from asterun import __version__
from asterun.backends.base import BackendCall, capability_rows
from asterun.backends.codex_output import MessageOutput
from asterun.config import BackendConfig
from asterun.contracts import ConnectionDisplay, RunStatus
from asterun.errors import AUTH_REQUIRED, BACKEND_UNAVAILABLE, AsterunError
from asterun.ids import RunId, TaskId
from asterun.transport import JsonRpcError, LineJsonRpcTransport

CodexAppServerError = JsonRpcError
LIVE_ENV = "ASTERUN_RUN_CODEX"
# 产品代码不再默认指向 macOS App 路径；集成测试可自行设置 CODEX_BIN。
DEFAULT_CODEX_BIN = None

CODEX_CLIENT_METHODS = (
    "initialize",
    "thread/start",
    "thread/resume",
    "thread/fork",
    "thread/archive",
    "thread/unsubscribe",
    "turn/start",
    "turn/steer",
    "turn/interrupt",
    "model/list",
    "account/read",
    "account/login/start",
    "account/logout",
    "thread/list",
    "thread/read",
    "thread/turns/list",
    "command/exec",
)


def live_codex_enabled() -> bool:
    return os.environ.get(LIVE_ENV) == "1"


def discover_codex_bin(explicit: str | None = None) -> Path | None:
    # 明确选择失效时不能悄悄改用另一安装或账户上下文。
    if explicit is not None:
        path = Path(explicit)
        return path if explicit and path.is_file() and os.access(path, os.X_OK) else None
    env = os.environ.get("CODEX_BIN")
    if env is not None:
        path = Path(env)
        return path if env and path.is_file() and os.access(path, os.X_OK) else None
    which = shutil.which("codex")
    return Path(which) if which else None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class CodexPromptResult:
    status: str
    thread_id: str
    turn_id: str
    message: str = ""
    thread: dict[str, Any] = field(default_factory=dict)
    turn: dict[str, Any] = field(default_factory=dict)
    method_counts: dict[str, int] = field(default_factory=dict)
    token_usage: dict[str, Any] = field(default_factory=dict)
    pending_requests: list[dict[str, Any]] = field(default_factory=list)
    recent_events: list[dict[str, Any]] = field(default_factory=list)
    item_completed_count: int = 0
    elapsed_ms: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        result = {
            "status": self.status,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "message": self.message,
            "method_counts": self.method_counts,
            "token_usage": self.token_usage,
            "pending_requests": self.pending_requests,
            "recent_events": self.recent_events,
            "item_completed_count": self.item_completed_count,
            "elapsed_ms": self.elapsed_ms,
        }
        if self.error:
            result["error"] = self.error
        return result


@dataclass
class CodexSession:
    session_id: str
    transport: CodexAppServerTransport
    cwd: str
    model: str | None
    started_at: str
    thread: dict[str, Any] = field(default_factory=dict)
    status: str = "idle"
    current_turn_id: str = ""
    message_chunks: list[str] = field(default_factory=list)
    message_output: MessageOutput = field(default_factory=MessageOutput)
    turns: list[dict[str, Any]] = field(default_factory=list)
    method_counts: dict[str, int] = field(default_factory=dict)
    token_usage: dict[str, Any] = field(default_factory=dict)
    pending_requests: dict[str, dict[str, Any]] = field(default_factory=dict)
    recent_events: list[dict[str, Any]] = field(default_factory=list)
    item_completed_count: int = 0
    last_error: str = ""
    _turn_complete: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def message_text(self) -> str:
        progress = "".join(self.message_chunks)
        return self.message_output.text(progress) if self.status in {"completed", "failed", "interrupted"} else progress

    def handle_message(self, msg: dict[str, Any]) -> None:
        method = msg.get("method", "<response>")
        params = msg.get("params", {})
        if "reasoning" in method.lower() or params.get("threadId", self.session_id) != self.session_id:
            return
        event_turn = params.get("turnId") or (params.get("turn") or {}).get("id")
        if event_turn and self.current_turn_id and event_turn != self.current_turn_id:
            return
        with self._lock:
            self.message_output.observe(method, params)
            self._append_event_locked(msg)
            self.method_counts[method] = self.method_counts.get(method, 0) + 1
            if "id" in msg and msg.get("method"):
                request_id = str(msg.get("id"))
                self.pending_requests[request_id] = {
                    "id": msg.get("id"),
                    "method": msg.get("method"),
                    "params": params,
                }
                self.status = "waiting_input"
                return
            if method == "turn/started":
                self.status = "running"
                self.current_turn_id = event_turn or self.current_turn_id
            elif method == "item/agentMessage/delta":
                self.message_chunks.append(params.get("delta", ""))
            elif method == "thread/tokenUsage/updated":
                self.token_usage = params
            elif method == "item/completed":
                self.item_completed_count += 1
            elif method == "turn/completed":
                self.current_turn_id = event_turn or self.current_turn_id
                self.status = (params.get("turn") or {}).get("status", "unknown")
                self.pending_requests.clear()
                self.turns.append(params)
                self._turn_complete.set()
            elif method == "serverRequest/resolved":
                self.pending_requests.pop(str(params.get("requestId")), None)
            elif method == "error":
                if not params.get("willRetry"):
                    self.status = "unknown"
                    self.last_error = "原生连接报告错误，等待确定轮次终态"
                    self._turn_complete.set()

    def _append_event_locked(self, msg: dict[str, Any]) -> None:
        event = {
            "method": msg.get("method", "<response>"),
            "params": _truncate_jsonish(msg.get("params", {})),
        }
        if "id" in msg:
            event["id"] = msg.get("id")
        self.recent_events.append(event)
        if len(self.recent_events) > 200:
            del self.recent_events[: len(self.recent_events) - 200]

    def reset_for_turn(self) -> None:
        with self._lock:
            self.status = "running"
            self.current_turn_id = ""
            self.message_chunks = []
            self.message_output = MessageOutput()
            self.pending_requests = {}
            self.item_completed_count = 0
            self.last_error = ""
            self._turn_complete = threading.Event()

    def to_status_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "session_id": self.session_id,
                "thread_id": self.session_id,
                "current_turn_id": self.current_turn_id,
                "status": self.status,
                "cwd": self.cwd,
                "model": self.model,
                "started_at": self.started_at,
                "message": self.message_text,
                "message_preview": self.message_text[:500],
                "method_counts": dict(self.method_counts),
                "token_usage": dict(self.token_usage),
                "pending_requests": list(self.pending_requests.values()),
                "recent_events": list(self.recent_events),
                "item_completed_count": self.item_completed_count,
                "last_error": self.last_error,
                "transport_alive": self.transport.alive,
            }


def _truncate_jsonish(value: Any, *, limit: int = 8000) -> Any:
    if isinstance(value, str):
        return value[:limit] + "...<truncated>" if len(value) > limit else value
    if isinstance(value, list):
        return [_truncate_jsonish(item, limit=limit) for item in value[:50]]
    if isinstance(value, dict):
        return {str(key): _truncate_jsonish(item, limit=limit) for key, item in list(value.items())[:80]}
    return value


def build_codex_app_server_command(codex_bin: str, *, listen: str = "stdio://",
                                   config_overrides: dict[str, Any] | None = None) -> list[str]:
    command = [codex_bin, "app-server", "--listen", listen]
    if config_overrides is not None:
        command.append("--strict-config")
        for key, value in sorted(config_overrides.items()):
            # Only scalar TOML values are used by the pinned read-scope profile.
            if not isinstance(value, (str, bool, int)):
                raise ValueError("App Server startup overrides must be scalar")
            command.extend(["-c", f"{key}={json.dumps(value)}"])
    return command


def build_initialize_params(
    *,
    client_name: str = "asterun",
    client_version: str = __version__,
    experimental_api: bool = True,
) -> dict[str, Any]:
    return {
        "clientInfo": {"name": client_name, "version": client_version},
        "capabilities": {"experimentalApi": experimental_api},
    }


def build_thread_start_params(
    *,
    cwd: str | None = None,
    model: str | None = None,
    service_name: str | None = None,
    service_tier: str | None = None,
    experimental_raw_events: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {"experimentalRawEvents": experimental_raw_events}
    if cwd is not None:
        params["cwd"] = cwd
    if model is not None:
        params["model"] = model
    if service_name is not None:
        params["serviceName"] = service_name
    if service_tier is not None:
        params["serviceTier"] = service_tier
    if extra:
        params.update(extra)
    return params


def build_text_user_input(text: str) -> dict[str, str]:
    return {"type": "text", "text": text}


def build_image_user_input(url: str, detail: str | None = None) -> dict[str, str]:
    item = {"type": "image", "url": url}
    if detail is not None:
        item["detail"] = detail
    return item


def build_local_image_user_input(path: str, detail: str | None = None) -> dict[str, str]:
    item = {"type": "localImage", "path": path}
    if detail is not None:
        item["detail"] = detail
    return item


def build_turn_start_params(
    *,
    thread_id: str,
    text: str | None = None,
    input_items: list[dict[str, Any]] | None = None,
    cwd: str | None = None,
    model: str | None = None,
    service_tier: str | None = None,
    output_schema: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "threadId": thread_id,
        "input": input_items if input_items is not None else [build_text_user_input(text or "")],
    }
    if cwd is not None:
        params["cwd"] = cwd
    if model is not None:
        params["model"] = model
    if service_tier is not None:
        params["serviceTier"] = service_tier
    if output_schema is not None:
        params["outputSchema"] = output_schema
    if extra:
        params.update(extra)
    return params


def build_turn_steer_params(
    *,
    thread_id: str,
    expected_turn_id: str,
    text: str | None = None,
    input_items: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "threadId": thread_id,
        "expectedTurnId": expected_turn_id,
        "input": input_items if input_items is not None else [build_text_user_input(text or "")],
    }
    if extra:
        params.update(extra)
    return params


def extract_thread_id(response: dict[str, Any]) -> str:
    thread = response.get("thread")
    if isinstance(thread, dict) and isinstance(thread.get("id"), str):
        return thread["id"]
    if isinstance(response.get("threadId"), str):
        return response["threadId"]
    if isinstance(response.get("id"), str):
        return response["id"]
    found = _find_string_values(response, {"threadId", "id"})
    for value in found:
        if value.startswith("019") or len(value) > 8:
            return value
    return ""


def extract_turn_id(response: dict[str, Any]) -> str:
    turn = response.get("turn")
    if isinstance(turn, dict) and isinstance(turn.get("id"), str):
        return turn["id"]
    if isinstance(response.get("turnId"), str):
        return response["turnId"]
    if isinstance(response.get("id"), str):
        return response["id"]
    return ""


def _find_string_values(obj: Any, keys: set[str]) -> list[str]:
    found: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in keys and isinstance(value, str):
                found.append(value)
            found.extend(_find_string_values(value, keys))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_find_string_values(item, keys))
    return found


class CodexAppServerTransport:
    def __init__(
        self,
        *,
        codex_bin: str | None = None,
        client_name: str = "asterun",
        client_version: str = __version__,
        experimental_api: bool = True,
        config_overrides: dict[str, Any] | None = None,
    ):
        self.codex_bin = codex_bin
        self.client_name = client_name
        self.client_version = client_version
        self.experimental_api = experimental_api
        self.config_overrides = config_overrides
        self._transport = LineJsonRpcTransport(include_jsonrpc=True)
        self.initialize_result: dict[str, Any] | None = None

    def spawn(self, cwd: str | Path) -> None:
        if not self.codex_bin:
            raise RuntimeError("未配置 Codex 可执行文件")
        child_env = os.environ.copy()
        self.native_home = Path(child_env.get("CODEX_HOME") or
            str(Path(child_env.get("HOME") or str(Path.home())) / ".codex")).resolve()
        self._transport.spawn(
            build_codex_app_server_command(self.codex_bin, config_overrides=self.config_overrides),
            cwd=cwd,
            env=child_env,
        )
        time.sleep(0.2)
        if self._transport.proc and self._transport.proc.poll() is not None:
            err = self._transport.proc.stderr.read() if self._transport.proc.stderr else ""
            raise RuntimeError(f"codex app-server exited immediately: {err[:500]}")

    def initialize(self, timeout: float = 20.0) -> dict[str, Any]:
        result = self.request(
            "initialize",
            build_initialize_params(
                client_name=self.client_name,
                client_version=self.client_version,
                experimental_api=self.experimental_api,
            ),
            timeout=timeout,
        )
        self.initialize_result = result
        self.notify("initialized", {})
        return result

    def list_models(self, timeout: float = 30.0) -> dict[str, Any]:
        return self.request("model/list", {}, timeout=timeout)

    def read_account(self, timeout: float = 20.0) -> dict[str, Any]:
        return self.request("account/read", {}, timeout=timeout)

    def start_thread(self, params: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
        return self.request("thread/start", params, timeout=timeout)

    def start_turn(self, params: dict[str, Any], timeout: float = 900.0) -> dict[str, Any]:
        return self.request("turn/start", params, timeout=timeout)

    def steer_turn(self, params: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
        return self.request("turn/steer", params, timeout=timeout)

    def interrupt_turn(self, thread_id: str, turn_id: str, timeout: float = 10.0) -> dict[str, Any]:
        return self.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=timeout)

    def create_session(
        self,
        *,
        cwd: str | Path,
        model: str | None = None,
        service_tier: str | None = None,
        thread_params: dict[str, Any] | None = None,
        timeout: float = 60.0,
    ) -> CodexSession:
        thread = self.start_thread(
            build_thread_start_params(
                cwd=str(cwd),
                model=model,
                service_tier=service_tier,
                extra=thread_params,
            ),
            timeout=timeout,
        )
        thread_id = extract_thread_id(thread)
        if not thread_id:
            raise RuntimeError("thread/start response did not contain a thread id")
        session = CodexSession(
            session_id=thread_id,
            transport=self,
            cwd=str(cwd),
            model=model,
            started_at=_now_iso(),
            thread=thread,
        )
        self.on_message(session.handle_message)
        return session

    def resume_session(
        self,
        *,
        thread_id: str,
        cwd: str | Path,
        model: str | None = None,
        service_tier: str | None = None,
        resume_params: dict[str, Any] | None = None,
        timeout: float = 60.0,
    ) -> CodexSession:
        params: dict[str, Any] = {"threadId": thread_id, "cwd": str(cwd)}
        if model is not None:
            params["model"] = model
        if service_tier is not None:
            params["serviceTier"] = service_tier
        if resume_params:
            params.update(resume_params)
        thread = self.request("thread/resume", params, timeout=timeout)
        resumed_thread_id = extract_thread_id(thread) or thread_id
        session = CodexSession(
            session_id=resumed_thread_id,
            transport=self,
            cwd=str(cwd),
            model=model,
            started_at=_now_iso(),
            thread=thread,
        )
        self.on_message(session.handle_message)
        return session

    def start_session_turn(
        self,
        session: CodexSession,
        *,
        text: str | None = None,
        input_items: list[dict[str, Any]] | None = None,
        timeout: float = 60.0,
        output_schema: dict[str, Any] | None = None,
        turn_params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session.reset_for_turn()
        turn = self.start_turn(
            build_turn_start_params(
                thread_id=session.session_id,
                text=text,
                input_items=input_items,
                cwd=session.cwd,
                model=session.model,
                output_schema=output_schema,
                extra=turn_params,
            ),
            timeout=timeout,
        )
        turn_id = extract_turn_id(turn)
        session.current_turn_id = turn_id or session.current_turn_id
        return turn

    def run_text_prompt(
        self,
        *,
        text: str,
        cwd: str | Path,
        model: str | None = None,
        service_tier: str | None = None,
        timeout: float = 300.0,
    ) -> CodexPromptResult:
        started = time.time()
        done = threading.Event()
        needs_input = threading.Event()
        messages: list[dict[str, Any]] = []
        chunks: list[str] = []
        from asterun.backends.codex_output import MessageOutput
        output = MessageOutput()
        pending_requests: list[dict[str, Any]] = []
        token_usage: dict[str, Any] = {}
        item_completed_count = 0
        completed_turn: dict[str, Any] = {}

        def on_msg(msg: dict[str, Any]) -> None:
            nonlocal item_completed_count, token_usage, completed_turn
            messages.append(msg)
            method = msg.get("method", "")
            params = msg.get("params", {})
            output.observe(method, params)
            if "id" in msg and method:
                pending_requests.append({"id": msg.get("id"), "method": method, "params": params})
                needs_input.set()
            if method == "item/agentMessage/delta":
                chunks.append(params.get("delta", ""))
            elif method == "thread/tokenUsage/updated":
                token_usage = params
            elif method == "item/completed":
                item_completed_count += 1
            elif method == "turn/completed":
                completed_turn = params.get("turn") or {}
                done.set()

        self.on_message(on_msg)
        thread = self.start_thread(build_thread_start_params(cwd=str(cwd), model=model, service_tier=service_tier))
        thread_id = extract_thread_id(thread)
        if not thread_id:
            return CodexPromptResult(
                status="error",
                thread_id="",
                turn_id="",
                thread=thread,
                elapsed_ms=round((time.time() - started) * 1000),
                error="thread/start response did not contain a thread id",
            )
        turn = self.start_turn(
            build_turn_start_params(thread_id=thread_id, text=text, cwd=str(cwd), model=model, service_tier=service_tier),
            timeout=min(timeout, 60.0),
        )
        turn_id = extract_turn_id(turn)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if done.is_set():
                status = completed_turn.get("status", "unknown")
                break
            if needs_input.is_set():
                status = "waiting_input"
                break
            time.sleep(0.05)
        else:
            status = "timeout"
        method_counts: dict[str, int] = {}
        for msg in messages:
            method = msg.get("method", "<response>")
            method_counts[method] = method_counts.get(method, 0) + 1
        return CodexPromptResult(
            status=status,
            thread_id=thread_id,
            turn_id=turn_id,
            message=output.text("".join(chunks)).strip(),
            thread=thread,
            turn={"turn": completed_turn} if completed_turn else turn,
            method_counts=method_counts,
            token_usage=token_usage,
            pending_requests=pending_requests,
            recent_events=[
                {
                    "method": msg.get("method", "<response>"),
                    "id": msg.get("id"),
                    "params": _truncate_jsonish(msg.get("params", {})),
                }
                for msg in messages[-50:]
            ],
            item_completed_count=item_completed_count,
            elapsed_ms=round((time.time() - started) * 1000),
            error="" if status == "completed" else str(completed_turn.get("error") or status),
        )

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
        return self._transport.request(method, params, timeout)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._transport.notify(method, params)

    def respond(self, request_id: int | str, result: dict[str, Any]) -> None:
        self._transport.respond(request_id, result)

    def on_request(self, cb) -> None:
        self._transport.on_request(cb)

    def on_notification(self, cb) -> None:
        self._transport.on_notification(cb)

    def on_message(self, cb) -> None:
        self._transport.on_message(cb)

    def close(self) -> None:
        self._transport.close()

    @property
    def alive(self) -> bool:
        return self._transport.alive


class CodexBackend:
    def __init__(self, config: BackendConfig) -> None:
        self.config = config
        self.calls: list[BackendCall] = []
        from asterun.backends.codex_runtime import CodexRuntime

        self.runtime = CodexRuntime(self)

    def dispatch_stream(self, *args, **kwargs):
        return self.runtime.run(*args, **kwargs)

    def resume_native(self, native, cwd, *, read_scope=None):
        return self.runtime.resume(native, cwd, read_scope=read_scope)

    def reconcile_native(self, native, cwd):
        return self.runtime.reconcile(native, cwd)

    def _present_project(self, transport, cwd, thread_id):
        """可选客户端登记；错误只属于展示，不进入执行状态机。"""
        if self.config.desktop_projects is not True:
            return None
        try:
            from asterun.backends.codex_desktop import CodexDesktop
            from asterun.backends.codex_projects import CodexProjects

            desktop = CodexDesktop(transport.codex_bin, transport.native_home)
            report = CodexProjects(transport, cwd, native_home=transport.native_home,
                                   prepare_project=desktop.register_project).present(thread_id)
            if report.get("association") in {"existing", "associated"}:
                report["desktop"]["thread_open"] = desktop.open_thread(thread_id)
            return report
        except Exception:
            # 不传播原生异常原文，其中可能包含配置、路径之外的私有字段。
            return {"registration": "unknown", "association": "unknown",
                    "client_visibility": "not_verified", "reason": "presentation_failed",
                    "thread_id": thread_id, "native_cwd": str(cwd)}

    def present_native(self, native, cwd):
        from asterun.errors import CAPABILITY_UNSUPPORTED, REMOTE_STATE_UNKNOWN

        if self.config.desktop_projects is not True:
            raise AsterunError(CAPABILITY_UNSUPPORTED, "该 Codex 后端未启用 desktop_projects")
        thread_id = native.get("backend_session_id") or native.get("thread_id")
        if not thread_id:
            raise AsterunError(REMOTE_STATE_UNKNOWN, "缺少已持久化的原生线程引用，不能登记替代会话")
        # 只连接并读取/登记项目；不 resume，不调用任何线程或轮次创建入口。
        try:
            transport = self.runtime._connect(cwd)
        except AsterunError:
            raise
        except Exception:
            return {"registration": "unknown", "association": "unknown",
                    "client_visibility": "not_verified", "reason": "presentation_connection_failed",
                    "thread_id": thread_id, "native_cwd": str(cwd)}
        try:
            return self._present_project(transport, cwd, thread_id)
        finally:
            transport.close()

    def close(self):
        self.runtime.close()

    @property
    def dispatch_count(self) -> int:
        return sum(1 for item in self.calls if item.method == "dispatch")

    def discover_bin(self) -> Path | None:
        return discover_codex_bin(self.config.bin)

    def can_dispatch(self) -> bool:
        return bool(self.config.enabled and live_codex_enabled() and self.discover_bin())

    def unavailable_error(self) -> AsterunError:
        binary = self.discover_bin()
        if binary is None:
            return AsterunError(
                BACKEND_UNAVAILABLE,
                "未发现 Codex 可执行文件",
                next_action="先修正显式 bin 或 CODEX_BIN（无效时不会回退）；未显式选择时可用 PATH。用 codex-discover 查看应用包候选后固定路径",
                details={"is_real_connection": False, "live_flag": live_codex_enabled()},
            )
        if not live_codex_enabled():
            return AsterunError(
                AUTH_REQUIRED,
                "Codex 真实派发默认关闭",
                next_action=f"确认账户可用后设置 {LIVE_ENV}=1；网页登录不能单独证明 App Server 可用",
                details={"binary": str(binary), "is_real_connection": False},
            )
        return AsterunError(BACKEND_UNAVAILABLE, "Codex 当前不可派发")

    def inspect(self) -> dict[str, Any]:
        self.calls.append(BackendCall("inspect"))
        binary = self.discover_bin()
        return {
            "backend": self.config.name,
            "kind": "codex",
            "enabled_in_config": self.config.enabled,
            "execution_enabled": self.can_dispatch(),
            "is_real_connection": False,
            "connection_display": str(ConnectionDisplay.NONE),
            "authentication": "not_tested",
            "binary": None if binary is None else str(binary),
            "binary_found": binary is not None,
            "live_flag": live_codex_enabled(),
            "desktop_projects": {"enabled": self.config.desktop_projects is True,
                                 "registration": "not_checked", "client_visibility": "not_verified"},
            "capabilities": [item.to_dict() for item in capability_rows("codex")],
            "methods": list(CODEX_CLIENT_METHODS),
            "message": "已迁入 App Server 协议适配；inspect 不启动进程。现场登录与真实任务仍未验证。",
        }

    def verify_connection(self) -> dict[str, Any]:
        self.calls.append(BackendCall("verify_connection"))
        binary = self.discover_bin()
        report = {
            "backend": self.config.name,
            "kind": "codex",
            "is_real_connection": False,
            "connection_display": str(ConnectionDisplay.NONE),
            "authentication": "not_tested",
            "binary_found": binary is not None,
            "binary": None if binary is None else str(binary),
            "live_flag": live_codex_enabled(),
            "simulated": False,
            "verified": False,
            "message": "默认只做安装发现，不调用 account/read，也不消耗额度。",
        }
        if not live_codex_enabled():
            return report
        if binary is None:
            raise self.unavailable_error()
        transport = CodexAppServerTransport(codex_bin=str(binary))
        try:
            transport.spawn(Path.cwd())
            init = transport.initialize()
            account = transport.read_account()
            if not isinstance(account, dict) or not isinstance(account.get("account"), dict) or not account["account"]:
                raise AsterunError(AUTH_REQUIRED, "App Server 尚未提供有效账户状态")
        except Exception as exc:
            raise AsterunError(
                AUTH_REQUIRED,
                "Codex App Server 未确认有效账户状态",
                next_action="在执行环境完成官方登录后重试；网页会话不能代替 App Server 认证",
                details={"is_real_connection": False},
            ) from exc
        finally:
            transport.close()
        report.update(
            {
                "is_real_connection": True,
                "connection_display": str(ConnectionDisplay.REAL),
                "authentication": "adapter_checked",
                "verified": True,
                "initialize": {"keys": sorted(init)},
                "account_keys": sorted(account) if isinstance(account, dict) else [],
                "message": "已做一次 App Server initialize/account.read；Desktop 可见性仍未验证。",
            }
        )
        return report

    def dispatch(self, task_id: TaskId, run_id: RunId, script: str, text: str, *, cwd: Path | None = None) -> dict[str, Any]:
        self.calls.append(
            BackendCall("dispatch", {"task_id": task_id.value, "run_id": run_id.value, "script": script, "text": text})
        )
        if not self.can_dispatch():
            raise self.unavailable_error()
        from asterun.backends.base import require_dispatch_cwd

        cwd = require_dispatch_cwd(cwd)
        binary = self.discover_bin()
        assert binary is not None
        transport = CodexAppServerTransport(codex_bin=str(binary))
        try:
            transport.spawn(cwd)
            transport.initialize()
            result = transport.run_text_prompt(text=text or "Reply with ASTERUN_CODEX_OK.", cwd=cwd)
            native = result.to_dict()
        finally:
            transport.close()
        events = [{"type": "run_started", "text": "codex app-server"}]
        for item in result.recent_events:
            events.append({"type": "message", "text": str(item.get("method"))})
        if result.status == "waiting_input":
            return {
                "status": str(RunStatus.WAITING_INPUT),
                "terminated": False,
                "needs_approval": True,
                "events": events,
                "summary": "Codex 等待输入或审批",
                "native": native,
            }
        if result.status == "completed":
            return {
                "status": str(RunStatus.SUCCEEDED),
                "terminated": True,
                "events": [*events, {"type": "run_succeeded", "text": result.message}],
                "summary": result.message,
                "native": native,
            }
        if result.status not in {"failed", "interrupted"}:
            return {"status": str(RunStatus.PENDING_RECONCILE), "terminated": False,
                    "error_code": "REMOTE_STATE_UNKNOWN", "events": events,
                    "summary": "Codex 未返回确定终态，需要对账", "native": native}
        return {
            "status": str(RunStatus.CANCELLED if result.status == "interrupted" else RunStatus.FAILED),
            "terminated": True,
            "error_code": None if result.status == "interrupted" else "CODEX_RUN_FAILED",
            "events": [*events, {"type": "run_terminated" if result.status == "interrupted" else "run_failed",
                                  "text": result.error or result.status}],
            "summary": result.error or result.status,
            "native": native,
        }

    def request_cancel(self, run_id: RunId, script: str) -> dict[str, Any]:
        self.calls.append(BackendCall("request_cancel", {"run_id": run_id.value, "script": script}))
        return self.runtime.cancel(run_id)

    def respond_approval(self, run_id: RunId, decision: str, *, native_request=None) -> dict[str, Any]:
        self.calls.append(BackendCall("respond_approval", {"run_id": run_id.value, "decision": decision}))
        return self.runtime.respond(run_id, decision, native_request or {})
