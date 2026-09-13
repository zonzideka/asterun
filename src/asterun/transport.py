"""行分隔 JSON-RPC 传输。由旧 agent_runner/transport.py 迁入，去掉作者路径假设。"""

from __future__ import annotations

import json
import os
import subprocess
import signal
import threading
import time
from pathlib import Path
from typing import Any, Callable


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.data = data
        super().__init__(f"JSON-RPC error {code}: {message}" + (f" ({data})" if data else ""))


MessageCallback = Callable[[dict[str, Any]], None]


class LineJsonRpcTransport:
    """newline JSON-RPC。响应可省略 jsonrpc 字段，id 可以是字符串或整数。"""

    def __init__(self, *, include_jsonrpc: bool = True):
        self.proc: subprocess.Popen | None = None
        self.include_jsonrpc = include_jsonrpc
        self._id_counter = 0
        self._responses: dict[Any, dict[str, Any]] = {}
        self._requests: list[MessageCallback] = []
        self._notifications: list[MessageCallback] = []
        self._messages: list[MessageCallback] = []
        self._running = False
        self._lock = threading.Lock()
        self._write_lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._reader: threading.Thread | None = None
        self._closed = False

    def spawn(
        self,
        command: list[str],
        *,
        cwd: str | Path,
        env: dict[str, str] | None = None,
    ) -> None:
        with self._write_lock:
            if self._closed or self.proc is not None:
                raise ConnectionError("连接已关闭或已经启动")
            self.proc = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                cwd=str(cwd), text=True, bufsize=1, env=env if env is not None else os.environ.copy(),
                start_new_session=True,
            )
            self._running = True
            self._reader = threading.Thread(target=self._read_loop, daemon=True)
            self._reader.start()

    def _read_loop(self) -> None:
        proc = self.proc
        assert proc and proc.stdout
        while self._running:
            try:
                line = proc.stdout.readline(1024 * 1024 + 1)
                if not line:
                    break
                if len(line) > 1024 * 1024:
                    break
                self._handle_line(line)
            except Exception:
                if self._running:
                    break
        with self._condition:
            self._running = False
            self._condition.notify_all()

    def _handle_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return
        if isinstance(msg, dict):
            self._handle_message(msg)

    def _handle_message(self, msg: dict[str, Any]) -> None:
        for cb in list(self._messages):
            self._safe_call(cb, msg)
        has_id = "id" in msg
        has_method = "method" in msg
        if has_id and not has_method:
            with self._condition:
                self._responses[msg["id"]] = msg
                # 迟到或未请求的响应不能让长期运行的连接无限积累。
                if len(self._responses) > 128:
                    self._responses.pop(next(iter(self._responses)))
                self._condition.notify_all()
        elif has_id and has_method:
            for cb in list(self._requests):
                self._safe_call(cb, msg)
        else:
            for cb in list(self._notifications):
                self._safe_call(cb, msg)

    @staticmethod
    def _safe_call(cb: MessageCallback, msg: dict[str, Any]) -> None:
        try:
            cb(msg)
        except Exception:
            pass

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
        with self._lock:
            self._id_counter += 1
            rid = self._id_counter
        msg: dict[str, Any] = {"id": rid, "method": method}
        if self.include_jsonrpc:
            msg["jsonrpc"] = "2.0"
        if params is not None:
            msg["params"] = params
        self._send(msg)
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                if rid in self._responses:
                    resp = self._responses.pop(rid)
                    if "error" in resp:
                        err = resp["error"]
                        raise JsonRpcError(err.get("code", -1), err.get("message", "?"), err.get("data"))
                    return resp.get("result", {})
                if not self._running:
                    raise ConnectionError("原生 JSON-RPC 连接已关闭")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"No response to {method} within {timeout}s")
                self._condition.wait(remaining)

    def _send(self, message):
        with self._write_lock:
            if not self.proc or not self.proc.stdin:
                raise ConnectionError("原生 JSON-RPC 连接未打开")
            self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()

    def respond(self, request_id: int | str, result: dict[str, Any]) -> None:
        msg: dict[str, Any] = {"id": request_id, "result": result}
        if self.include_jsonrpc:
            msg["jsonrpc"] = "2.0"
        self._send(msg)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        msg: dict[str, Any] = {"method": method}
        if self.include_jsonrpc:
            msg["jsonrpc"] = "2.0"
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def on_request(self, cb: MessageCallback) -> None:
        self._requests.append(cb)

    def on_notification(self, cb: MessageCallback) -> None:
        self._notifications.append(cb)

    def on_message(self, cb: MessageCallback) -> None:
        self._messages.append(cb)

    def close(self) -> None:
        with self._condition:
            self._running = False
            self._condition.notify_all()
        with self._write_lock:
            self._closed = True
            proc, self.proc = self.proc, None
        if proc:
            try:
                if proc.poll() is None and hasattr(os, "killpg"):
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except (ProcessLookupError, PermissionError):
                        proc.terminate()
                elif proc.poll() is None:
                    proc.terminate()
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    if hasattr(os, "killpg"):
                        os.killpg(proc.pid, signal.SIGKILL)
                    else:
                        proc.kill()
                except (ProcessLookupError, PermissionError):
                    proc.kill()
                proc.wait(timeout=2)
            if self._reader and self._reader is not threading.current_thread():
                self._reader.join(timeout=1)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream:
                    stream.close()

    @property
    def alive(self) -> bool:
        proc = self.proc
        return self._running and proc is not None and proc.poll() is None
