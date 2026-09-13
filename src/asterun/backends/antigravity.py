"""官方 agy headless 后端；首版仅接入无扩展工作区的受限读取。"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
from threading import Event, Lock
from typing import Any

from asterun.backends.base import BackendCall, capability_rows, require_dispatch_cwd
from asterun.config import BackendConfig
from asterun.errors import (
    AUTH_REQUIRED, BACKEND_UNAVAILABLE, CAPABILITY_UNSUPPORTED, CONFIG_REQUIRED,
    INVALID_CONFIG, AsterunError,
)
from asterun.ids import RunId, TaskId

LIVE_ENV = "ASTERUN_RUN_ANTIGRAVITY"
BIN_ENV = "ANTIGRAVITY_BIN"
DISPATCH_RECEIPT_KEY = "asterun.antigravity_dispatch_v1"
PRE_PROMPT_STAGES = frozenset({"preflight", "spawn"})


def live_antigravity_enabled() -> bool:
    return os.environ.get(LIVE_ENV) == "1"


def discover_antigravity_bin(explicit: str | None = None) -> Path | None:
    def executable(value: str) -> Path | None:
        if not value or "\x00" in value:
            return None
        path = Path(value).expanduser()
        return path.resolve() if path.is_file() and os.access(path, os.X_OK) else None

    if explicit is not None:
        return executable(explicit)
    if BIN_ENV in os.environ:
        return executable(os.environ[BIN_ENV])
    found = shutil.which("agy")
    if found:
        return executable(found)
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        return executable(str(Path(local) / "agy/bin/agy.exe")) if local else None
    return executable(str(Path.home() / ".local/bin/agy"))


def build_command(binary: Path, model: str) -> list[str]:
    if not model or model != model.strip() or "\x00" in model or model.startswith("-"):
        raise AsterunError(INVALID_CONFIG, "Antigravity 必须配置明确、有效的 model ID")
    return [str(binary), "--input-format", "stream-json", "--output-format", "stream-json",
            "--model", model, "--disable-slash-commands", "--print-timeout", "5m"]


class AntigravityBackend:
    def __init__(self, config: BackendConfig) -> None:
        self.config = config
        self.calls: list[BackendCall] = []
        self._runtime = None
        self._runtime_lock = Lock()

    @property
    def runtime(self):
        with self._runtime_lock:
            if self._runtime is None:
                from asterun.backends.antigravity_runtime import AntigravityRuntime

                self._runtime = AntigravityRuntime()
            return self._runtime

    @property
    def dispatch_count(self) -> int:
        return sum(call.method == "dispatch" for call in self.calls)

    def discover_bin(self) -> Path | None:
        return discover_antigravity_bin(self.config.bin)

    def _prepared_home(self) -> Path:
        from asterun.antigravity_profile import validate_home

        if not self.config.home:
            raise AsterunError(CONFIG_REQUIRED, "Antigravity 需要显式的专用 HOME",
                               next_action="使用 asterun-antigravity prepare --home 初始化新目录")
        return validate_home(self.config.home)

    def _preflight(self) -> tuple[Path, Path, str]:
        if not self.config.enabled:
            raise AsterunError(BACKEND_UNAVAILABLE, "Antigravity 后端配置未启用")
        binary = self.discover_bin()
        if binary is None:
            raise AsterunError(BACKEND_UNAVAILABLE, "未发现官方 agy 可执行入口",
                               next_action="配置 bin 或 ANTIGRAVITY_BIN；也可安装 agy 到标准目录")
        if not live_antigravity_enabled():
            raise AsterunError(AUTH_REQUIRED, "Antigravity 真实派发门闩未开启",
                               next_action=f"核对账户与受限配置后设置 {LIVE_ENV}=1")
        home = self._prepared_home()
        if not self.config.model:
            raise AsterunError(CONFIG_REQUIRED, "Antigravity 需要显式固定 model ID")
        build_command(binary, self.config.model)
        return binary, home, self.config.model

    def can_dispatch(self) -> bool:
        try:
            self._preflight()
            return True
        except AsterunError:
            return False

    def unavailable_error(self) -> AsterunError:
        try:
            self._preflight()
        except AsterunError as exc:
            return exc
        return AsterunError(BACKEND_UNAVAILABLE, "Antigravity 当前不可派发")

    def inspect(self) -> dict[str, Any]:
        self.calls.append(BackendCall("inspect"))
        binary = self.discover_bin()
        return {
            "backend": self.config.name, "kind": "antigravity",
            "enabled_in_config": self.config.enabled, "execution_enabled": self.can_dispatch(),
            "binary_found": binary is not None, "binary": str(binary) if binary else None,
            "live_flag": live_antigravity_enabled(), "home": self.config.home,
            "model": self.config.model, "execution_profile": "workspace-read-v1",
            "wire_protocol": "agy-stream-json", "authentication": "not_tested",
            "authentication_mode": "native-account", "is_real_connection": False,
            "connection_display": "none", "native_resume_verified": False,
            "desktop_visibility": "not_tested", "os_isolation_verified": False,
            "capabilities": [row.to_dict() for row in capability_rows("antigravity")],
            "message": "仅发现与配置检查；工作区读取受原生策略约束，未宣称 OS 沙箱或账户验证通过。",
        }

    def verify_connection(self) -> dict[str, Any]:
        self.calls.append(BackendCall("verify_connection"))
        return {**self.inspect(), "verified": False, "simulated": False,
                "message": "agy 未提供本适配器可用的无模型认证握手；安装发现不等于真实连接。"}

    def dispatch(self, task_id: TaskId, run_id: RunId, script: str, text: str,
                 *, cwd: Path | None = None) -> dict[str, Any]:
        return self.dispatch_stream(task_id, run_id, script, text, cwd=cwd, native={},
                                    publish=lambda result: None, stopping=Event())

    def dispatch_stream(self, task_id: TaskId, run_id: RunId, script: str, text: str,
                        *, cwd: Path | None, native: dict, publish, stopping: Event) -> dict[str, Any]:
        self.calls.append(BackendCall("dispatch", {"task_id": task_id.value, "run_id": run_id.value}))
        try:
            from asterun.antigravity_profile import build_environment, validate_workspace, validate_workspace_binding

            binary, home, model = self._preflight()
            workspace = validate_workspace(require_dispatch_cwd(cwd))
            validate_workspace_binding(home, workspace)
            if native.get("backend_session_id"):
                raise AsterunError(CAPABILITY_UNSUPPORTED,
                                   "首版缺少原生只读续接核对接口；请显式创建新会话")
            if not isinstance(text, str) or not text.strip() or "\x00" in text:
                raise AsterunError(INVALID_CONFIG, "Antigravity prompt 必须是非空文本")
            # CLI 的 cwd 回执不等于原生项目已包含该目录；显式绑定读取根。
            command = [*build_command(binary, model), "--add-dir", str(workspace)]
            env = build_environment(home)
        except AsterunError as exc:
            return {"status": "failed", "terminated": True, "error_code": exc.code,
                    "summary": exc.message, "native": {DISPATCH_RECEIPT_KEY: {
                        "task_id": task_id.value, "run_id": run_id.value, "stage": "preflight",
                        "prompt_attempted": False, "delivery": "not_sent", "failure_type": type(exc).__name__,
                    }}}

        def bind_receipt(result):
            receipt = result.get("native", {}).get(DISPATCH_RECEIPT_KEY)
            if isinstance(receipt, dict):
                receipt["task_id"] = task_id.value
            return result

        result = self.runtime.run(run_id, text, command=command, env=env, cwd=workspace,
                                  expected_model=model,
                                  publish=lambda item: publish(bind_receipt(item)), stopping=stopping)
        return bind_receipt(result)

    def request_cancel(self, run_id: RunId, script: str) -> dict[str, Any]:
        self.calls.append(BackendCall("request_cancel", {"run_id": run_id.value}))
        return self.runtime.cancel(run_id)

    def respond_approval(self, run_id: RunId, decision: str, *, native_request=None) -> dict[str, Any]:
        raise AsterunError(CAPABILITY_UNSUPPORTED, "agy headless 不支持机器审批消息；不能转发此决定")

    def close(self) -> None:
        if self._runtime is not None:
            self._runtime.close()
