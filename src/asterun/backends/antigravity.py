"""官方 agy headless 后端；首版仅接入无扩展工作区的受限读取。"""
from __future__ import annotations

import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
from threading import Event, Lock
import time
from typing import Any

from asterun.backends.antigravity_runtime import READ_ONLY_PREAMBLE
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


MIN_CLI_VERSION = (1, 2, 0)
# Flags that build_command and the plugin worker depend on. A CLI release is
# accepted when it is at least MIN_CLI_VERSION and advertises all of them.
REQUIRED_CLI_FLAGS = (
    "--input-format", "--output-format", "--model", "--disable-slash-commands",
    "--print-timeout", "--add-dir",
)
_CLI_VERSION = re.compile(
    r"(?:(?:Antigravity(?: CLI)?|agy)\s+)?v?([0-9]{1,4})\.([0-9]{1,4})\.([0-9]{1,4})(?:[-+][0-9A-Za-z.-]{1,64})?",
    re.IGNORECASE,
)
_PROBE_LIMIT = 65536
_PROBE_TIMEOUT = 15
_PROBE_CACHE: dict[tuple[object, ...], dict[str, object]] = {}
_PROBE_LOCK = Lock()


def parse_cli_version(text: str) -> tuple[int, int, int] | None:
    match = _CLI_VERSION.fullmatch(text.strip())
    return None if match is None else (int(match[1]), int(match[2]), int(match[3]))


def _probe_output(binary: Path, argument: str, scratch: str) -> tuple[int, str]:
    """Run one probe argument with streamed, capped reads and group cleanup."""
    env = {"HOME": scratch, "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "LANG": "C.UTF-8"}
    try:
        proc = subprocess.Popen([str(binary), argument], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env, cwd=scratch, start_new_session=True)
    except OSError as exc:
        raise AsterunError(BACKEND_UNAVAILABLE, f"无法运行 agy {argument}：{type(exc).__name__}",
                           next_action="确认 bin 指向可执行的官方 agy CLI") from exc
    buffers = {proc.stdout: bytearray(), proc.stderr: bytearray()}
    deadline = time.monotonic() + _PROBE_TIMEOUT
    total = 0
    failure = None
    code = None
    try:
        with selectors.DefaultSelector() as selector:
            for stream in buffers:
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map() and failure is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failure = "超时"
                    break
                for key, _ in selector.select(min(remaining, 0.1)):
                    data = os.read(key.fileobj.fileno(), 4096)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(data)
                    if total > _PROBE_LIMIT:
                        failure = "输出超出检查上限"
                        break
                    buffers[key.fileobj].extend(data)
        if failure is None:
            try:
                code = proc.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                failure = "超时"
    finally:
        # The probe owns its session: remove the CLI and anything it spawned.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        for stream in buffers:
            stream.close()
    if failure is not None:
        raise AsterunError(BACKEND_UNAVAILABLE, f"agy {argument} {failure}",
                           next_action="确认 bin 指向官方 agy CLI")
    text = bytes(buffers[proc.stdout]).decode("utf-8", "replace")
    if argument == "--help":
        # Go's flag package prints usage on stderr.
        text += "\n" + bytes(buffers[proc.stderr]).decode("utf-8", "replace")
    return code, text


def _binary_identity(binary: Path) -> tuple[int, int, int, int] | None:
    try:
        info = binary.stat()
    except OSError:
        return None
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def probe_cli(binary: Path) -> dict[str, object]:
    """Detect the agy version and required features in a throwaway HOME.

    The real profile is never used here, so probing cannot rewrite native
    state. Results are cached per binary identity (path, inode, size, mtime).
    """
    try:
        info = binary.stat()
    except OSError as exc:
        raise AsterunError(BACKEND_UNAVAILABLE, f"无法读取 agy 可执行文件：{type(exc).__name__}") from exc
    key = (str(binary), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    with _PROBE_LOCK:
        cached = _PROBE_CACHE.get(key)
    if cached is not None:
        return dict(cached)
    with tempfile.TemporaryDirectory(prefix="asterun-agy-probe-") as scratch:
        code, version_text = _probe_output(binary, "--version", scratch)
        version = parse_cli_version(version_text) if code == 0 else None
        if version is None:
            raise AsterunError(BACKEND_UNAVAILABLE, "无法识别 agy --version 输出的版本号",
                               next_action="确认 bin 指向官方 agy CLI")
        label = ".".join(map(str, version))
        if version < MIN_CLI_VERSION:
            raise AsterunError(BACKEND_UNAVAILABLE,
                f"agy {label} 低于 Asterun 支持的最低版本 {'.'.join(map(str, MIN_CLI_VERSION))}",
                next_action="升级官方 agy CLI")
        _code, help_text = _probe_output(binary, "--help", scratch)
    missing = [flag for flag in REQUIRED_CLI_FLAGS
               if not re.search(r"(?m)^\s*" + re.escape(flag) + r"(?![\w-])", help_text)]
    if missing:
        raise AsterunError(BACKEND_UNAVAILABLE,
            f"agy {label} 缺少 Asterun 依赖的功能：{', '.join(missing)}",
            next_action="换用提供这些参数的官方 agy CLI 版本")
    result = {"version": label, "features": list(REQUIRED_CLI_FLAGS)}
    # Do not cache output that may belong to a binary replaced mid-probe.
    if (str(binary), *(_binary_identity(binary) or ())) == key:
        with _PROBE_LOCK:
            _PROBE_CACHE[key] = dict(result)
    return result


def build_command(binary: Path, model: str, timeout_seconds: int = 300) -> list[str]:
    if not model or model != model.strip() or "\x00" in model or model.startswith("-"):
        raise AsterunError(INVALID_CONFIG, "Antigravity 必须配置明确、有效的 model ID")
    # 30..3600 与配置校验共用。插件按函数摘取本段，不能引用模块级名字。
    if type(timeout_seconds) is not int or not 30 <= timeout_seconds <= 3600:
        raise AsterunError(INVALID_CONFIG, "Antigravity timeout_seconds 必须为 30..3600 的整数")
    print_timeout = f"{timeout_seconds // 60}m" if timeout_seconds % 60 == 0 else f"{timeout_seconds}s"
    return [str(binary), "--input-format", "stream-json", "--output-format", "stream-json",
            "--model", model, "--disable-slash-commands", "--print-timeout", print_timeout]


def validate_antigravity_timeout(value: object, where: str) -> None:
    if type(value) is not int or not 30 <= value <= 3600:
        raise AsterunError(INVALID_CONFIG, f"{where} 必须为 30..3600 的整数")


class AntigravityBackend:
    def __init__(self, config: BackendConfig) -> None:
        self.config = config
        self.calls: list[BackendCall] = []
        self._runtime = None
        self._runtime_lock = Lock()

    def effective_timeout_seconds(self) -> int:
        value = self.config.timeout_seconds
        return 300 if value is None else value

    @property
    def runtime(self):
        with self._runtime_lock:
            if self._runtime is None:
                from asterun.backends.antigravity_runtime import AntigravityRuntime

                self._runtime = AntigravityRuntime(timeout=self.effective_timeout_seconds())
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
        probe_cli(binary)
        home = self._prepared_home()
        if not self.config.model:
            raise AsterunError(CONFIG_REQUIRED, "Antigravity 需要显式固定 model ID")
        build_command(binary, self.config.model, self.effective_timeout_seconds())
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
        cli_version = None
        # Probing executes the binary, so it stays behind the same gates as dispatch.
        if binary is not None and self.config.enabled and live_antigravity_enabled():
            try:
                cli_version = probe_cli(binary)["version"]
            except AsterunError:
                cli_version = None
        return {
            "backend": self.config.name, "kind": "antigravity",
            "enabled_in_config": self.config.enabled, "execution_enabled": self.can_dispatch(),
            "binary_found": binary is not None, "binary": str(binary) if binary else None,
            "cli_version": cli_version,
            "live_flag": live_antigravity_enabled(), "home": self.config.home,
            "model": self.config.model, "timeout_seconds": self.effective_timeout_seconds(),
            "execution_profile": "workspace-read-v1",
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
            command = [*build_command(binary, model, self.effective_timeout_seconds()), "--add-dir", str(workspace)]
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

        # The CLI may update itself at any time. Every launch re-probes (the
        # probe cache is keyed by file identity), and the version used is
        # recorded per run; cli_updated_during_run marks any replacement after
        # the probe. A running process keeps its original image, so an update
        # mid-run is recorded but does not change the run's outcome.
        try:
            # Bind the recorded version to one file identity: if the binary
            # changes while probing, probe again. Any replacement after this
            # point (before launch or mid-run) is flagged below.
            for _attempt in range(3):
                before = _binary_identity(binary)
                cli_version = probe_cli(binary)["version"]
                identity = _binary_identity(binary)
                if identity is not None and identity == before:
                    break
            else:
                raise AsterunError(BACKEND_UNAVAILABLE, "agy 可执行文件在探测期间持续变化（可能正在自更新）",
                                   next_action="稍后重试")
        except AsterunError as exc:
            return {"status": "failed", "terminated": True, "error_code": exc.code,
                    "summary": exc.message, "native": {DISPATCH_RECEIPT_KEY: {
                        "task_id": task_id.value, "run_id": run_id.value, "stage": "preflight",
                        "prompt_attempted": False, "delivery": "not_sent", "failure_type": type(exc).__name__,
                    }}}
        # Profile denies writes, commands, URLs, MCP, and subagents. The model
        # still tries them unless the prompt says not to. This prefix is not
        # part of the stored task or the input hash.
        cli_text = READ_ONLY_PREAMBLE.replace("{workspace}", str(workspace), 1) + text
        result = self.runtime.run(run_id, cli_text, command=command, env=env, cwd=workspace,
                                  expected_model=model,
                                  publish=lambda item: publish(bind_receipt(item)), stopping=stopping)
        native_out = result.get("native")
        if isinstance(native_out, dict):
            native_out["cli_version"] = cli_version
            if _binary_identity(binary) != identity:
                native_out["cli_updated_during_run"] = True
        return bind_receipt(result)

    def request_cancel(self, run_id: RunId, script: str) -> dict[str, Any]:
        self.calls.append(BackendCall("request_cancel", {"run_id": run_id.value}))
        return self.runtime.cancel(run_id)

    def respond_approval(self, run_id: RunId, decision: str, *, native_request=None) -> dict[str, Any]:
        raise AsterunError(CAPABILITY_UNSUPPORTED, "agy headless 不支持机器审批消息；不能转发此决定")

    def close(self) -> None:
        if self._runtime is not None:
            self._runtime.close()
