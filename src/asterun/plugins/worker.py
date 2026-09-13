"""有界 stdio RPC；只提供受信插件的进程故障隔离，不是 OS 沙箱。"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import tempfile
import time
from typing import Any
from uuid import uuid4

from asterun.errors import AsterunError, BACKEND_UNAVAILABLE, INVALID_CONFIG
from asterun.plugin_api.protocol import (
    WORKER_PROTOCOL_VERSION, json_copy, validate_worker_request, validate_worker_response,
)


@dataclass(frozen=True, slots=True)
class WorkerLimits:
    timeout_seconds: float = 30.0
    max_frame_bytes: int = 256 * 1024
    max_output_bytes: int = 1024 * 1024
    max_stderr_bytes: int = 256 * 1024

    def __post_init__(self):
        if type(self.timeout_seconds) not in {int, float} or not 0 < self.timeout_seconds <= 3600:
            raise ValueError("worker 截止时间必须在 0 到 3600 秒内")
        for value in (self.max_frame_bytes, self.max_output_bytes, self.max_stderr_bytes):
            if type(value) is not int or not 1 <= value <= 16 * 1024 * 1024:
                raise ValueError("worker 字节限制必须在 1 到 16 MiB 内")


class WorkerFailure(AsterunError):
    """公开错误只含本地分类；不携带插件消息、stdout 或 stderr。"""

    def __init__(self, reason: str, *, request_sent: bool, stderr_bytes: int = 0):
        super().__init__(BACKEND_UNAVAILABLE, "插件 worker 未取得可确认响应",
                         details={"reason": reason, "request_sent": request_sent,
                                  "stderr_bytes": stderr_bytes})
        self.reason = reason
        self.request_sent = request_sent
        self.stderr_bytes = stderr_bytes


@dataclass(frozen=True, slots=True)
class WorkerReply:
    result: dict[str, Any]
    stderr_bytes: int
    stdout_bytes: int


def _loads(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique)


def _contains_secret(value, secrets):
    if isinstance(value, str):
        return any(secret and secret in value for secret in secrets)
    if isinstance(value, dict):
        return any(_contains_secret(key, secrets) or _contains_secret(child, secrets)
                   for key, child in value.items())
    if isinstance(value, list):
        return any(_contains_secret(child, secrets) for child in value)
    return False


class WorkerHost:
    """一次 RPC 一个进程；取消/观察无需等待阻塞的 start RPC 完成。

    单线程 selector 同时读 stdout/stderr 并增量写 stdin，内存最多一帧和
    一次系统读取；内核管道提供背压，不使用无限队列或 communicate 缓冲。
    """

    def __init__(self, registry, registration, *, limits: WorkerLimits | None = None):
        self.registry = registry
        self.registration = registration
        self.limits = limits or WorkerLimits()

    def _verify(self, method):
        current = self.registry.verify_installation(self.registration.plugin_id)
        if current.source != "external" or current.runtime_path is None or current.runtime_sha256 is None:
            raise AsterunError(INVALID_CONFIG, "外部 worker 执行需要固定实际安装代码的 runtime 摘要")
        expected = self.registration
        fields = ("runner", "runner_sha256", "installation_sha256", "runtime_sha256", "manifest_file_sha256")
        if current.manifest.sha256 != expected.manifest.sha256 or any(
                getattr(current, name) != getattr(expected, name) for name in fields):
            raise AsterunError(INVALID_CONFIG, "活动 worker 不能热替换已固定的插件字节")
        if not current.enabled and method not in {"execution.observe", "execution.reconcile", "execution.cancel"}:
            raise AsterunError(BACKEND_UNAVAILABLE, "插件已停用，不能开始新动作")
        return current

    def call(self, method, input, context, *, cwd, environment=None, secrets=(), stopping=None, timeout_seconds=None):
        registration = self._verify(method)
        request_id = "wreq_" + uuid4().hex
        request = validate_worker_request({"jsonrpc": "2.0", "id": request_id, "method": method,
            "params": {"protocol_version": WORKER_PROTOCOL_VERSION, "input": input, "context": context}})
        encoded = json.dumps(request, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(encoded) > self.limits.max_frame_bytes:
            raise WorkerFailure("request_frame_limit", request_sent=False)
        if stopping is not None and stopping.is_set():
            raise WorkerFailure("core_stopping", request_sent=False)
        if not isinstance(environment, (dict, type(None))):
            raise AsterunError(INVALID_CONFIG, "worker 凭据环境必须为显式对象")
        environment = environment or {}
        forbidden = {"PATH", "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONSAFEPATH",
                     "PYTHONNOUSERSITE", "PYTHONDONTWRITEBYTECODE", "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES",
                     "DYLD_LIBRARY_PATH", "SSH_AUTH_SOCK", "DOCKER_HOST", "BASH_ENV", "ENV", "TMPDIR"}
        if any(not isinstance(key, str) or not isinstance(value, str) or "\0" in key + value
               or key in forbidden or key.startswith(("DYLD_", "LD_")) for key, value in environment.items()):
            raise AsterunError(INVALID_CONFIG, "worker 凭据注入不能改变执行器或宿主环境入口")
        with tempfile.TemporaryDirectory(prefix="asterun-worker-") as temporary:
            home = Path(temporary)
            (home / "tmp").mkdir(mode=0o700)
            env = {"PATH": os.pathsep.join([str(Path(registration.runner[0]).parent), "/usr/bin", "/bin"]),
                   "HOME": str(home), "TMPDIR": str(home / "tmp"), "LANG": "C.UTF-8",
                   "PYTHONSAFEPATH": "1", "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
                   "PYTHONUNBUFFERED": "1", **environment}
            # secrets 只用于内存匹配，既不入 argv，也不放在错误对象里。
            try:
                return self._exchange(registration.runner, encoded, request_id, cwd, env,
                                      tuple(secrets), stopping, timeout_seconds)
            finally:
                env.clear()

    def _exchange(self, argv, encoded, request_id, cwd, env, secrets, stopping, timeout_seconds):
        sent = False
        stdout_bytes = stderr_bytes = offset = 0
        buffer = bytearray()
        process = None
        duration = self.limits.timeout_seconds if timeout_seconds is None else min(timeout_seconds, self.limits.timeout_seconds)
        if duration <= 0:
            raise WorkerFailure("timeout", request_sent=False)
        deadline = time.monotonic() + duration
        try:
            process = subprocess.Popen(list(argv), shell=False, cwd=Path(cwd), env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0, start_new_session=True)
            with selectors.DefaultSelector() as selector:
                for stream, events, name in ((process.stdin, selectors.EVENT_WRITE, "stdin"),
                        (process.stdout, selectors.EVENT_READ, "stdout"),
                        (process.stderr, selectors.EVENT_READ, "stderr")):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, events, name)
                while True:
                    if stopping is not None and stopping.is_set():
                        raise WorkerFailure("core_stopping", request_sent=sent, stderr_bytes=stderr_bytes)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise WorkerFailure("timeout", request_sent=sent, stderr_bytes=stderr_bytes)
                    ready = selector.select(min(0.05, remaining))
                    for key, _ in ready:
                        if key.data == "stdin":
                            try:
                                written = os.write(key.fd, encoded[offset:offset + 4096])
                            except BlockingIOError:
                                continue
                            sent = sent or written > 0
                            offset += written
                            if offset == len(encoded):
                                selector.unregister(key.fileobj)
                                key.fileobj.close()
                            continue
                        try:
                            chunk = os.read(key.fd, 4096)
                        except BlockingIOError:
                            continue
                        if not chunk:
                            selector.unregister(key.fileobj)
                            if key.data == "stdout":
                                raise WorkerFailure("stdout_closed", request_sent=sent, stderr_bytes=stderr_bytes)
                            continue
                        if key.data == "stderr":
                            stderr_bytes += len(chunk)
                            if stderr_bytes > self.limits.max_stderr_bytes:
                                raise WorkerFailure("stderr_limit", request_sent=sent, stderr_bytes=stderr_bytes)
                            continue  # 原文不保留，不转发，不写日志。
                        stdout_bytes += len(chunk)
                        if stdout_bytes > self.limits.max_output_bytes:
                            raise WorkerFailure("stdout_limit", request_sent=sent, stderr_bytes=stderr_bytes)
                        buffer.extend(chunk)
                        newline = buffer.find(b"\n")
                        if newline < 0:
                            if len(buffer) > self.limits.max_frame_bytes:
                                raise WorkerFailure("response_frame_limit", request_sent=sent, stderr_bytes=stderr_bytes)
                            continue
                        if newline + 1 > self.limits.max_frame_bytes or buffer[newline + 1:].strip():
                            raise WorkerFailure("response_frame_limit", request_sent=sent, stderr_bytes=stderr_bytes)
                        try:
                            response = validate_worker_response(_loads(buffer[:newline]), request_id=request_id)
                        except (AsterunError, ValueError, UnicodeError, RecursionError):
                            raise WorkerFailure("invalid_response", request_sent=sent, stderr_bytes=stderr_bytes) from None
                        if "error" in response:
                            raise WorkerFailure("plugin_error", request_sent=sent, stderr_bytes=stderr_bytes)
                        if _contains_secret(response["result"], secrets):
                            raise WorkerFailure("secret_in_response", request_sent=sent, stderr_bytes=stderr_bytes)
                        return WorkerReply(response["result"], stderr_bytes, stdout_bytes)
        except WorkerFailure:
            raise
        except (OSError, ValueError, TypeError):
            raise WorkerFailure("process_io", request_sent=sent, stderr_bytes=stderr_bytes) from None
        finally:
            if process is not None:
                # 仅清理本机 worker；即便 kill 成功，也不推断远端作业已停止。
                try:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        # macOS 已退出但尚未 wait 的进程组可能返回 EPERM。
                        # poll/wait 核对实际退出；异常不能跳过回收和管道关闭。
                        if process.poll() is None:
                            try:
                                process.kill()
                            except (ProcessLookupError, PermissionError):
                                pass
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        raise WorkerFailure("cleanup_unconfirmed", request_sent=sent, stderr_bytes=stderr_bytes) from None
                finally:
                    for stream in (process.stdin, process.stdout, process.stderr):
                        stream.close()
