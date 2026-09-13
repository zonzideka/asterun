"""单用户本机 IPC。服务进程独占 SQLite；CLI/MCP 只传递语义请求。"""

from __future__ import annotations

import json
import os
import selectors
import socket
import stat
import time
from pathlib import Path
from threading import Event

from asterun.contracts import Envelope
from asterun.control_protocol import loads
from asterun.errors import AsterunError, BACKEND_UNAVAILABLE, INVALID_REQUEST

MAX_MESSAGE = 1024 * 1024


class LocalClient:
    def __init__(self, path: Path, *, entry="cli", timeout=30):
        self.path, self.entry, self.timeout = path, entry, timeout

    def handle(self, method, payload=None):
        may_have_been_sent = False
        try:
            info = self.path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise AsterunError(INVALID_REQUEST, "核心 socket 类型、所有者或权限不匹配")
            message = json.dumps({"method": method, "payload": {} if payload is None else payload, "entry": self.entry},
                                 ensure_ascii=False, allow_nan=False).encode() + b"\n"
            if len(message) > MAX_MESSAGE:
                raise AsterunError(INVALID_REQUEST, "请求超过本机 IPC 大小上限")
            loads(message)
            deadline = time.monotonic() + self.timeout
            def remaining():
                value = deadline - time.monotonic()
                if value <= 0:
                    raise TimeoutError("本机请求总时限已到")
                return value
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(remaining())
                conn.connect(str(self.path))
                may_have_been_sent = True
                conn.settimeout(remaining())
                conn.sendall(message)
                response = bytearray()
                while b"\n" not in response:
                    conn.settimeout(remaining())
                    chunk = conn.recv(65536)
                    if not chunk:
                        raise OSError("响应未完成")
                    response.extend(chunk)
                    if len(response) > MAX_MESSAGE:
                        raise AsterunError(INVALID_REQUEST, "响应超过本机 IPC 大小上限")
            data = loads(bytes(response.partition(b"\n")[0]))
            return Envelope(**data)
        except AsterunError as error:
            if method == "control.command" and may_have_been_sent:
                return _unknown_control_result(payload)
            return Envelope(ok=False, error=error.to_dict())
        except (OSError, ValueError, TypeError, UnicodeError):
            # 可能已经受理；不自动重试有副作用请求。
            if method == "control.command" and may_have_been_sent:
                return _unknown_control_result(payload)
            return Envelope(ok=False, error=AsterunError(BACKEND_UNAVAILABLE,
                "未取得核心响应；请用原幂等键查询，勿创建新的重复请求",
                next_action="确认 asterun serve 正在运行，核对状态目录").to_dict())

    def close(self):
        pass


def _unknown_control_result(payload):
    from asterun.control import api_error

    command = payload.get("command", payload) if isinstance(payload, dict) else {}
    request_id = command.get("request_id") if isinstance(command, dict) else None
    error = api_error(AsterunError("UNKNOWN_REMOTE", "控制命令可能已受理，但未取得可信响应；请用原幂等键核对，勿创建重复命令"), request_id)
    return Envelope(ok=False, error=error)


def serve(app, path: Path, *, stop: Event | None = None, ready: Event | None = None):
    stop = stop or Event()
    selector = selectors.DefaultSelector()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    clients = set()
    bound_inode = None
    try:
        if app.instance_lock is None or app.state_dir.resolve() != path.parent.resolve():
            raise AsterunError(INVALID_REQUEST, "常驻服务必须先独占对应状态目录")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent = path.parent.stat()
        if parent.st_uid != os.getuid() or parent.st_mode & 0o022:
            raise AsterunError(INVALID_REQUEST, "核心 socket 目录必须由当前用户拥有且不可被其他用户写入")
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise AsterunError(INVALID_REQUEST, "socket 路径已被其他类型文件占用")
            # 调用方已取得状态目录的 OS 锁，只有同目录旧 socket 可在此清理。
            path.unlink()
        previous_umask = os.umask(0o177)
        try:
            listener.bind(str(path))
        finally:
            os.umask(previous_umask)
        path.chmod(0o600)
        bound_inode = path.lstat().st_ino
        listener.listen(32)
        listener.setblocking(False)
        selector.register(listener, selectors.EVENT_READ, None)
        if ready:
            ready.set()
        while not stop.is_set():
            app.poll()
            for key, mask in selector.select(0.03):
                if key.fileobj is listener:
                    conn, _ = listener.accept()
                    if len(clients) >= 32:
                        conn.close()
                        continue
                    conn.setblocking(False)
                    clients.add(conn)
                    selector.register(conn, selectors.EVENT_READ, bytearray())
                    continue
                conn, buffer = key.fileobj, key.data
                try:
                    if mask & selectors.EVENT_WRITE:
                        sent = conn.send(buffer)
                        del buffer[:sent]
                        if buffer:
                            continue
                    else:
                        data = conn.recv(65536)
                        if data:
                            buffer.extend(data)
                        if data and len(buffer) <= MAX_MESSAGE and b"\n" not in buffer:
                            continue
                        if data:
                            try:
                                if len(buffer) > MAX_MESSAGE:
                                    raise ValueError()
                                request = loads(bytes(buffer.partition(b"\n")[0]))
                                if (not isinstance(request, dict) or set(request) != {"method", "payload", "entry"}
                                        or not isinstance(request["method"], str)
                                        or not isinstance(request["payload"], dict)
                                        or request["entry"] not in {"cli", "mcp"}):
                                    raise ValueError()
                                app.entry = request["entry"]
                                result = app.handle(request["method"], request["payload"]).to_dict()
                            except AsterunError as error:
                                result = Envelope(ok=False, error=error.to_dict()).to_dict()
                            except (ValueError, TypeError, UnicodeError):
                                result = Envelope(ok=False, error=AsterunError(INVALID_REQUEST, "无效的本机 IPC 请求").to_dict()).to_dict()
                            finally:
                                app.entry = "core"
                            output = json.dumps(result, ensure_ascii=False).encode() + b"\n"
                            if len(output) > MAX_MESSAGE:
                                output = json.dumps(Envelope(ok=False, error=AsterunError(INVALID_REQUEST,
                                    "响应过大，请减小事件页大小").to_dict()).to_dict()).encode() + b"\n"
                            selector.modify(conn, selectors.EVENT_WRITE, bytearray(output))
                            continue
                except (OSError, UnicodeError):
                    pass
                selector.unregister(conn)
                clients.discard(conn)
                conn.close()
    finally:
        for conn in clients:
            conn.close()
        listener.close()
        selector.close()
        if bound_inode and path.exists() and path.lstat().st_ino == bound_inode:
            path.unlink()
        app.close()
