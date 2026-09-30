"""受管 Grok 会话绑定和跨进程互斥；不认领外部原生会话。"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import quote

from asterun.errors import AsterunError


def _reject(message):
    raise AsterunError("BINDING_MISMATCH", message)


def _safe(path):
    if any(p.is_symlink() for p in (path, *path.parents)):
        _reject("Grok 会话路径不能含符号链接")
    return path


def binding(config, home, cwd, binary, identity):
    # Native credentials remain opaque. Replacing/changing their generation
    # cannot silently reuse a session as another account (refresh may also
    # conservatively require a new session until native identity is available).
    auth = home / "auth.json"
    info = auth.stat() if auth.exists() else None
    generation = None if info is None else [info.st_ino, info.st_size, info.st_mtime_ns]
    return {"home": str(home.resolve()), "workspace": str(cwd.resolve()),
            "binary": str(binary.resolve()), "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "model": config.model, "profile": config.execution_profile,
            "connection_ref": config.connection_ref, "identity": identity or {},
            "credential_generation": generation}


def verify_resume_cli(binary, env):
    # Local capability discovery only; never probe by sending a prompt.
    from asterun.backends.grok_code import CodeProcessHandle
    import selectors
    import time
    process = CodeProcessHandle()
    ready = False
    proc = None
    try:
        proc = process.spawn([str(binary), "--help"], env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        output = bytearray()
        deadline = time.monotonic() + 5
        closed = False
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline and len(output) <= 128 * 1024:
                for key, _ in selector.select(timeout=0.05):
                    part = os.read(key.fd, 4096)
                    if not part:
                        closed = True
                        selector.unregister(key.fd)
                    else:
                        output.extend(part)
                if closed and proc.poll() is not None:
                    break
        ready = (closed and proc.poll() == 0 and len(output) <= 128 * 1024
                 and b"--resume" in output and b"--session-id" in output)
    except OSError:
        ready = False
    finally:
        process.close()
        if proc is not None:
            proc.stdout.close()
    if not ready:
        raise AsterunError("CAPABILITY_UNSUPPORTED", "当前 Grok CLI 未确认支持 --resume 与 --session-id；未派发")


def _log_hashes(session):
    hashes = {}
    for name in ("summary.json", "updates.jsonl"):
        path = _safe(session / name)
        h = hashlib.sha256()
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(chunk)
            after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            _reject("Grok 原生日志仍在变化")
        hashes[name] = h.hexdigest()
    return hashes


@contextmanager
def session_lease(home: Path, cwd: Path, session_id: str, expected: dict, *, resume: bool, update_after=False):
    if not isinstance(session_id, str) or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", session_id):
        _reject("Grok 原生会话引用必须是规范 UUID")
    root = _safe(home / ".asterun-session-bindings")
    root.mkdir(mode=0o700, exist_ok=True)
    lock = _safe(root / (session_id + ".lock"))
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AsterunError("REMOTE_STATE_UNKNOWN", "Grok 原生会话正在被另一个执行者使用") from None
        marker = _safe(root / (session_id + ".json"))
        session = _safe(home / "sessions" / quote(str(cwd.resolve()), safe="") / session_id)
        if resume:
            try:
                if marker.stat().st_size > 16384:
                    _reject("Grok 绑定记录过大")
                saved = json.loads(marker.read_text())
                if saved.get("binding") != expected:
                    _reject("Grok 会话的账户、运行主机、配置、CLI 或工作区绑定已改变")
                if saved.get("logs") != _log_hashes(session):
                    _reject("Grok 原生日志被外部修改；不能接管未知轮次")
                summary_path = _safe(session / "summary.json")
                updates = _safe(session / "updates.jsonl")
                if summary_path.stat().st_size > 1024 * 1024:
                    _reject("Grok 原生摘要过大")
                summary = json.loads(summary_path.read_text())
                if summary.get("info", {}).get("id") != session_id or summary.get("info", {}).get("cwd") != str(cwd.resolve()):
                    _reject("Grok 原生摘要不属于请求的会话")
                with updates.open("rb") as stream:
                    stream.seek(-1, os.SEEK_END)
                    if stream.read(1) != b"\n":
                        _reject("Grok 原生日志尾部不完整，需先对账")
            except (OSError, ValueError, TypeError, AttributeError):
                _reject("缺少完整的受管 Grok 会话或绑定记录；不会静默创建新会话")
        else:
            try:
                with os.fdopen(os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600), "w") as stream:
                    json.dump({"binding": expected, "logs": None}, stream, sort_keys=True)
                    stream.flush()
                    os.fsync(stream.fileno())
            except FileExistsError:
                _reject("Grok 新会话引用已存在")
        try:
            yield
        finally:
            if update_after:
                try:
                    hashes = _log_hashes(session)
                except (OSError, AsterunError):
                    hashes = None  # No stable logs means no future resume.
                temp_fd, temp_name = tempfile.mkstemp(dir=root, prefix=".binding-")
                try:
                    with os.fdopen(temp_fd, "w") as stream:
                        json.dump({"binding": expected, "logs": hashes}, stream, sort_keys=True)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temp_name, marker)
                finally:
                    Path(temp_name).unlink(missing_ok=True)
    finally:
        os.close(fd)
