"""GrokBot 可选宿主的 Asterun 接入账本。不发送消息、不定时唤醒、不自动审批。"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import time
from typing import Any


MAX_INPUT_BYTES = 512 * 1024
MAX_CORE_STDOUT = 2 * 1024 * 1024
DEFAULT_TIMEOUT = 30.0
SUBMIT_TIMEOUT = 60.0
POLL_ITEM_TIMEOUT = 15.0
BUSY_TIMEOUT_MS = 5000
SCHEMA_VERSION = 2
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
NOTIFY_STATUSES = frozenset({
    "waiting_input", "waiting_auth", "pending_reconcile", "paused",
    "succeeded", "failed", "cancelled",
})
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
REQUEST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:+-]{0,127}\Z")
DEST_RE = re.compile(r"[^\x00-\x1f]{1,256}\Z")
ALIAS_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
CORE_CODES = frozenset({
    "INVALID_REQUEST", "NOT_FOUND", "IDEMPOTENCY_CONFLICT", "SCOPE_DENIED",
    "BINDING_MISMATCH", "CONFIG_REQUIRED", "INVALID_CONFIG", "INSTANCE_LOCKED",
    "BACKEND_UNAVAILABLE", "REMOTE_STATE_UNKNOWN", "REVISION_CONFLICT",
    "PATH_INVALID", "PATH_OUT_OF_SCOPE", "UNKNOWN_PROJECT", "AUTH_REQUIRED",
    "CAPABILITY_UNSUPPORTED",
})
CLEAN_ENV = {
    "PATH": "/usr/bin:/bin:/usr/local/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
}


class BridgeError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


class HelpRequested(Exception):
    pass


class BridgeParser(argparse.ArgumentParser):
    def print_help(self, file=None) -> None:
        super().print_help(sys.stderr)

    def print_usage(self, file=None) -> None:
        super().print_usage(sys.stderr)

    def error(self, message: str) -> None:
        raise BridgeError("INVALID_REQUEST", "命令参数无效")

    def exit(self, status: int = 0, message: str | None = None) -> None:
        if status == 0:
            raise HelpRequested()
        raise BridgeError("INVALID_REQUEST", "命令参数无效")


def emit(ok: bool, data: dict[str, Any] | None, error: dict[str, Any] | None) -> None:
    json.dump({"ok": ok, "data": data, "error": error}, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    sys.stdout.flush()


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def require_name(value: str, label: str, pattern: re.Pattern[str] = NAME_RE) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise BridgeError("INVALID_REQUEST", f"{label} 无效")
    return value


def check_no_symlinks(path: Path) -> None:
    current = path
    for _ in range(64):
        if current.is_symlink():
            raise BridgeError("PATH_INVALID", "路径不能是符号链接")
        parent = current.parent
        if parent == current:
            return
        current = parent
    raise BridgeError("PATH_INVALID", "路径无效")


def require_absolute(path_text: str, label: str) -> Path:
    path = Path(path_text)
    if not path.is_absolute():
        raise BridgeError("PATH_INVALID", f"{label} 必须是绝对路径")
    check_no_symlinks(path)
    return path


def require_regular_file(path: Path, label: str, *, executable: bool = False) -> Path:
    if path.is_symlink():
        raise BridgeError("PATH_INVALID", f"{label} 不能是符号链接")
    if not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
        raise BridgeError("PATH_INVALID", f"{label} 必须是普通文件")
    if executable and not os.access(path, os.X_OK):
        raise BridgeError("PATH_INVALID", f"{label} 不可执行")
    return path


def require_directory(path: Path, label: str) -> Path:
    if path.is_symlink():
        raise BridgeError("PATH_INVALID", f"{label} 不能是符号链接")
    if not path.is_dir():
        raise BridgeError("PATH_INVALID", f"{label} 必须是目录")
    return path


def ensure_private_dir(path: Path) -> None:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_dir():
            raise BridgeError("PATH_INVALID", "账本目录无效")
        mode = path.stat().st_mode
        if not stat.S_ISDIR(mode) or mode & 0o077:
            raise BridgeError("PATH_INVALID", "账本目录权限必须仅限当前用户")
        return
    missing = []
    current = path
    while not current.exists():
        if current.is_symlink():
            raise BridgeError("PATH_INVALID", "账本目录无效")
        missing.append(current)
        current = current.parent
    require_directory(current, "账本目录的父目录")
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            require_directory(directory, "账本目录")
    # Existing ancestors may be public traversal directories; the ledger leaf is private.
    ensure_private_dir(path)


def read_limited(path: Path | None, *, stdin: bool) -> str:
    if stdin:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    else:
        assert path is not None
        if path.is_symlink():
            raise BridgeError("PATH_INVALID", "输入不能是符号链接")
        if not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
            raise BridgeError("PATH_INVALID", "输入必须是普通文件")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise BridgeError("INVALID_REQUEST", "输入超过 512 KiB 上限")
    try:
        return raw.decode("utf-8")
    except UnicodeError as exc:
        raise BridgeError("INVALID_REQUEST", "输入必须是 UTF-8 文本") from exc


def invoke_asterun(
    asterun: str,
    argv: list[str],
    *,
    stdin_text: str | None = None,
    timeout: float,
) -> dict[str, Any]:
    if not math.isfinite(timeout) or timeout <= 0:
        raise BridgeError("INVALID_REQUEST", "超时必须为正数")
    command = [asterun, *argv]
    try:
        completed = subprocess.run(
            command,
            input=None if stdin_text is None else stdin_text.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            shell=False,
            env=CLEAN_ENV,
        )
    except subprocess.TimeoutExpired as exc:
        raise BridgeError("CORE_TIMEOUT", "核心命令超时", retryable=True) from exc
    except OSError as exc:
        raise BridgeError("CORE_UNAVAILABLE", "无法启动 Asterun 可执行文件") from exc
    stdout = completed.stdout or b""
    if len(stdout) > MAX_CORE_STDOUT:
        raise BridgeError("CORE_RESPONSE_INVALID", "核心输出超出上限")
    if not stdout.strip():
        raise BridgeError("CORE_UNAVAILABLE", "未取得核心响应", retryable=True)
    try:
        text = stdout.decode("utf-8")
        payload = json.loads(text)
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise BridgeError("CORE_RESPONSE_INVALID", "核心输出不是 JSON Envelope") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
        raise BridgeError("CORE_RESPONSE_INVALID", "核心 Envelope 结构无效")
    return payload


def core_argv(config: str, core_state: str, *command: str) -> list[str]:
    return ["--config", config, "--state-dir", core_state, "--connect", *command]


def core_error(payload: dict[str, Any], fallback: str) -> BridgeError:
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    code = error.get("code")
    if not isinstance(code, str) or code not in CORE_CODES:
        code = fallback
    retryable = error.get("retryable") is True or code in {
        "REMOTE_STATE_UNKNOWN", "BACKEND_UNAVAILABLE", "INSTANCE_LOCKED",
    }
    return BridgeError(code, "核心拒绝了该请求" if code == fallback else str(error.get("message") or "核心拒绝了该请求"),
                       retryable=retryable)


def require_ok(payload: dict[str, Any], fallback: str) -> dict[str, Any]:
    if payload.get("ok") is not True or not isinstance(payload.get("data"), dict):
        raise core_error(payload, fallback)
    return payload["data"]


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = path
        parent = path.parent
        check_no_symlinks(parent)
        ensure_private_dir(parent)
        if path.is_symlink():
            raise BridgeError("PATH_INVALID", "账本不能是符号链接")
        if path.exists() and (not path.is_file() or not stat.S_ISREG(path.stat().st_mode)):
            raise BridgeError("PATH_INVALID", "账本必须是普通文件")
        if path.exists() and path.stat().st_mode & 0o077:
            raise BridgeError("PATH_INVALID", "账本文件权限必须为 0600")
        flags = os.O_RDWR | os.O_NOFOLLOW
        if path.exists():
            fd = os.open(path, flags)
        else:
            try:
                fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                fd = os.open(path, flags)
        os.close(fd)
        os.chmod(path, 0o600)
        self.conn = sqlite3.connect(str(path), isolation_level=None, timeout=5)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        self._init_schema()

    def close(self) -> None:
        self.conn.close()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bindings (
                name TEXT PRIMARY KEY,
                asterun TEXT NOT NULL,
                config TEXT NOT NULL,
                core_state TEXT NOT NULL,
                location TEXT NOT NULL,
                workspaces TEXT NOT NULL,
                backends TEXT NOT NULL,
                destination TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS submissions (
                binding TEXT NOT NULL,
                request_id TEXT NOT NULL,
                input_digest TEXT NOT NULL,
                workspace TEXT NOT NULL,
                backend TEXT NOT NULL,
                conversation_id TEXT NOT NULL DEFAULT '',
                core_idempotency_key TEXT,
                task_id TEXT,
                run_id TEXT,
                result_conversation_id TEXT,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (binding, request_id),
                FOREIGN KEY (binding) REFERENCES bindings(name)
            );
            CREATE TABLE IF NOT EXISTS observations (
                binding TEXT NOT NULL,
                task_id TEXT NOT NULL,
                request_id TEXT,
                run_id TEXT,
                last_status TEXT,
                events_cursor INTEGER NOT NULL DEFAULT 0,
                observing INTEGER NOT NULL DEFAULT 1,
                poll_sequence INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (binding, task_id),
                FOREIGN KEY (binding) REFERENCES bindings(name)
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                delivery_key TEXT PRIMARY KEY,
                binding TEXT NOT NULL,
                destination TEXT NOT NULL,
                task_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                status TEXT NOT NULL,
                approval_digest TEXT NOT NULL DEFAULT '',
                delivery_state TEXT NOT NULL,
                content TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                summary TEXT NOT NULL,
                evidence TEXT NOT NULL,
                message_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (binding) REFERENCES bindings(name)
            );
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT NOT NULL,
                binding TEXT NOT NULL,
                delivery_key TEXT,
                action TEXT NOT NULL,
                note TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_deliveries_binding_state
                ON deliveries(binding, delivery_state);
            CREATE INDEX IF NOT EXISTS idx_observations_active
                ON observations(binding, observing);
            """
        )
        self.begin_write()
        try:
            columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(submissions)")}
            if "core_idempotency_key" not in columns:
                self.conn.execute("ALTER TABLE submissions ADD COLUMN core_idempotency_key TEXT")
            # A v1 intent may already have reached the core with its original raw key.
            self.conn.execute("UPDATE submissions SET core_idempotency_key=request_id WHERE core_idempotency_key IS NULL")
            columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(observations)")}
            if "poll_sequence" not in columns:
                self.conn.execute("ALTER TABLE observations ADD COLUMN poll_sequence INTEGER NOT NULL DEFAULT 0")
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(SCHEMA_VERSION),),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    def begin_write(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        self.conn.execute("COMMIT")

    def rollback(self) -> None:
        try:
            self.conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def get_binding(self, name: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM bindings WHERE name=?", (name,)).fetchone()
        if row is None:
            raise BridgeError("NOT_FOUND", "绑定不存在")
        return row

    def audit(self, binding: str, action: str, note: str, delivery_key: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO audit(at, binding, delivery_key, action, note) VALUES (?, ?, ?, ?, ?)",
            (utc_now(), binding, delivery_key, action, note),
        )


def binding_fingerprint(asterun: str, config: str, core_state: str, location: str,
                        workspaces: list[str], backends: list[str], destination: str) -> str:
    return sha256_text(canonical({
        "asterun": asterun,
        "config": config,
        "core_state": core_state,
        "location": location,
        "workspaces": workspaces,
        "backends": backends,
        "destination": destination,
    }))


def input_digest(workspace: str, backend: str, text: str, conversation_id: str) -> str:
    return sha256_text(canonical({
        "workspace": workspace,
        "backend": backend,
        "text": text,
        "conversation_id": conversation_id,
    }))


def scoped_idempotency_key(binding: sqlite3.Row, request_id: str) -> str:
    return "grokbot_" + sha256_text(canonical({
        "binding": binding["name"], "fingerprint": binding["fingerprint"], "request_id": request_id,
    }))


def approval_digest(approval: dict[str, Any] | None) -> str:
    if not isinstance(approval, dict) or approval.get("state") != "pending":
        return ""
    return sha256_text(canonical({
        "id": approval.get("id"),
        "state": approval.get("state"),
        "target_hash": approval.get("target_hash"),
        "summary": approval.get("summary"),
        "task_id": approval.get("task_id"),
        "run_id": approval.get("run_id"),
    }))


def notification_key(binding: str, destination: str, task_id: str, run_id: str,
                     status: str, approval: str) -> str:
    return sha256_text(canonical({
        "binding": binding,
        "destination": destination,
        "task_id": task_id,
        "run_id": run_id,
        "status": status,
        "approval": approval,
    }))


def public_status(task: dict[str, Any], run: dict[str, Any], approval: dict[str, Any] | None) -> str:
    if task.get("paused") is True or run.get("status") == "paused":
        return "paused"
    status = str(run.get("status") or "")
    if isinstance(approval, dict) and approval.get("state") == "pending":
        return "waiting_input"
    if isinstance(approval, dict) and approval.get("state") == "forwarding":
        return "unknown"
    if status in NOTIFY_STATUSES or status in TERMINAL_STATUSES:
        return status
    if status in {"queued", "dispatching", "running"}:
        return status
    return "unknown"


def build_material(task: dict[str, Any], run: dict[str, Any], approval: dict[str, Any] | None,
                   events: list[dict[str, Any]]) -> tuple[str, str, str]:
    summary = str(run.get("summary") or "")
    if len(summary) > 4096:
        summary = summary[:4096]
    acceptance = str(task.get("acceptance") or "pending")
    status = str(run.get("status") or "")
    lines = [
        f"状态：{status}",
        f"任务：{task.get('id')} 运行：{run.get('id')}",
        f"工作区：{task.get('workspace')} 后端：{task.get('backend')}",
        f"结果：{summary}" if summary else "结果：（无摘要）",
        f"验收：{acceptance}（运行成功不等于验收通过）",
    ]
    evidence: dict[str, Any] = {
        "task_id": task.get("id"),
        "run_id": run.get("id"),
        "status": status,
        "summary": summary,
        "acceptance": acceptance,
        "error_code": run.get("error_code"),
        "run_success_is_not_acceptance": True,
    }
    if isinstance(approval, dict) and approval.get("state") == "pending":
        lines.append(
            f"待批：{approval.get('id')} target_hash={approval.get('target_hash')} "
            f"{approval.get('summary') or ''}".rstrip()
        )
        lines.append("请使用 Asterun approval-respond，绑定实际审批 ID 与 target_hash；本适配器不自动审批。")
        evidence["approval"] = {
            "id": approval.get("id"),
            "state": approval.get("state"),
            "target_hash": approval.get("target_hash"),
            "summary": approval.get("summary"),
            "task_id": approval.get("task_id"),
            "run_id": approval.get("run_id"),
        }
    public_events = []
    for event in events[:20]:
        if not isinstance(event, dict):
            continue
        text = event.get("payload", {}).get("text") if isinstance(event.get("payload"), dict) else None
        if text is None:
            text = event.get("text")
        if isinstance(text, str) and len(text) > 200:
            text = text[:200]
        public_events.append({"type": event.get("type"), "text": text})
    if public_events:
        evidence["events"] = public_events
    content = "\n".join(lines)
    if len(content) > 16384:
        content = content[:16384]
    return content, summary or status, canonical(evidence)


def extract_ids(payload: dict[str, Any]) -> dict[str, str | None]:
    ids = payload.get("ids") if isinstance(payload.get("ids"), dict) else {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    task = data.get("task") if isinstance(data.get("task"), dict) else {}
    run = data.get("run") if isinstance(data.get("run"), dict) else {}
    session = data.get("session") if isinstance(data.get("session"), dict) else {}
    task_id = ids.get("task_id") or task.get("id")
    run_id = ids.get("run_id") or run.get("id") or task.get("current_run_id")
    conversation_id = ids.get("conversation_id") or task.get("conversation_id") or session.get("conversation_id")
    return {
        "task_id": task_id if isinstance(task_id, str) and task_id else None,
        "run_id": run_id if isinstance(run_id, str) and run_id else None,
        "conversation_id": conversation_id if isinstance(conversation_id, str) and conversation_id else None,
    }


def inspect_backends(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows = data.get("backends")
    if not isinstance(rows, list):
        raise BridgeError("CORE_RESPONSE_INVALID", "后端发现结果无效")
    result = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        name = item.get("backend") or item.get("name")
        if not isinstance(name, str):
            continue
        result.append({
            "backend": name,
            "kind": item.get("kind"),
            "enabled_in_config": item.get("enabled_in_config", item.get("enabled")),
            "is_real_connection": item.get("is_real_connection") is True,
            "authentication": item.get("authentication"),
        })
    return result


def cmd_init(ledger: Ledger, args: argparse.Namespace, timeout: float) -> dict[str, Any]:
    name = require_name(args.binding, "binding")
    asterun = require_regular_file(require_absolute(args.asterun, "asterun"), "asterun", executable=True)
    config = require_regular_file(require_absolute(args.config, "config"), "config")
    core_state = require_directory(require_absolute(args.core_state, "core-state"), "core-state")
    if args.location not in {"local", "host-cloud"}:
        raise BridgeError("INVALID_REQUEST", "location 必须是 local 或 host-cloud")
    workspaces = sorted({require_name(item, "workspace", ALIAS_RE) for item in args.workspace})
    backends = sorted({require_name(item, "backend", ALIAS_RE) for item in args.backend})
    destination = args.destination
    if not isinstance(destination, str) or not DEST_RE.fullmatch(destination):
        raise BridgeError("INVALID_REQUEST", "destination 无效")
    fingerprint = binding_fingerprint(
        str(asterun), str(config), str(core_state), args.location, workspaces, backends, destination,
    )
    version = require_ok(
        invoke_asterun(str(asterun), ["version"], timeout=timeout),
        "CORE_UNAVAILABLE",
    )
    validated = require_ok(
        invoke_asterun(str(asterun), core_argv(str(config), str(core_state), "config-validate"), timeout=timeout),
        "CORE_UNAVAILABLE",
    )
    discovered = inspect_backends(require_ok(
        invoke_asterun(str(asterun), core_argv(str(config), str(core_state), "backend-inspect"), timeout=timeout),
        "CORE_UNAVAILABLE",
    ))
    config_obj = validated.get("config") if isinstance(validated.get("config"), dict) else {}
    known_workspaces = set((config_obj.get("workspaces") or {}).keys()) if isinstance(config_obj.get("workspaces"), dict) else set()
    missing_ws = [item for item in workspaces if item not in known_workspaces]
    if missing_ws:
        raise BridgeError("SCOPE_DENIED", "工作区不在核心配置中")
    known_backends = {item["backend"] for item in discovered}
    missing_be = [item for item in backends if item not in known_backends]
    if missing_be:
        raise BridgeError("SCOPE_DENIED", "后端不在核心发现结果中")
    now = utc_now()
    ledger.begin_write()
    try:
        existing = ledger.conn.execute("SELECT * FROM bindings WHERE name=?", (name,)).fetchone()
        if existing is not None:
            if existing["fingerprint"] != fingerprint:
                raise BridgeError("BINDING_CONFLICT", "同名绑定已存在且配置或目的地不同；请使用新的 binding，旧任务保留")
            reused = True
        else:
            ledger.conn.execute(
                "INSERT INTO bindings(name, asterun, config, core_state, location, workspaces, backends, "
                "destination, fingerprint, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, str(asterun), str(config), str(core_state), args.location,
                 json.dumps(workspaces), json.dumps(backends), destination, fingerprint, now),
            )
            reused = False
        ledger.commit()
    except Exception:
        ledger.rollback()
        raise
    selected = [item for item in discovered if item["backend"] in set(backends)]
    return {
        "binding": name,
        "reused": reused,
        "location": args.location,
        "destination": destination,
        "workspaces": workspaces,
        "backends": backends,
        "installation": {
            "version": version.get("version"),
            "package": version.get("package"),
        },
        "configuration": {
            "valid": validated.get("valid") is True,
            "loaded_config_revision": validated.get("loaded_config_revision"),
            "applied_config_revision": validated.get("applied_config_revision"),
            "backends": selected,
        },
        "authenticated": False,
        "callable": False,
        "notes": "仅验证安装与配置，不表示已登录或可调用真实后端。",
    }


def cmd_submit(ledger: Ledger, args: argparse.Namespace, timeout: float) -> dict[str, Any]:
    name = require_name(args.binding, "binding")
    request_id = require_name(args.request_id, "request-id", REQUEST_RE)
    workspace = require_name(args.workspace, "workspace", ALIAS_RE)
    backend = require_name(args.backend, "backend", ALIAS_RE)
    conversation_id = args.conversation_id or ""
    if conversation_id and not REQUEST_RE.fullmatch(conversation_id):
        raise BridgeError("INVALID_REQUEST", "conversation-id 无效")
    if args.input == "-":
        text = read_limited(None, stdin=True)
    else:
        text = read_limited(Path(args.input), stdin=False)
    digest = input_digest(workspace, backend, text, conversation_id)
    binding = ledger.get_binding(name)
    allowed_ws = set(json.loads(binding["workspaces"]))
    allowed_be = set(json.loads(binding["backends"]))
    if workspace not in allowed_ws or backend not in allowed_be:
        raise BridgeError("SCOPE_DENIED", "工作区或后端不在绑定范围内")
    now = utc_now()
    ledger.begin_write()
    try:
        existing = ledger.conn.execute(
            "SELECT * FROM submissions WHERE binding=? AND request_id=?",
            (name, request_id),
        ).fetchone()
        if existing is None:
            try:
                ledger.conn.execute(
                    "INSERT INTO submissions(binding, request_id, input_digest, workspace, backend, "
                    "conversation_id, core_idempotency_key, state, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (name, request_id, digest, workspace, backend, conversation_id,
                     scoped_idempotency_key(binding, request_id), "intended", now, now),
                )
            except sqlite3.IntegrityError:
                existing = ledger.conn.execute(
                    "SELECT * FROM submissions WHERE binding=? AND request_id=?",
                    (name, request_id),
                ).fetchone()
        if existing is not None:
            if existing["input_digest"] != digest:
                raise BridgeError("IDEMPOTENCY_CONFLICT", "同一 request-id 的输入不一致")
            row = existing
        else:
            row = ledger.conn.execute(
                "SELECT * FROM submissions WHERE binding=? AND request_id=?",
                (name, request_id),
            ).fetchone()
        ledger.commit()
    except Exception:
        ledger.rollback()
        raise
    if row["task_id"]:
        return {
            "binding": name,
            "request_id": request_id,
            "reused": True,
            "workspace": workspace,
            "backend": backend,
            "task_id": row["task_id"],
            "run_id": row["run_id"],
            "conversation_id": row["result_conversation_id"],
            "submit_state": "submitted",
        }
    argv = core_argv(binding["config"], binding["core_state"], "task-submit",
                     "--workspace", workspace, "--backend", backend,
                     "--input", "-", "--idempotency-key", row["core_idempotency_key"])
    if conversation_id:
        argv.extend(["--conversation-id", conversation_id])
    try:
        payload = invoke_asterun(binding["asterun"], argv, stdin_text=text, timeout=max(timeout, SUBMIT_TIMEOUT) if timeout == DEFAULT_TIMEOUT else timeout)
    except BridgeError as error:
        if error.code in {"CORE_TIMEOUT", "CORE_RESPONSE_INVALID", "CORE_UNAVAILABLE"}:
            error = BridgeError("CORE_RESULT_UNKNOWN", "提交结果不明，已保留原幂等键，请用同一 request-id 回读", retryable=True)
        raise error
    if payload.get("ok") is not True:
        raise core_error(payload, "CORE_RESULT_UNKNOWN")
    ids = extract_ids(payload)
    if ids["task_id"] is None or ids["run_id"] is None:
        raise BridgeError("CORE_RESULT_UNKNOWN", "提交结果不明，已保留原幂等键，请用同一 request-id 回读", retryable=True)
    now = utc_now()
    ledger.begin_write()
    try:
        current = ledger.conn.execute(
            "SELECT * FROM submissions WHERE binding=? AND request_id=?", (name, request_id),
        ).fetchone()
        if current["task_id"]:
            if (current["task_id"], current["run_id"], current["result_conversation_id"]) != (
                ids["task_id"], ids["run_id"], ids["conversation_id"]
            ):
                raise BridgeError("CORE_RESPONSE_INVALID", "并发提交响应与已保存任务引用不一致，保留原记录")
            # Another request already recorded the response; do not rewind its observation.
            ledger.commit()
            return {
                "binding": name, "request_id": request_id, "reused": True,
                "workspace": workspace, "backend": backend, **ids, "submit_state": "submitted",
            }
        ledger.conn.execute(
            "UPDATE submissions SET task_id=?, run_id=?, result_conversation_id=?, state=?, updated_at=? "
            "WHERE binding=? AND request_id=? AND input_digest=?",
            (ids["task_id"], ids["run_id"], ids["conversation_id"], "submitted", now, name, request_id, digest),
        )
        ledger.conn.execute(
            "INSERT INTO observations(binding, task_id, request_id, run_id, last_status, events_cursor, observing, updated_at, poll_sequence) "
            "VALUES (?, ?, ?, ?, ?, 0, 1, ?, "
            "(SELECT COALESCE(MAX(poll_sequence), 0) + 1 FROM observations WHERE binding=?)) "
            "ON CONFLICT(binding, task_id) DO NOTHING",
            (name, ids["task_id"], request_id, ids["run_id"], None, now, name),
        )
        ledger.commit()
    except Exception:
        ledger.rollback()
        raise
    return {
        "binding": name,
        "request_id": request_id,
        "reused": False,
        "workspace": workspace,
        "backend": backend,
        "task_id": ids["task_id"],
        "run_id": ids["run_id"],
        "conversation_id": ids["conversation_id"],
        "submit_state": "submitted",
    }


def _task_events(binding: sqlite3.Row, task_id: str, run_id: str, cursor: int,
                 timeout: float) -> tuple[list[dict[str, Any]], int]:
    payload = invoke_asterun(
        binding["asterun"],
        core_argv(binding["config"], binding["core_state"], "task-events", task_id,
                  "--cursor", str(cursor), "--page-size", "50"),
        timeout=timeout,
    )
    data = require_ok(payload, "CORE_UNAVAILABLE")
    if data.get("task_id") != task_id or data.get("run_id") != run_id:
        raise BridgeError("CORE_SNAPSHOT_CHANGED", "事件与任务快照所属运行不同，下一次轮询重新读取", retryable=True)
    events = data.get("events")
    next_cursor = data.get("next_cursor")
    if (not isinstance(events, list) or any(not isinstance(item, dict) for item in events)
            or type(next_cursor) is not int or next_cursor < cursor
            or (events and next_cursor <= cursor)):
        raise BridgeError("CORE_RESPONSE_INVALID", "事件页或游标无效")
    return events, next_cursor


def cmd_poll(ledger: Ledger, args: argparse.Namespace, timeout: float) -> dict[str, Any]:
    name = require_name(args.binding, "binding")
    limit = args.limit
    if type(limit) is not int or not 1 <= limit <= 100:
        raise BridgeError("INVALID_REQUEST", "limit 必须是 1 到 100")
    if not math.isfinite(timeout) or timeout <= 0:
        raise BridgeError("INVALID_REQUEST", "timeout 必须为有限正数")
    deadline = time.monotonic() + timeout
    binding = ledger.get_binding(name)
    rows = ledger.conn.execute(
        "SELECT * FROM observations WHERE binding=? AND observing=1 "
        "ORDER BY poll_sequence ASC, task_id ASC LIMIT ?", (name, limit),
    ).fetchall()
    observed: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    def remaining() -> float:
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise BridgeError("CORE_TIMEOUT", "本次轮询已到截止时间", retryable=True)
        return min(budget, POLL_ITEM_TIMEOUT)

    for selected in rows:
        if time.monotonic() >= deadline:
            break
        task_id = selected["task_id"]
        # Rotate only attempted items, before I/O, including failed requests. A sequence
        # also identifies stale results from an overlapping poll of the same task.
        ledger.begin_write()
        try:
            row = ledger.conn.execute(
                "SELECT * FROM observations WHERE binding=? AND task_id=?", (name, task_id),
            ).fetchone()
            if not row["observing"] or row["poll_sequence"] != selected["poll_sequence"]:
                ledger.commit()
                continue
            sequence = ledger.conn.execute(
                "SELECT COALESCE(MAX(poll_sequence), 0) + 1 FROM observations WHERE binding=?", (name,),
            ).fetchone()[0]
            ledger.conn.execute(
                "UPDATE observations SET poll_sequence=?, updated_at=? WHERE binding=? AND task_id=?",
                (sequence, utc_now(), name, task_id),
            )
            ledger.commit()
        except Exception:
            ledger.rollback()
            raise
        try:
            payload = invoke_asterun(
                binding["asterun"],
                core_argv(binding["config"], binding["core_state"], "task-get", task_id),
                timeout=remaining(),
            )
            data = require_ok(payload, "CORE_UNAVAILABLE")
            task = data.get("task") if isinstance(data.get("task"), dict) else {}
            run = data.get("run") if isinstance(data.get("run"), dict) else {}
            approval = data.get("approval") if isinstance(data.get("approval"), dict) else None
            if (task.get("id") != task_id or not isinstance(run.get("id"), str) or not run["id"]
                    or run.get("task_id", task_id) != task_id):
                raise BridgeError("CORE_RESPONSE_INVALID", "任务快照引用不匹配")
            submission = ledger.conn.execute(
                "SELECT workspace, backend FROM submissions WHERE binding=? AND request_id=?",
                (name, row["request_id"]),
            ).fetchone()
            if (submission is None or task.get("workspace") != submission["workspace"]
                    or task.get("backend") != submission["backend"]):
                raise BridgeError("SCOPE_DENIED", "任务快照与已登记的工作区或后端不匹配")
            run_id = run["id"]
            if approval and (approval.get("task_id") != task_id or approval.get("run_id") != run_id):
                raise BridgeError("CORE_RESPONSE_INVALID", "审批引用与任务运行不匹配")
            cursor = 0 if run_id != row["run_id"] else int(row["events_cursor"] or 0)
            events, next_cursor = _task_events(binding, task_id, run_id, cursor, remaining())
            status = public_status(task, run, approval)
            digest = approval_digest(approval)
            created = False
            delivery_key = None
            notify = status in NOTIFY_STATUSES or status == "unknown"
            if notify:
                delivery_key = notification_key(name, binding["destination"], task_id, run_id, status, digest)
                content, summary, evidence = build_material(task, run, approval, events)
            now = utc_now()
            ledger.begin_write()
            try:
                current = ledger.conn.execute(
                    "SELECT * FROM observations WHERE binding=? AND task_id=?", (name, task_id),
                ).fetchone()
                if not current["observing"] or current["poll_sequence"] != sequence:
                    ledger.commit()
                    continue
                ledger.conn.execute(
                    "UPDATE deliveries SET delivery_state='superseded', updated_at=? "
                    "WHERE binding=? AND task_id=? AND delivery_state='pending' "
                    "AND (? IS NULL OR delivery_key<>?)",
                    (now, name, task_id, delivery_key, delivery_key),
                )
                observing = 1
                if notify:
                    ledger.conn.execute(
                        "INSERT OR IGNORE INTO deliveries(delivery_key, binding, destination, task_id, run_id, "
                        "status, approval_digest, delivery_state, content, content_sha256, summary, evidence, "
                        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)",
                        (delivery_key, name, binding["destination"], task_id, run_id, status,
                         digest, content, sha256_text(content), summary, evidence, now, now),
                    )
                    created = ledger.conn.execute("SELECT changes()").fetchone()[0] > 0
                    # An unsent state may become current again (for example, auth
                    # waiting after reconnect). It is eligible only after this readback.
                    ledger.conn.execute(
                        "UPDATE deliveries SET delivery_state='pending', content=?, content_sha256=?, "
                        "summary=?, evidence=?, updated_at=? WHERE delivery_key=? AND delivery_state='superseded'",
                        (content, sha256_text(content), summary, evidence, now, delivery_key),
                    )
                    created = created or ledger.conn.execute("SELECT changes()").fetchone()[0] > 0
                    delivered = ledger.conn.execute(
                        "SELECT delivery_state FROM deliveries WHERE delivery_key=?", (delivery_key,),
                    ).fetchone()
                    if status in TERMINAL_STATUSES and delivered["delivery_state"] == "host_reported_delivered":
                        observing = 0
                ledger.conn.execute(
                    "UPDATE observations SET run_id=?, last_status=?, events_cursor=?, observing=?, updated_at=? "
                    "WHERE binding=? AND task_id=?",
                    (run_id, status, next_cursor, observing, now, name, task_id),
                )
                ledger.commit()
            except Exception:
                ledger.rollback()
                raise
            observed.append({
                "task_id": task_id, "run_id": run_id, "status": status,
                "delivery_key": delivery_key, "notification_created": created,
            })
        except BridgeError as error:
            errors.append({"task_id": task_id, "code": error.code})
    active = ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM observations WHERE binding=? AND observing=1", (name,),
    ).fetchone()["n"]
    pending = ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM deliveries WHERE binding=? AND delivery_state='pending'", (name,),
    ).fetchone()["n"]
    sending = ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM deliveries WHERE binding=? AND delivery_state='sending'", (name,),
    ).fetchone()["n"]
    return {
        "binding": name,
        "needs_followup": active > 0 or pending > 0 or sending > 0,
        "active_count": active,
        "pending_delivery": pending,
        "unconfirmed": sending,
        "observed": observed,
        "errors": errors,
    }


def _delivery_view(row: sqlite3.Row) -> dict[str, Any]:
    evidence = json.loads(row["evidence"]) if row["evidence"] else {}
    return {
        "delivery_key": row["delivery_key"],
        "destination": row["destination"],
        "task_id": row["task_id"],
        "run_id": row["run_id"],
        "status": row["status"],
        "delivery_state": row["delivery_state"],
        "content": row["content"],
        "content_sha256": row["content_sha256"],
        "summary": row["summary"],
        "evidence": evidence,
        "message_id": row["message_id"],
        "run_success_is_not_acceptance": True,
    }


def cmd_inbox(ledger: Ledger, args: argparse.Namespace, timeout: float) -> dict[str, Any]:
    name = require_name(args.binding, "binding")
    ledger.get_binding(name)
    if type(args.limit) is not int or not 1 <= args.limit <= 100:
        raise BridgeError("INVALID_REQUEST", "limit 必须是 1 到 100")
    rows = ledger.conn.execute(
        "SELECT * FROM deliveries WHERE binding=? AND delivery_state IN ('pending', 'sending') "
        "ORDER BY created_at ASC LIMIT ?",
        (name, args.limit),
    ).fetchall()
    return {
        "binding": name,
        "items": [_delivery_view(row) for row in rows],
        "count": len(rows),
    }


def cmd_claim(ledger: Ledger, args: argparse.Namespace, timeout: float) -> dict[str, Any]:
    name = require_name(args.binding, "binding")
    key = args.delivery_key
    if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
        raise BridgeError("INVALID_REQUEST", "delivery-key 无效")
    ledger.get_binding(name)
    now = utc_now()
    ledger.begin_write()
    try:
        row = ledger.conn.execute(
            "SELECT * FROM deliveries WHERE delivery_key=? AND binding=?",
            (key, name),
        ).fetchone()
        if row is None:
            raise BridgeError("NOT_FOUND", "交付记录不存在")
        if row["delivery_state"] == "superseded":
            raise BridgeError("DELIVERY_STATE", "该通知已被新的任务状态替代")
        already = row["delivery_state"] != "pending"
        if row["delivery_state"] == "pending":
            ledger.conn.execute(
                "UPDATE deliveries SET delivery_state='sending', updated_at=? "
                "WHERE delivery_key=? AND delivery_state='pending'",
                (now, key),
            )
            ledger.audit(name, "claim", "pending_to_sending", key)
            row = ledger.conn.execute("SELECT * FROM deliveries WHERE delivery_key=?", (key,)).fetchone()
        ledger.commit()
    except Exception:
        ledger.rollback()
        raise
    view = _delivery_view(row)
    view["already_claimed"] = already
    return view


def cmd_ack(ledger: Ledger, args: argparse.Namespace, timeout: float) -> dict[str, Any]:
    name = require_name(args.binding, "binding")
    key = args.delivery_key
    message_id = args.message_id
    digest = args.content_sha256
    if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
        raise BridgeError("INVALID_REQUEST", "delivery-key 无效")
    if not isinstance(message_id, str) or not DEST_RE.fullmatch(message_id):
        raise BridgeError("INVALID_REQUEST", "message-id 无效")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise BridgeError("INVALID_REQUEST", "content-sha256 无效")
    ledger.get_binding(name)
    now = utc_now()
    ledger.begin_write()
    try:
        row = ledger.conn.execute(
            "SELECT * FROM deliveries WHERE delivery_key=? AND binding=?",
            (key, name),
        ).fetchone()
        if row is None:
            raise BridgeError("NOT_FOUND", "交付记录不存在")
        if row["content_sha256"] != digest:
            raise BridgeError("HASH_MISMATCH", "内容摘要与待发内容不一致")
        if row["delivery_state"] == "host_reported_delivered":
            if row["message_id"] != message_id:
                raise BridgeError("DELIVERY_STATE", "已记录的回执不匹配")
            reused = True
        elif row["delivery_state"] != "sending":
            raise BridgeError("DELIVERY_STATE", "尚未领取，不能确认交付")
        else:
            ledger.conn.execute(
                "UPDATE deliveries SET delivery_state='host_reported_delivered', message_id=?, updated_at=? "
                "WHERE delivery_key=?",
                (message_id, now, key),
            )
            ledger.audit(name, "host_reported_delivered", "host receipt recorded", key)
            # A receipt only confirms delivery. The next poll checks the current run
            # before retiring observation, since a new run may have started meanwhile.
            reused = False
        row = ledger.conn.execute("SELECT * FROM deliveries WHERE delivery_key=?", (key,)).fetchone()
        ledger.commit()
    except Exception:
        ledger.rollback()
        raise
    return {
        "delivery_key": key,
        "delivery_state": "host_reported_delivered",
        "host_reported_delivered": True,
        "core_acknowledged": False,
        "chat_visibility": "not_verified",
        "exactly_once": False,
        "reused": reused,
        "message_id": message_id,
    }


def cmd_resolve(ledger: Ledger, args: argparse.Namespace, timeout: float) -> dict[str, Any]:
    name = require_name(args.binding, "binding")
    key = args.delivery_key
    if not args.not_sent:
        raise BridgeError("INVALID_REQUEST", "resolve 需要 --not-sent，表示操作者声明宿主已确认未发出")
    if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
        raise BridgeError("INVALID_REQUEST", "delivery-key 无效")
    ledger.get_binding(name)
    now = utc_now()
    ledger.begin_write()
    try:
        row = ledger.conn.execute(
            "SELECT * FROM deliveries WHERE delivery_key=? AND binding=?",
            (key, name),
        ).fetchone()
        if row is None:
            raise BridgeError("NOT_FOUND", "交付记录不存在")
        if row["delivery_state"] != "sending":
            raise BridgeError("DELIVERY_STATE", "只有未确认的发送可以恢复为待发送")
        ledger.conn.execute(
            "UPDATE deliveries SET delivery_state='pending', updated_at=? WHERE delivery_key=? AND delivery_state='sending'",
            (now, key),
        )
        ledger.audit(name, "operator_declared_not_sent", "sending_to_pending", key)
        ledger.commit()
    except Exception:
        ledger.rollback()
        raise
    return {
        "delivery_key": key,
        "delivery_state": "pending",
        "note": "已按操作者声明恢复为待发送，不是自动推测的发送结果。",
    }


def cmd_status(ledger: Ledger, args: argparse.Namespace, timeout: float) -> dict[str, Any]:
    name = require_name(args.binding, "binding")
    binding = ledger.get_binding(name)
    tasks = ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM observations WHERE binding=?", (name,),
    ).fetchone()["n"]
    active = ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM observations WHERE binding=? AND observing=1", (name,),
    ).fetchone()["n"]
    pending = ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM deliveries WHERE binding=? AND delivery_state='pending'", (name,),
    ).fetchone()["n"]
    sending = ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM deliveries WHERE binding=? AND delivery_state='sending'", (name,),
    ).fetchone()["n"]
    delivered = ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM deliveries WHERE binding=? AND delivery_state='host_reported_delivered'",
        (name,),
    ).fetchone()["n"]
    return {
        "binding": name,
        "location": binding["location"],
        "destination": binding["destination"],
        "workspaces": json.loads(binding["workspaces"]),
        "backends": json.loads(binding["backends"]),
        "asterun": binding["asterun"],
        "config": binding["config"],
        "core_state": binding["core_state"],
        "tasks": {"total": tasks, "observing": active},
        "deliveries": {
            "pending": pending,
            "sending": sending,
            "host_reported_delivered": delivered,
        },
        "unconfirmed": sending,
        "needs_followup": active > 0 or pending > 0 or sending > 0,
    }


COMMANDS = {
    "init": cmd_init,
    "submit": cmd_submit,
    "poll": cmd_poll,
    "inbox": cmd_inbox,
    "claim": cmd_claim,
    "ack": cmd_ack,
    "resolve": cmd_resolve,
    "status": cmd_status,
}


def build_parser() -> BridgeParser:
    parser = BridgeParser(
        prog="bridge.py",
        description="GrokBot 可选宿主的 Asterun 提交、观察与交付账本。不发送消息，不自动审批。",
    )
    parser.add_argument("--ledger", required=True, help="私有 SQLite 账本路径")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="核心命令时限（秒）")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="绑定核心实例和允许范围，只验证安装与配置")
    init.add_argument("--binding", required=True)
    init.add_argument("--asterun", required=True)
    init.add_argument("--config", required=True)
    init.add_argument("--core-state", required=True)
    init.add_argument("--location", required=True, choices=("local", "host-cloud"))
    init.add_argument("--workspace", required=True, action="append")
    init.add_argument("--backend", required=True, action="append")
    init.add_argument("--destination", required=True)
    submit = sub.add_parser("submit", help="按幂等键登记并提交任务")
    submit.add_argument("--binding", required=True)
    submit.add_argument("--request-id", required=True)
    submit.add_argument("--workspace", required=True)
    submit.add_argument("--backend", required=True)
    submit.add_argument("--input", required=True, help="提示文件或 - 表示 stdin")
    submit.add_argument("--conversation-id")
    poll = sub.add_parser("poll", help="有界读取已登记任务并生成去重通知")
    poll.add_argument("--binding", required=True)
    poll.add_argument("--limit", type=int, default=20)
    inbox = sub.add_parser("inbox", help="列出待发送或待确认通知")
    inbox.add_argument("--binding", required=True)
    inbox.add_argument("--limit", type=int, default=20)
    claim = sub.add_parser("claim", help="领取待发通知；sending 不会被当成可自动重发")
    claim.add_argument("--binding", required=True)
    claim.add_argument("--delivery-key", required=True)
    ack = sub.add_parser("ack", help="记录宿主自报的发送回执，不表示核心 ack 或聊天可见")
    ack.add_argument("--binding", required=True)
    ack.add_argument("--delivery-key", required=True)
    ack.add_argument("--message-id", required=True)
    ack.add_argument("--content-sha256", required=True)
    resolve = sub.add_parser(
        "resolve",
        help="仅在操作者声明宿主已确认未发出时，将 sending 恢复为 pending；不会自动推测发送结果",
    )
    resolve.add_argument("--binding", required=True)
    resolve.add_argument("--delivery-key", required=True)
    resolve.add_argument(
        "--not-sent",
        action="store_true",
        required=True,
        help="操作者声明：已向宿主查询并确认该消息未发出。不是自动推测。",
    )
    status = sub.add_parser("status", help="查看绑定、任务计数和未确认交付")
    status.add_argument("--binding", required=True)
    return parser


def _main(argv: list[str] | None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    timeout = args.timeout
    if type(timeout) is not float and type(timeout) is not int:
        raise BridgeError("INVALID_REQUEST", "timeout 无效")
    timeout = float(timeout)
    if not math.isfinite(timeout) or timeout <= 0:
        raise BridgeError("INVALID_REQUEST", "timeout 必须为有限正数")
    ledger_path = require_absolute(args.ledger, "ledger")
    ledger = Ledger(ledger_path)
    try:
        data = COMMANDS[args.command](ledger, args, timeout)
        emit(True, data, None)
        return 0
    finally:
        ledger.close()


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except HelpRequested:
        emit(True, {"help": True}, None)
        return 0
    except BridgeError as error:
        emit(False, None, error.to_dict())
        return 2
    except Exception:
        emit(False, None, {"code": "INTERNAL_ERROR", "message": "适配器内部错误", "retryable": False})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
