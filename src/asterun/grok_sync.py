"""Opt-in, one-way copies of native Grok logs for local usage readers."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path

from asterun.errors import AsterunError, INVALID_CONFIG

FILES = ("summary.json", "updates.jsonl")
MARKER = ".asterun-sync.json"


def _invalid(message):
    return AsterunError(INVALID_CONFIG, message)


def _root(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path.is_symlink():
        raise _invalid("会话同步目录必须是无符号链接的绝对路径")
    return path.resolve()


def _directory(path):
    if path.is_symlink():
        raise ValueError("symlink_directory")
    path.mkdir(mode=0o700, exist_ok=True)
    if not path.is_dir():
        raise ValueError("not_directory")


def _read(path):
    # Do not follow native-log or destination symlinks, including dangling ones.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("not_regular_file")
        data = handle.read()
        after = os.fstat(handle.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("source_changed")
    return data


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _write(path, data):
    fd, temporary = tempfile.mkstemp(prefix=".asterun-sync-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _copy_session(source, destination, source_root):
    data = {name: _read(source / name) for name in FILES}
    summary = json.loads(data["summary.json"])
    if not isinstance(summary, dict):
        raise ValueError("invalid_summary")
    updates = data["updates.jsonl"]
    if updates and not updates.endswith(b"\n"):
        raise ValueError("incomplete_log_line")
    # Reject malformed snapshots rather than publishing partial usage histories.
    for line in updates.splitlines():
        if line.strip():
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("invalid_update")
            sid = row.get("params", {}).get("sessionId")
            if sid is not None and sid != source.name:
                raise ValueError("session_id_mismatch")
    hashes = {name: _sha(content) for name, content in data.items()}
    identity = {"source_home": str(source_root.parent),
                "session_path": str(source.relative_to(source_root))}
    _directory(destination.parent)
    if destination.is_symlink():
        raise ValueError("symlink_directory")
    existed = destination.exists()
    if existed and not destination.is_dir():
        raise ValueError("not_directory")
    previous = {}
    current_hashes = {}
    if existed:
        marker = destination / MARKER
        if marker.exists() or marker.is_symlink():
            previous = json.loads(_read(marker))
            if any(previous.get(key) != value for key, value in identity.items()):
                return "conflict"
        # A native client may have resumed this copy. Never overwrite its history.
        if set(p.name for p in destination.iterdir()) - {*FILES, MARKER}:
            return "conflict"
        for name in FILES:
            path = destination / name
            if path.exists() or path.is_symlink():
                existing = _read(path)
                current = _sha(existing)
                if current not in {hashes[name], previous.get("hashes", {}).get(name),
                                   previous.get("pending_hashes", {}).get(name)}:
                    return "conflict"
                current_hashes[name] = current
                if name == "updates.jsonl" and not data[name].startswith(existing):
                    return "conflict"
            elif not previous:
                return "conflict"
        if all((destination / name).is_file() and _sha(_read(destination / name)) == hashes[name]
               for name in FILES):
            # A prior export may have published the data but failed to write its marker.
            if previous.get("hashes") != hashes or "pending_hashes" in previous:
                _write(marker, (json.dumps(identity | {"hashes": hashes}) + "\n").encode())
            return "unchanged"
    if not existed:
        # Publish a complete new session together; readers never see an empty log.
        with tempfile.TemporaryDirectory(prefix=".asterun-sync-", dir=destination.parent) as tmp:
            stage = Path(tmp) / "session"
            stage.mkdir(mode=0o700)
            for name, content in data.items():
                _write(stage / name, content)
            _write(stage / MARKER, (json.dumps(identity | {"hashes": hashes}) + "\n").encode())
            os.rename(stage, destination)
    else:
        # Persist both verified current bytes and intended replacements before
        # overwriting data. A retry can then recognize partial writes even if
        # the source has advanced again, including after an interrupted retry.
        _write(destination / MARKER, (json.dumps(identity | {
            "hashes": current_hashes, "pending_hashes": hashes}) + "\n").encode())
        # Summary first, authoritative usage log last. No deletion or truncation
        # of unrelated native files; modified destination copies fail closed.
        for name, content in data.items():
            _write(destination / name, content)
        _write(destination / MARKER, (json.dumps(identity | {"hashes": hashes}) + "\n").encode())
    return "copied"


def sync_sessions(home, destination_home, *, session_id=None):
    """Copy only two native log files. No credentials, config, or model calls.

    Native files are untrusted input. Errors are per-session, and a destination
    lock serializes Asterun writers across instances. Nothing is ever deleted.
    """
    source_home, target_home = _root(home), _root(destination_home)
    if (source_home == target_home or source_home in target_home.parents
            or target_home in source_home.parents):
        raise _invalid("同步源与目标目录不能相同或互相包含")
    if session_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
        raise _invalid("无效的原生会话 ID")
    source = source_home / "sessions"
    if source.is_symlink() or not source.is_dir():
        raise _invalid("同步源 sessions 目录不存在或为符号链接")
    target_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = target_home / "sessions"
    _directory(target)
    report = {"copied": 0, "unchanged": 0, "conflicts": 0, "skipped": 0,
              "matched": 0, "issues": []}
    fd = os.open(target / ".asterun-sync.lock",
                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "r+") as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            raise _invalid("同步锁必须是普通文件")
        fcntl.flock(lock, fcntl.LOCK_EX)
        for group in sorted(source.iterdir()):
            if group.name.startswith(".") or not group.is_dir() or group.is_symlink():
                continue
            for session in sorted(group.iterdir()):
                if session_id is not None and session.name != session_id:
                    continue
                if session.name.startswith(".") or not session.is_dir():
                    continue
                report["matched"] += 1
                try:
                    if session.is_symlink():
                        raise ValueError("symlink_directory")
                    outcome = _copy_session(session, target / group.name / session.name, source)
                    key = "conflicts" if outcome == "conflict" else outcome
                    report[key] += 1
                    reason = "destination_conflict" if outcome == "conflict" else None
                except (OSError, ValueError, TypeError, AttributeError) as error:
                    report["skipped"] += 1
                    # Do not expose log contents or native exception messages.
                    reason = type(error).__name__
                if reason and len(report["issues"]) < 50:
                    report["issues"].append({"session_id": session.name, "reason": reason})
        if session_id is not None and not report["matched"]:
            report["skipped"] += 1
            report["issues"].append({"session_id": session_id, "reason": "session_not_found"})
    return report
