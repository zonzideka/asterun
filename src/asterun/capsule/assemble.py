"""从任务、运行和只读 Git 状态组装续段胶囊。

不写库、不改工作区、不派发。同一组权威记录得到同一胶囊。
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from copy import deepcopy
from pathlib import Path

from asterun.capsule.model import (
    build_capsule,
    canonical,
    extract_notes,
    sha,
    validate_capsule,
)
from asterun.context_transition import contract
from asterun.handoff import state
from asterun.redact import redact_text

_PAGE_SIZE = 100
_MAX_DIRTY = 500
_GIT_TIMEOUT_SECONDS = 10
_TERMINAL_TOOL_STATUSES = frozenset({"completed", "failed"})
_FILE_INPUT_KEYS = ("path", "file_path", "target_file")
_RESULT_KINDS = frozenset({"tool_call_update", "tool_result"})
_PROGRAM_RE = re.compile(r"^[A-Za-z0-9._+-]{1,64}$")
_NOTE_PREFIX = "ASTERUN-NOTE:"
_NOTE_DROP_RES = (
    re.compile(
        r"(?i)(password|passwd|secret|token|api[_-]?key|authorization|bearer|cookie|private[_-]?key)\s*[:=]"
    ),
    re.compile(r"(?i)bearer\s+\S+"),
    re.compile(r"[A-Za-z0-9_\-]{32,}"),
    re.compile(r"-----BEGIN"),
)
# 白名单用排除字符，而不是允许字符：中文和普通标点要能留下。
_NOTE_FORBIDDEN_CHARS = frozenset((
    "@", "=", "{", "}", "[", "]", "<", ">", "$", "`",
    ";", "|", "(", ")", '"', "'", "\\",
))
_NOTE_KEYWORDS = (
    "def", "class", "import", "from", "return", "function", "const", "let",
    "var", "lambda", "export", "password", "passwd", "secret", "token",
    "apikey", "api_key", "bearer", "authorization", "cookie",
)
_NOTE_KEYWORD_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    + "|".join(re.escape(word) for word in sorted(_NOTE_KEYWORDS, key=len, reverse=True))
    + r")(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_NOTE_LONG_RUN_RE = re.compile(r"[A-Za-z0-9_+/-]{20,}")
_EMPTY_WORKSPACE = {
    "head": None,
    "branch": None,
    "dirty_files": None,
    "diff_stat": None,
}


class _GitFailed(Exception):
    pass


def mandatory_for(app, task) -> dict:
    """任务契约里必须原样进入胶囊的部分。约束留在 objective 原文中。"""
    quality = task.quality if isinstance(getattr(task, "quality", None), dict) else {}
    checks = quality.get("checks", [])
    if not isinstance(checks, list):
        checks = []
    return {
        "objective": task.text if isinstance(task.text, str) else str(task.text or ""),
        "constraints": [],
        "acceptance": deepcopy(checks),
        "review_requirements": {
            "require_review": bool(app.config.workflow.require_review or task.external_require_review),
            "review_status": task.review_status,
        },
        "findings": _findings(getattr(task, "findings", None) or []),
        "limits": {
            "max_runs_per_task": app.config.scheduler.max_runs_per_task,
            "max_repairs": app.config.workflow.max_repairs,
            "repair_count": task.repair_count,
        },
    }


def workspace_for(root) -> dict:
    """只读探测工作区。失败时不抛错，返回 head 为空的错误结构。"""
    root = Path(root)
    # 只认工作区根自己的 .git。父目录是仓库时，不能把父仓库的状态算进这个工作区。
    if not root.is_dir() or not (root / ".git").exists():
        return {**_EMPTY_WORKSPACE, "error": "not_git"}
    try:
        inside = _git(root, ["rev-parse", "--is-inside-work-tree"])
    except _GitFailed as exc:
        code = exc.args[0] if exc.args in {("not_git",), ("git_failed",)} else "git_failed"
        return {**_EMPTY_WORKSPACE, "error": code}
    if inside != "true":
        return {**_EMPTY_WORKSPACE, "error": "not_git"}
    try:
        snapshot = {
            "head": _git(root, ["rev-parse", "HEAD"]),
            "branch": _git(root, ["rev-parse", "--abbrev-ref", "HEAD"]),
            "dirty_files": _dirty_files(root, _git_bytes(root, ["status", "--porcelain=v1", "-z"])),
            "diff_stat": _diff_stat(_git(root, ["diff", "--shortstat", "HEAD"])),
        }
    except _GitFailed:
        return {**_EMPTY_WORKSPACE, "error": "git_failed"}
    snapshot["binding_hash"] = sha(snapshot)
    return snapshot


def progress_for(app, task, run) -> tuple[dict, dict, list[str]]:
    """段列表只含 created_at 不晚于源运行的记录；文件、命令和笔记只取当前运行的事件。"""
    segments = []
    history = [item for item in _runs(app, task) if item.created_at <= run.created_at]
    for index, item in enumerate(history):
        native = item.native if isinstance(item.native, dict) else {}
        num_turns = native.get("num_turns", 0)
        if isinstance(num_turns, bool) or not isinstance(num_turns, int):
            num_turns = 0
        status = getattr(item.status, "value", item.status)
        segments.append({
            "index": index,
            "run_id": item.id.value,
            "stop_reason": native.get("stop_reason"),
            "num_turns": num_turns,
            "status": str(status),
        })
    files: list[str] = []
    commands: list[dict] = []
    texts: list[str] = []
    calls: dict[str, dict] = {}
    order: list[str] = []
    for event in _events(app.store, run.id):
        payload = event.payload if isinstance(getattr(event, "payload", None), dict) else {}
        kind = payload.get("kind")
        if kind == "text":
            text = payload.get("text")
            if isinstance(text, str):
                texts.append(text)
            continue
        if kind == "tool_call":
            tool_input = payload.get("input") if isinstance(payload.get("input"), dict) else {}
            for key in _FILE_INPUT_KEYS:
                value = tool_input.get(key)
                if isinstance(value, str) and value:
                    files.append(value)
            command = tool_input.get("command")
            if isinstance(command, str):
                commands.append(_command_record(command))
            call_id = payload.get("toolCallId")
            if not isinstance(call_id, str) or not call_id:
                call_id = f"anon:{getattr(event, 'seq', len(order))}"
            if call_id not in calls:
                order.append(call_id)
            calls[call_id] = {
                "tool": payload.get("toolName") if isinstance(payload.get("toolName"), str) else "",
                "input": payload.get("input") if "input" in payload else None,
                "status": payload.get("status") if isinstance(payload.get("status"), str) else "",
                "saw_result": False,
            }
            continue
        if kind in _RESULT_KINDS:
            call_id = payload.get("toolCallId")
            record = calls.get(call_id) if isinstance(call_id, str) else None
            if record is None:
                continue
            record["saw_result"] = True
            if isinstance(payload.get("status"), str):
                record["status"] = payload["status"]
    unconfirmed = []
    for call_id in order:
        record = calls[call_id]
        # 后续已经有 update/result，或工具自己已是终态，都不再当成未确认动作。
        if record["saw_result"] or record["status"] in _TERMINAL_TOOL_STATUSES:
            continue
        unconfirmed.append({
            "tool": record["tool"],
            "input_digest": sha(record["input"]),
            "reason": "no_result",
        })
    approval = app.store.find_pending_approval(run.id)
    approvals = []
    if approval is not None:
        approvals.append({
            "action_sha256": sha(approval.to_dict()),
            "summary": "pending approval",
        })
    progress = {
        "segments": segments,
        "files_touched": files,
        "commands": commands,
        "checks": [],
    }
    open_items = {
        "approvals_not_revived": approvals,
        "unconfirmed_actions": unconfirmed,
        "open_questions": [],
    }
    return progress, open_items, texts


def assemble_capsule(app, task, run, *, trigger, segment_index=None) -> dict:
    """把权威记录装进胶囊。created_at 用源运行时间，避免墙钟破坏确定性。"""
    if segment_index is None:
        segment_index = segment_index_for(app, task, run)
    progress, open_items, texts = progress_for(app, task, run)
    notes_text, dropped_lines = _redacted_notes(texts)
    authority = {
        "contract_sha256": contract(task),
        **state(app.store, task, run),
        "config_revision": task.config_revision,
    }
    return build_capsule(
        task_id=task.id.value,
        source_run_id=run.id.value,
        segment_index=segment_index,
        role=run.role,
        backend_kind=app.config.get_backend(run.backend.value).kind,
        trigger=trigger,
        created_at=run.created_at,
        authority=authority,
        mandatory=mandatory_for(app, task),
        workspace=workspace_for(app.config.get_workspace(task.workspace).root),
        progress=progress,
        open_items=open_items,
        notes_text=notes_text,
        dropped_lines=dropped_lines,
        budget_ref={"budget_id": None, "ledger_revision": 0},
    )


def verify_capsule(app, task, capsule) -> list[str]:
    """胶囊与当前任务契约不一致时，在模型校验结果后追加 contract_changed。"""
    problems = list(validate_capsule(capsule, expected_mandatory=mandatory_for(app, task)))
    stored = None
    if isinstance(capsule, dict):
        authority = capsule.get("authority")
        if isinstance(authority, dict):
            stored = authority.get("contract_sha256")
    if stored != contract(task):
        problems.append("contract_changed")
    return problems


def _findings(items) -> list[dict]:
    findings = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            findings.append({"id": f"idx{index}", "text": _json_text(item)})
            continue
        finding = deepcopy(item)
        if "text" not in finding:
            finding["text"] = _json_text(item)
        elif not isinstance(finding["text"], str):
            finding["text"] = _json_text(finding["text"])
        if "id" not in finding:
            finding["id"] = f"idx{index}"
        findings.append(finding)
    return findings


def _json_text(value) -> str:
    try:
        return canonical(value).decode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        return canonical(str(value)).decode("utf-8")


def _runs(app, task):
    runs = list(app.store.list_runs(task.id))
    runs.sort(key=lambda item: (item.created_at, item.id.value))
    return runs


def segment_index_for(app, task, run) -> int:
    ids = [r.id for r in _runs(app, task)]
    return ids.index(run.id) if run.id in ids else len(ids) - 1


def _events(store, run_id):
    cursor = 0
    while True:
        page = store.list_events(run_id, cursor=cursor, page_size=_PAGE_SIZE)
        if not page:
            return
        for event in page:
            yield event
        last = page[-1].seq
        if not isinstance(last, int) or last <= cursor:
            return
        cursor = last


def _command_program(command: str) -> str | None:
    """第一个 token 的 basename。不像程序名时不保留 token 本身。"""
    pieces = command.split(maxsplit=1)
    if not pieces:
        return None
    base = pieces[0].rsplit("/", 1)[-1]
    if _PROGRAM_RE.fullmatch(base):
        return base
    return None


def _command_record(command: str) -> dict:
    return {
        "program": _command_program(command),
        "sha256": hashlib.sha256(command.encode("utf-8")).hexdigest(),
        "length": len(command),
    }


def safe_note_line(line: str) -> str | None:
    """去前缀并脱敏后，只有同时满足白名单的一行才保留。

    行首关键字按前缀判断，文中关键字按独立标识符判断。大小写不敏感。
    凭据模式仍整行丢弃。不合格返回 None，调用方只计数。
    """
    if not isinstance(line, str):
        return None
    body = line.strip()
    if body.startswith(_NOTE_PREFIX):
        body = body[len(_NOTE_PREFIX):].strip()
    if "\n" in body or "\r" in body:
        return None
    redacted = redact_text(body)
    if not 1 <= len(redacted) <= 200:
        return None
    if any(char in _NOTE_FORBIDDEN_CHARS for char in redacted):
        return None
    if "://" in redacted.lower():
        return None
    if _NOTE_LONG_RUN_RE.search(redacted):
        return None
    head = redacted.lstrip().lower()
    if any(head.startswith(keyword) for keyword in _NOTE_KEYWORDS):
        return None
    if _NOTE_KEYWORD_RE.search(redacted):
        return None
    if any(pattern.search(redacted) for pattern in _NOTE_DROP_RES):
        return None
    return redacted


def _redacted_notes(texts: list[str]) -> tuple[str | None, int]:
    """笔记正文只经 safe_note_line。丢弃的行只计数。"""
    extracted = extract_notes(texts)
    if extracted is None:
        return None, 0
    kept: list[str] = []
    dropped = 0
    for line in extracted.splitlines():
        safe = safe_note_line(line)
        if safe is None:
            dropped += 1
            continue
        kept.append(safe)
    return "\n".join(kept), dropped


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    return env


def _run_git(root: Path, args: list[str]) -> subprocess.CompletedProcess:
    command = [
        "git",
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "diff.external=",
        *args,
    ]
    try:
        return subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
            env=_git_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise _GitFailed("git_failed") from exc
    except OSError as exc:
        raise _GitFailed("git_failed") from exc


def _git(root: Path, args: list[str]) -> str:
    completed = _run_git(root, args)
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").lower()
        if "not a git repository" in detail or "not a git repo" in detail:
            raise _GitFailed("not_git")
        raise _GitFailed("git_failed")
    return completed.stdout.decode("utf-8", errors="replace").strip()


def _git_bytes(root: Path, args: list[str]) -> bytes:
    completed = _run_git(root, args)
    if completed.returncode != 0:
        raise _GitFailed("git_failed")
    return completed.stdout


def _dirty_files(root: Path, raw: bytes) -> list[dict]:
    parts = raw.split(b"\0")
    if parts and parts[-1] == b"":
        parts.pop()
    items = []
    index = 0
    while index < len(parts) and len(items) < _MAX_DIRTY:
        entry = parts[index]
        index += 1
        if len(entry) < 4 or entry[2:3] != b" ":
            continue
        status = entry[:2].decode("ascii", errors="replace")
        # porcelain v1 -z 把重命名写成「新路径 NUL 旧路径」，第一条才是当前文件。
        path = entry[3:].decode("utf-8", errors="replace")
        if "R" in status or "C" in status:
            if index >= len(parts):
                continue
            index += 1
        if not path:
            continue
        item = {"path": path, "status": status}
        digest = _file_sha256(root, path)
        if digest is not None:
            item["sha256"] = digest
        items.append(item)
    return items


def _file_sha256(root: Path, relative: str) -> str | None:
    candidate = Path(relative)
    if candidate.is_absolute():
        return None
    try:
        resolved_root = root.resolve()
        resolved = (root / candidate).resolve()
        resolved.relative_to(resolved_root)
    except (OSError, ValueError):
        return None
    if not resolved.is_file():
        return None
    digest = hashlib.sha256()
    try:
        with resolved.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _diff_stat(text: str) -> dict:
    body = text.strip()
    if not body:
        return {"files": 0, "insertions": 0, "deletions": 0}
    files = re.search(r"(\d+) files? changed", body)
    if files is None:
        raise _GitFailed("git_failed")
    insertions = re.search(r"(\d+) insertions?\(\+\)", body)
    deletions = re.search(r"(\d+) deletions?\(-\)", body)
    return {
        "files": int(files.group(1)),
        "insertions": int(insertions.group(1)) if insertions else 0,
        "deletions": int(deletions.group(1)) if deletions else 0,
    }
