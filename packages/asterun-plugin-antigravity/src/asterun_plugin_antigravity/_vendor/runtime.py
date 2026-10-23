"""Antigravity CLI 双向 NDJSON 运行器；没有原生终态就保留未知结果。"""

from __future__ import annotations

import json
import math
import os
import re
import selectors
import signal
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .compat import BACKEND_UNAVAILABLE, REMOTE_STATE_UNKNOWN, RunId

DISPATCH_RECEIPT_KEY = "asterun.antigravity_dispatch_v1"
ANTIGRAVITY_RUN_INCOMPLETE = "ANTIGRAVITY_RUN_INCOMPLETE"
ANTIGRAVITY_PERMISSION_REVIEW_REQUIRED = "ANTIGRAVITY_PERMISSION_REVIEW_REQUIRED"
# Prepended only to the text sent to the CLI. The stored task text and its
# input hash stay the caller's original. {workspace} is the resolved workspace.
# Lives here so the built-in backend and the vendored plugin runtime share it.
READ_ONLY_PREAMBLE = (
    "[Asterun] This run is read-only. You may only read files under {workspace} with read_file. "
    "Writing files (including /tmp), running commands or scripts, network/URL access, MCP tools and subagents "
    "are blocked by policy; do not attempt them, and do not try to list directories (read files by exact path). "
    "Finish with your complete answer in this turn.\n\n"
)
MAX_FRAME_BYTES = 256 * 1024
MAX_STDOUT_BYTES = 4 * 1024 * 1024
MAX_STDERR_BYTES = 256 * 1024
MAX_EVENTS = 8192
MAX_SUMMARY_BYTES = 32 * 1024
CANCEL_GRACE = 0.5
TERMINATE_GRACE = 0.3
# True when agy runs in its own session/process group that this runtime may
# signal and inspect. The plugin copy sets this to False (the host owns the
# worker group), so it never claims a delegated run's processes are gone.
_OWNS_PROCESS_GROUP = False
PROGRESS_INTERVAL = 1.0
USAGE_KEYS = {"input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens"}
# These are diagnostics, never permission grants. Keep a rolling suffix so a notice
# split across reads cannot turn a denied tool into a successful execution.
# Print mode logs "soft-denying", not only "soft-denied", then closes the turn.
_DENIED = re.compile(
    r"soft[- ]denied|soft[- ]denying|permission (?:was )?denied|(?:tool|action|command)[^\n]{0,100}"
    r"(?:requires? approval|requires? permission|not allowed|denied)|"
    r"(?:cannot|can't|unable to)[^\n]{0,80}(?:ask|obtain)[^\n]{0,80}(?:approval|permission)",
    re.IGNORECASE,
)
# 普通 OS “permission denied” 只算宽泛信号；复核码只认软拒绝或审批措辞。
_REVIEW_EVIDENCE = re.compile(r"soft[- ]den(?:ied|ying)|requires? approval", re.IGNORECASE)
# Profile deny rules are a hard refusal only for agy's whole native sentence.
# A substring, an extra field, or a non-string value is an ordinary tool error.
# Never copy the tool error text into a public result. The quoted argument is
# Go %q and must unquote to the parenthesized text, which may contain newlines.
_MAX_DENIAL_MESSAGE = 40000
_MAX_GO_QUOTE_BODY = 16384
# Single-byte escapes. \x and octal are raw bytes as well; \u and \U are
# code points written as UTF-8. See _go_unquote.
_GO_SIMPLE_ESCAPES = {
    "\\": 0x5C, '"': 0x22, "n": 0x0A, "t": 0x09, "r": 0x0D,
    "a": 0x07, "b": 0x08, "f": 0x0C, "v": 0x0B,
}
# strconv.Unquote accepts only ASCII hex. int(text, 16) also accepts signs
# and underscores, which are not hex digits and must be rejected first.
_ASCII_HEX = frozenset("0123456789abcdefABCDEF")
# Q stops at an unescaped quote. Raw text is sliced up to the fixed suffix so
# a newline inside the command is kept and an earlier copy of the suffix is not
# treated as the end of the sentence.
_QUOTED_BODY = r'((?:[^"\\\r\n]|\\.)*)'
_POLICY_TOOLS = "unsandboxed|command|read_file|write_file|read_url|execute_url|mcp"
_REVIEW_TOOLS = "read_file|write_file|command|unsandboxed|read_url|execute_url|mcp"
_POLICY_PREFIX = re.compile(
    "^permission check failed for (" + _POLICY_TOOLS + ') "' + _QUOTED_BODY
    + '": Permission denied for \\1\\('
)
_POLICY_SUFFIX = "). Matches user-configured deny rule."
_POLICY_DENIED_KEYS = frozenset({"type", "message"})
# Second line of agy's unattended request-review denial. The message must end
# with this sentence verbatim: no prefix, suffix, or extra line. agy writes
# "soft-denying tool confirmation" only to its own log; stderr may carry a
# generic "permission denied" that is not this denial.
_REVIEW_DENIAL_SECOND_LINE = (
    "Do not attempt to circumvent this denial by rephrasing the command, "
    "using alternative tools/scripts (e.g. python, sh, curl), or accessing "
    "the same target resource. Proceed without performing this action."
)
_REVIEW_PREFIX = re.compile(
    "^permission check failed for (" + _REVIEW_TOOLS + ') "' + _QUOTED_BODY
    + '": user denied permission for \\1\\('
)
_REVIEW_SUFFIX = ")\n" + _REVIEW_DENIAL_SECOND_LINE
_KNOWN_TOOLS = frozenset({
    "run_command", "read_file", "write_file", "view_file", "list_dir", "grep_search",
    "find_by_name", "codebase_search", "read_url_content", "view_content_chunk",
    "search_web", "read_url", "execute_url", "mcp", "replace_file_content",
    "multi_replace_file_content", "write_to_file", "ReadFile", "WriteFile",
    "RunCommand", "ReadUrl", "ExecuteUrl", "Mcp", "ViewFile", "ListDir",
    "GrepSearch", "FindByName",
})
_SOFT_DENIED_TOOL = re.compile(
    r'soft-denying tool confirmation "([A-Za-z_][A-Za-z0-9_]{0,63})"'
)
# agy >= 1.4.5 stores a soft-denied tool confirmation as conversation step
# type 132, status 6. The file name is the lowercase UUID form already used
# for agy state files (antigravity_profile._validate_uuid_state).
_CONVERSATION_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_REVIEW_STEP_TYPE = 132
_REVIEW_STEP_STATUS = 6
_MAX_CONVERSATION_DB_BYTES = 64 * 1024 * 1024
_MAX_REVIEW_ROWS = 256
_MAX_PROTO_BYTES = 256 * 1024
# A conversation-db query that exceeds any of these is no evidence.
# The instruction budget is large enough for a scan of the size cap.
_SQL_PROGRESS_INSTRUCTIONS = 10000
_SQL_INSTRUCTION_BUDGET = 50000000
_SQL_DEADLINE_SECONDS = 1.0
_MAX_PROTO_DEPTH = 8
_MAX_PROTO_FIELDS = 512
_COPY_CHUNK = 1024 * 1024
# agy 真实额度错误格式出现后在此补充
AGY_QUOTA_PATTERNS: tuple[re.Pattern[str], ...] = ()
_AGY_RATE_LIMIT = re.compile(r"\b429\b|RESOURCE_EXHAUSTED")
_AGY_QUOTA_WORD = re.compile(r"quota", re.IGNORECASE)
_PROVIDER_FAILURE_KEYS = ("kind", "status_code", "source", "provider")


def classify_agy_error(status, text) -> dict | None:
    """只在 ERROR 终态做白名单分类。返回值只有四个字段，不含供应商原文。"""
    if status != "ERROR" or not isinstance(text, str):
        return None
    limited = _AGY_RATE_LIMIT.search(text) is not None
    quota_hit = _AGY_QUOTA_WORD.search(text) is not None or any(
        pattern.search(text) for pattern in AGY_QUOTA_PATTERNS
    )
    if quota_hit and (limited or any(pattern.search(text) for pattern in AGY_QUOTA_PATTERNS)):
        kind = "quota_exhausted"
    elif limited:
        kind = "rate_limited"
    else:
        return None
    return {
        "kind": kind,
        "status_code": 429,
        "source": "native_result",
        "provider": "antigravity",
    }


def _provider_failure(classified: dict) -> dict[str, Any]:
    return {key: classified[key] for key in _PROVIDER_FAILURE_KEYS}


class _ProtocolError(Exception):
    pass


def _public_tool_name(value: Any) -> str:
    if isinstance(value, str) and value in _KNOWN_TOOLS:
        return value
    return "other"


class _ReviewTools(set):
    """Whitelist tool names from conversation-db review steps."""

    def __init__(self, names: set[str], step_count: int) -> None:
        super().__init__(names)
        self.step_count = step_count


def _read_varint(data: bytes, index: int) -> tuple[int, int] | None:
    """Read one protobuf varint. None if the encoding does not fit in 64 bits."""
    value = 0
    shift = 0
    limit = min(len(data), index + 10)
    while index < limit:
        byte = data[index]
        index += 1
        # The 10th byte may contribute only bit 63. A continuation bit or any
        # higher bit does not fit in an unsigned 64-bit value.
        if shift == 63 and byte > 1:
            return None
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, index
        shift += 7
    return None


def _protobuf_fields(data: bytes, *, depth: int = 0) -> list[tuple[int, int, object]] | None:
    """Parse one protobuf message into (field number, wire type, value).

    Accepts varint, fixed64, length-delimited, and fixed32. Groups and any
    other wire type are rejected. Length-delimited values stay raw bytes;
    callers descend explicitly so a string is not required to be a message.
    Returns None on any malformation, depth overflow, or size overflow.
    """
    try:
        if not isinstance(data, (bytes, bytearray)) or depth > _MAX_PROTO_DEPTH or len(data) > _MAX_PROTO_BYTES:
            return None
        fields: list[tuple[int, int, object]] = []
        index = 0
        size = len(data)
        while index < size:
            if len(fields) >= _MAX_PROTO_FIELDS:
                return None
            parsed = _read_varint(data, index)
            if parsed is None:
                return None
            key, index = parsed
            field_number = key >> 3
            wire = key & 7
            if field_number < 1 or field_number > 536870911:
                return None
            if wire == 0:
                parsed = _read_varint(data, index)
                if parsed is None:
                    return None
                value, index = parsed
            elif wire == 1:
                if index + 8 > size:
                    return None
                value = data[index:index + 8]
                index += 8
            elif wire == 2:
                parsed = _read_varint(data, index)
                if parsed is None:
                    return None
                length, index = parsed
                if length > _MAX_PROTO_BYTES or index + length > size:
                    return None
                value = data[index:index + length]
                index += length
            elif wire == 5:
                if index + 4 > size:
                    return None
                value = data[index:index + 4]
                index += 4
            else:
                return None
            fields.append((field_number, wire, value))
        return fields
    except Exception:
        return None


def _call_tool_name(call: list[tuple[int, int, object]]) -> str | None:
    """Whitelist tool name from the first field 2 of a tool-call message."""
    for number, wire, value in call:
        if number != 2 or wire != 2:
            continue
        if not isinstance(value, bytes) or len(value) > 64:
            return None
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
        public = _public_tool_name(text)
        # Unknown names stay off the public result. "other" would hide which
        # tool was denied and is not evidence for this path.
        return None if public == "other" else public
    return None


def _review_tool_from_payload(payload: bytes) -> str | None:
    """Public tool name encoded in an agy 1.4.5 review step payload.

    The name is field 2 of the tool call at field 5 → field 4. Arguments in
    sibling fields are ignored. Malformed input returns None and does not raise.
    """
    try:
        if isinstance(payload, memoryview):
            payload = payload.tobytes()
        if not isinstance(payload, (bytes, bytearray)):
            return None
        fields = _protobuf_fields(bytes(payload), depth=0)
        if fields is None:
            return None
        for number, wire, value in fields:
            if number != 5 or wire != 2 or not isinstance(value, bytes):
                continue
            body = _protobuf_fields(value, depth=1)
            if body is None:
                return None
            for sub_number, sub_wire, sub_value in body:
                if sub_number != 4 or sub_wire != 2 or not isinstance(sub_value, bytes):
                    continue
                call = _protobuf_fields(sub_value, depth=2)
                if call is None:
                    return None
                return _call_tool_name(call)
        return None
    except Exception:
        return None


def _copy_fd(fd: int, dest: Path, size: int) -> None:
    with dest.open("wb") as handle:
        remaining = size
        while remaining:
            chunk = os.read(fd, min(_COPY_CHUNK, remaining))
            if not chunk:
                raise OSError("short_copy")
            handle.write(chunk)
            remaining -= len(chunk)


def _open_directory(parent: int | None, name: str | Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    if parent is None:
        return os.open(name, flags)
    # O_NOFOLLOW keeps a swapped directory entry from escaping the agy HOME.
    return os.open(name, flags | os.O_NOFOLLOW, dir_fd=parent)


def _bounded_regular(dir_fd: int, name: str) -> os.stat_result | None:
    """lstat of one regular file inside the size cap, or None when absent.

    A symlink, another file type, or a file over the cap raises OSError so
    the caller returns no evidence instead of following the link.
    """
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise OSError("not_regular")
    if info.st_size < 0 or info.st_size > _MAX_CONVERSATION_DB_BYTES:
        raise OSError("db_size")
    return info


def _opened_regular_size(fd: int, earlier: os.stat_result) -> int | None:
    """Byte size of fd when it is still the regular file ``earlier`` lstat saw.

    None means the opened object is not a regular file, is over the size cap,
    or has a different device or inode. Callers treat that as no evidence.
    """
    found = os.fstat(fd)
    if not stat.S_ISREG(found.st_mode):
        return None
    if found.st_size < 0 or found.st_size > _MAX_CONVERSATION_DB_BYTES:
        return None
    if (found.st_dev, found.st_ino) != (earlier.st_dev, earlier.st_ino):
        return None
    return found.st_size


def _tools_from_copied_db(dest: Path) -> set[str]:
    """Whitelist tools from review steps in a private read-only database copy.

    ``steps`` must be an ordinary sqlite_master table. Rows are filtered to
    step_type 132, status 6, and payloads within the cap before the candidate
    limit. SQLite's string/BLOB limit, an instruction budget, and a deadline
    are armed first. A limit or any SQLite error returns no evidence.
    """
    try:
        connection = sqlite3.connect(dest.as_uri() + "?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        return set()
    rows: list = []
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _MAX_PROTO_BYTES)
        deadline = time.monotonic() + _SQL_DEADLINE_SECONDS
        used = 0

        def _abort_over_budget() -> int:
            nonlocal used
            used += _SQL_PROGRESS_INSTRUCTIONS
            if used > _SQL_INSTRUCTION_BUDGET or time.monotonic() >= deadline:
                return 1
            return 0

        connection.set_progress_handler(_abort_over_budget, _SQL_PROGRESS_INSTRUCTIONS)
        # SELECT does not fire triggers, so the steps object's own type is enough.
        found = connection.execute(
            "SELECT type FROM sqlite_master WHERE name = ?",
            ("steps",),
        ).fetchall()
        if found != [("table",)]:
            return set()
        rows = connection.execute(
            "SELECT step_type, status, step_payload FROM steps "
            "WHERE step_type = ? AND status = ? AND length(step_payload) <= ? "
            "ORDER BY idx LIMIT ?",
            (_REVIEW_STEP_TYPE, _REVIEW_STEP_STATUS, _MAX_PROTO_BYTES, _MAX_REVIEW_ROWS),
        ).fetchall()
    except sqlite3.Error:
        return set()
    finally:
        try:
            connection.set_progress_handler(None, 0)
        except sqlite3.Error:
            pass
        try:
            connection.close()
        except sqlite3.Error:
            pass
    names: set[str] = set()
    count = 0
    for step_type, status, payload in rows:
        if step_type != _REVIEW_STEP_TYPE or status != _REVIEW_STEP_STATUS:
            continue
        if isinstance(payload, memoryview):
            if payload.nbytes > _MAX_PROTO_BYTES:
                continue
            payload = payload.tobytes()
        if not isinstance(payload, (bytes, bytearray)) or len(payload) > _MAX_PROTO_BYTES:
            continue
        try:
            tool = _review_tool_from_payload(payload)
        except Exception:
            continue
        if tool:
            names.add(tool)
            count += 1
    if count == 0:
        return set()
    return _ReviewTools(names, count)


def _readonly_open_flags() -> int:
    """Flags for the database and WAL: read-only, no symlink follow, no block."""
    return os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


def _review_steps_in_directory(dir_fd: int, filename: str) -> set[str]:
    """Copy filename and an optional -wal, then read review tool names.

    The agy profile is never opened with SQLite. SQLite would create a -shm
    sidecar even for a read-only connection. Open cannot block on a FIFO
    swapped in after lstat. The fd must still be that regular file, within
    the size cap.
    """
    info = _bounded_regular(dir_fd, filename)
    if info is None or info.st_size == 0:
        return set()
    wal_name = filename + "-wal"
    wal_info = _bounded_regular(dir_fd, wal_name)
    flags = _readonly_open_flags()
    db_fd = os.open(filename, flags, dir_fd=dir_fd)
    try:
        size = _opened_regular_size(db_fd, info)
        if size is None:
            return set()
        wal_fd = None
        wal_size = None
        if wal_info is not None:
            wal_fd = os.open(wal_name, flags, dir_fd=dir_fd)
        try:
            if wal_fd is not None and wal_info is not None:
                wal_size = _opened_regular_size(wal_fd, wal_info)
                if wal_size is None:
                    return set()
            with tempfile.TemporaryDirectory(prefix="asterun-agy-conv-") as tmp:
                dest = Path(tmp) / filename
                _copy_fd(db_fd, dest, size)
                if wal_fd is not None and wal_size is not None:
                    _copy_fd(wal_fd, Path(tmp) / wal_name, wal_size)
                return _tools_from_copied_db(dest)
        finally:
            if wal_fd is not None:
                os.close(wal_fd)
    finally:
        os.close(db_fd)


def _review_steps_from_conversation_db(home: Path, conversation_id: str) -> set[str]:
    """Public tool names of soft-denied confirmation steps in one agy conversation.

    The conversation id must be the lowercase UUID of the session. The database
    must be a regular file under ``<home>/.gemini/antigravity-cli/conversations/``.
    Any OS error, SQLite error, rejected path, or resource limit yields an empty set.
    """
    try:
        if not isinstance(home, Path) or not isinstance(conversation_id, str):
            return set()
        if _CONVERSATION_UUID.fullmatch(conversation_id) is None or "\x00" in conversation_id:
            return set()
        filename = conversation_id + ".db"
        root = _open_directory(None, home)
        try:
            gemini = _open_directory(root, ".gemini")
            try:
                cli = _open_directory(gemini, "antigravity-cli")
                try:
                    conversations = _open_directory(cli, "conversations")
                    try:
                        return _review_steps_in_directory(conversations, filename)
                    finally:
                        os.close(conversations)
                finally:
                    os.close(cli)
            finally:
                os.close(gemini)
        finally:
            os.close(root)
    except (OSError, sqlite3.Error):
        return set()


def _go_unquote(body: str) -> str | None:
    """Decode one Go strconv.Unquote body with the surrounding quotes removed.

    The result is built as bytes, then decoded as UTF-8. \\xNN and exactly
    three octal digits append one raw byte (not a Unicode code point), so
    ``\\xc3\\xa9`` is é. \\u, \\U and literal characters append their UTF-8
    encoding. Simple escapes append their single byte. \\u and \\U reject
    surrogates and code points above U+10FFFF. A sign, underscore, or other
    non-hex digit is invalid. Any invalid escape, bare quote, raw newline or
    CR, body longer than 16384 characters, or byte string that is not valid
    UTF-8 yields None. This function does not raise.
    """
    try:
        if not isinstance(body, str) or len(body) > _MAX_GO_QUOTE_BODY:
            return None
        out = bytearray()
        index = 0
        limit = len(body)
        while index < limit:
            char = body[index]
            if char in {'"', "\n", "\r"}:
                return None
            if char != "\\":
                out.extend(char.encode("utf-8"))
                index += 1
                continue
            if index + 1 >= limit:
                return None
            escaped = body[index + 1]
            index += 2
            simple = _GO_SIMPLE_ESCAPES.get(escaped)
            if simple is not None:
                out.append(simple)
                continue
            if escaped == "x":
                end = index + 2
                if end > limit:
                    return None
                hex_digits = body[index:end]
                if any(digit not in _ASCII_HEX for digit in hex_digits):
                    return None
                # Digits were checked, so int() cannot see a sign or underscore.
                out.append(int(hex_digits, 16))
                index = end
                continue
            if escaped in {"u", "U"}:
                width = 4 if escaped == "u" else 8
                end = index + width
                if end > limit:
                    return None
                hex_digits = body[index:end]
                if any(digit not in _ASCII_HEX for digit in hex_digits):
                    return None
                code = int(hex_digits, 16)
                index = end
                if code > 0x10FFFF or 0xD800 <= code <= 0xDFFF:
                    return None
                out.extend(chr(code).encode("utf-8"))
                continue
            if "0" <= escaped <= "7":
                # strconv.Unquote requires exactly three octal digits, not one or two.
                end = index + 2
                if end > limit:
                    return None
                second, third = body[index], body[index + 1]
                if not ("0" <= second <= "7" and "0" <= third <= "7"):
                    return None
                value = ((ord(escaped) - 48) << 6) | ((ord(second) - 48) << 3) | (ord(third) - 48)
                index = end
                if value > 255:
                    return None
                out.append(value)
                continue
            return None
        return bytes(out).decode("utf-8")
    except Exception:
        return None


def _quoted_argument_matches(quoted: str, raw: str) -> bool:
    """True when unquoting the Go %q body yields raw's UTF-8 bytes.

    Invalid UTF-8 does not match. This function does not raise.
    """
    produced = _go_unquote(quoted)
    if produced is None or not isinstance(raw, str):
        return False
    try:
        return produced.encode("utf-8") == raw.encode("utf-8")
    except Exception:
        return False


def _denial_parts(message: str, prefix: re.Pattern[str], suffix: str) -> tuple[str, str, str] | None:
    """Return tool, quoted body, and raw text for a sentence that ends with suffix.

    Raw text is the slice before that final suffix, so it may contain newlines.
    Anchoring on the suffix keeps an earlier copy inside the command inside the
    raw text, where the Go-unquote check rejects it.
    """
    if len(message) > _MAX_DENIAL_MESSAGE or not message.endswith(suffix):
        return None
    match = prefix.match(message)
    if match is None:
        return None
    raw_end = len(message) - len(suffix)
    if match.end() > raw_end:
        return None
    return match.group(1), match.group(2), message[match.end():raw_end]


def _is_policy_denial(error_info: Any) -> bool:
    """True only for agy's native deny sentence in a {type?, message} object.

    The quoted argument must be a Go %q body that unquotes to the parenthesized
    text. A command that only contains the deny sentence fails that check.
    """
    if not isinstance(error_info, dict) or not set(error_info).issubset(_POLICY_DENIED_KEYS):
        return False
    message = error_info.get("message")
    if not isinstance(message, str):
        return False
    if "type" in error_info and not isinstance(error_info["type"], str):
        return False
    parts = _denial_parts(message, _POLICY_PREFIX, _POLICY_SUFFIX)
    if parts is None:
        return False
    _tool, quoted, raw = parts
    return _quoted_argument_matches(quoted, raw)


def _is_review_denial(error_info: Any) -> str | None:
    """Tool token from agy's exact unattended review denial, else None.

    The quoted argument must be a Go %q body that unquotes to the parenthesized
    argument. A path or command that only embeds the sentence cannot pass.
    """
    if not isinstance(error_info, dict) or not set(error_info).issubset(_POLICY_DENIED_KEYS):
        return None
    message = error_info.get("message")
    if not isinstance(message, str):
        return None
    if "type" in error_info and not isinstance(error_info["type"], str):
        return None
    parts = _denial_parts(message, _REVIEW_PREFIX, _REVIEW_SUFFIX)
    if parts is None:
        return None
    tool, quoted, raw = parts
    if not _quoted_argument_matches(quoted, raw):
        return None
    return tool


def _record_step_denial(state: "_Run", index: int, status: str, info: Any) -> None:
    """Replace this step's denial from its current record.

    Only a terminal ERROR is evidence. An earlier ACTIVE frame, or any other
    non-terminal status, must not leave the step classified as a denial.
    """
    policy_name = None
    review_name = None
    if status == "ERROR" and isinstance(info, dict) and isinstance(info.get("error"), dict):
        error_info = info["error"]
        named = state.step_tools[index] if index in state.step_tools else info.get("name")
        public = _public_tool_name(named)
        if _is_policy_denial(error_info):
            policy_name = public
        if _is_review_denial(error_info) is not None:
            review_name = public
    if policy_name is None:
        state._policy_tools.pop(index, None)
    else:
        state._policy_tools[index] = policy_name
    if review_name is None:
        state._review_tools.pop(index, None)
    else:
        state._review_tools[index] = review_name
    _publish_denial_flags(state)


def _clear_step_denial(state: "_Run", index: int) -> None:
    if index not in state._policy_tools and index not in state._review_tools:
        return
    state._policy_tools.pop(index, None)
    state._review_tools.pop(index, None)
    _publish_denial_flags(state)


def _publish_denial_flags(state: "_Run") -> None:
    state.policy_denials = set(state._policy_tools)
    state.review_denials = set(state._review_tools)
    state.policy_denied_tools = set(state._policy_tools.values())
    state.soft_denied_tools = set(state._stderr_tools) | set(state._review_tools.values()) | set(state._db_tools)
    state.soft_denied = state._stderr_soft or bool(state._review_tools)
    state.review_evidence = state._stderr_review or bool(state._review_tools) or bool(state._db_tools)
    if state._review_tools:
        state.review_evidence_source = "stream"
    elif state._stderr_review or state._stderr_tools:
        state.review_evidence_source = "stderr"
    elif state._db_tools:
        state.review_evidence_source = "conversation_db"
    else:
        state.review_evidence_source = None


def _bounded_text(value: str) -> str:
    raw = value.encode("utf-8")
    return raw[:MAX_SUMMARY_BYTES].decode("utf-8", errors="ignore")


def _number(value: Any, *, integer: bool = False) -> bool:
    return not isinstance(value, bool) and isinstance(value, int if integer else (int, float)) and math.isfinite(value) and value >= 0


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _ProtocolError("duplicate_json_key")
        result[key] = value
    return result


@dataclass
class _Run:
    run_id: str
    expected_cwd: Path | None = None
    expected_model: str | None = None
    cancelled: threading.Event = field(default_factory=threading.Event)
    proc: subprocess.Popen | None = None
    prompt_attempted: bool = False
    stage: str = "preflight"
    failure_type: str | None = None
    session_id: str = ""
    init_seen: bool = False
    init_evidence: dict[str, str] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    tool_errors: set[int] = field(default_factory=set)
    step_errors: set[int] = field(default_factory=set)
    active_tools: set[int] = field(default_factory=set)
    subagents: set[int] = field(default_factory=set)
    step_tools: dict[int, str] = field(default_factory=dict)
    cleaned: bool = False
    step_states: dict[int, str] = field(default_factory=dict)
    step_types: dict[int, str] = field(default_factory=dict)
    soft_denied: bool = False
    review_evidence: bool = False
    policy_denials: set[int] = field(default_factory=set)
    review_denials: set[int] = field(default_factory=set)
    policy_denied_tools: set[str] = field(default_factory=set)
    soft_denied_tools: set[str] = field(default_factory=set)
    review_step_count: int = 0
    review_evidence_source: str | None = None
    _policy_tools: dict[int, str] = field(default_factory=dict)
    _review_tools: dict[int, str] = field(default_factory=dict)
    _stderr_soft: bool = False
    _stderr_review: bool = False
    _stderr_tools: set[str] = field(default_factory=set)
    _db_tools: set[str] = field(default_factory=set)
    text: str = ""
    text_truncated: bool = False
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    events_seen: int = 0
    provider_failure: dict[str, Any] | None = None

    def native(self) -> dict[str, Any]:
        native: dict[str, Any] = {
            DISPATCH_RECEIPT_KEY: {
                "run_id": self.run_id, "stage": self.stage,
                "prompt_attempted": self.prompt_attempted,
                "delivery": "may_have_been_sent" if self.prompt_attempted else "not_sent",
                "failure_type": self.failure_type,
                "process_started": self.proc is not None,
                "process_exit_code": None if self.proc is None else self.proc.poll(),
                "stdout_bytes": self.stdout_bytes, "stderr_bytes": self.stderr_bytes,
                "events_seen": self.events_seen,
            },
            "tool_error_count": len(self.tool_errors), "step_error_count": len(self.step_errors),
            "unexplained_error_count": len(
                (self.tool_errors | self.step_errors) - self.policy_denials - self.review_denials),
            "soft_denied": self.soft_denied, "subagent_count": len(self.subagents),
            "summary_truncated": self.text_truncated,
            "policy_denied_count": len(self.policy_denials),
            "review_denied_count": len(self.review_denials),
            "review_step_count": self.review_step_count,
            "review_evidence_source": self.review_evidence_source,
            "policy_denied_tools": sorted(self.policy_denied_tools),
            "soft_denied_tools": sorted(self.soft_denied_tools),
        }
        if self.session_id:
            native["backend_session_id"] = self.session_id
        if self.init_evidence:
            native["init_evidence"] = dict(self.init_evidence)
        if self.result is not None:
            native["result_status"] = self.result["status"]
            for name in ("num_turns", "duration_seconds", "usage"):
                if name in self.result:
                    native[name] = self.result[name]
            if "usage" in self.result:
                native["usage_scope"] = "session"
        # 缺字段或非 dict 时不输出，避免 native() 抛错把已确认终态打成未决。
        failure = self.provider_failure
        try:
            if isinstance(failure, dict) and all(key in failure for key in _PROVIDER_FAILURE_KEYS):
                native["provider_failure"] = _provider_failure(failure)
        except Exception:
            pass
        return native


class AntigravityRuntime:
    def __init__(self, timeout: float = 300.0) -> None:
        if not _number(timeout) or timeout <= 0:
            raise ValueError("timeout 必须是有限正数")
        self.timeout = timeout
        self._lock = threading.Lock()
        self._runs: dict[str, _Run] = {}
        self._cancelled: set[str] = set()
        self._closed = threading.Event()

    def cancel(self, run_id: RunId) -> dict[str, Any]:
        with self._lock:
            self._cancelled.add(run_id.value)
            state = self._runs.get(run_id.value)
            if state is not None:
                state.cancelled.set()
        return {"cancel_requested": True, "terminated": False}

    def close(self) -> None:
        self._closed.set()

    @staticmethod
    def _signal(proc: subprocess.Popen, sig: signal.Signals) -> None:
        try:
            proc.send_signal(sig)
        except ProcessLookupError:
            pass

    @classmethod
    def _cleanup(cls, proc: subprocess.Popen) -> None:
        # The host owns the worker group; final host cleanup covers remaining descendants.
        try:
            try:
                cls._signal(proc, signal.SIGTERM)
            except OSError:
                pass
            try:
                proc.wait(timeout=TERMINATE_GRACE)
            except subprocess.TimeoutExpired:
                pass
            try:
                cls._signal(proc, signal.SIGKILL)
            except OSError:
                pass
            try:
                proc.wait(timeout=TERMINATE_GRACE)
            except subprocess.TimeoutExpired:
                pass
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()

    @staticmethod
    def _session(state: _Run, value: Any, *, allow_empty: bool = False) -> None:
        if value == "" and allow_empty and not state.session_id:
            return
        if not isinstance(value, str) or not value.strip() or len(value) > 256 or any(ord(c) < 32 for c in value):
            raise _ProtocolError("invalid_conversation_id")
        if state.session_id and state.session_id != value:
            raise _ProtocolError("conversation_id_mismatch")
        state.session_id = value

    @classmethod
    def _event(cls, state: _Run, line: bytes) -> bool:
        state.events_seen += 1
        if state.events_seen > MAX_EVENTS:
            raise _ProtocolError("event_limit")
        try:
            item = json.loads(line.decode("utf-8"), object_pairs_hook=_unique_object,
                              parse_constant=lambda _: (_ for _ in ()).throw(_ProtocolError("invalid_number")))
        except (UnicodeDecodeError, ValueError, RecursionError) as error:
            raise _ProtocolError("malformed_json") from error
        if not isinstance(item, dict) or state.result is not None:
            raise _ProtocolError("event_after_result" if state.result is not None else "invalid_event")
        event = item.get("event")
        if event == "init":
            if state.init_seen or state.events_seen != 1 or not isinstance(item.get("init"), dict):
                raise _ProtocolError("invalid_init")
            cls._session(state, item.get("conversation_id"))
            init = item["init"]
            cwd = init.get("cwd")
            if (not isinstance(cwd, str) or not Path(cwd).is_absolute()
                    or Path(cwd).resolve() != state.expected_cwd):
                raise _ProtocolError("workspace_mismatch")
            # Match the profile's effective mode exactly; strict and bypass
            # modes are different policies, never interchangeable fallbacks.
            if init.get("permission_mode") != "request-review":
                raise _ProtocolError("unsafe_permission_mode")
            if state.expected_model is not None and init.get("model") != state.expected_model:
                raise _ProtocolError("model_mismatch")
            state.init_evidence = {"cwd": str(Path(cwd).resolve()), "permission_mode": "request-review"}
            if isinstance(init.get("model"), str) and len(init["model"]) <= 256:
                state.init_evidence["model"] = init["model"]
            state.init_seen = True
            return True
        if event == "step_update":
            step = item.get("step_update")
            if not state.init_seen or not isinstance(step, dict):
                raise _ProtocolError("invalid_step")
            cls._session(state, step.get("conversation_id"))
            index, status, kind = step.get("step_index"), step.get("state"), step.get("step_type")
            if not _number(index, integer=True) or status not in {"ACTIVE", "DONE", "ERROR"} or not isinstance(kind, str):
                raise _ProtocolError("invalid_step")
            if state.step_states.get(index) == "DONE":
                raise _ProtocolError("step_after_done")
            if state.step_states.get(index) == "ERROR":
                raise _ProtocolError("step_after_error")
            if index in state.step_types and state.step_types[index] != kind:
                # CLI 1.4+ re-labels an invoke_subagent tool step as "subagent"
                # once the delegate starts. Only that one transition is accepted.
                if not (state.step_types[index] == "tool" and kind == "subagent"
                        and state.step_tools.get(index) == "invoke_subagent"
                        and step.get("tool_name") == "invoke_subagent"):
                    raise _ProtocolError("step_type_changed")
            state.step_states[index] = status
            state.step_types[index] = kind
            if kind == "tool" and isinstance(step.get("tool_name"), str):
                if state.step_tools.setdefault(index, step["tool_name"]) != step["tool_name"]:
                    raise _ProtocolError("step_tool_changed")
            if status == "ERROR":
                state.step_errors.add(index)
            if kind == "subagent":
                state.active_tools.discard(index)
                state.subagents.add(index)
                if status == "ERROR":
                    state.tool_errors.add(index)
            if kind == "tool":
                if status == "ACTIVE":
                    state.active_tools.add(index)
                else:
                    state.active_tools.discard(index)
                if status == "ERROR":
                    state.tool_errors.add(index)
                info = step.get("tool_info")
                if info is not None and not isinstance(info, dict):
                    raise _ProtocolError("invalid_tool_info")
                if isinstance(info, dict) and info.get("error") is not None:
                    if not isinstance(info["error"], dict):
                        raise _ProtocolError("invalid_tool_error")
                    state.tool_errors.add(index)
                # Denial evidence is only this step's latest terminal ERROR.
                # A prior ACTIVE frame that carried the sentence is replaced.
                _record_step_denial(state, index, status, info if isinstance(info, dict) else None)
            else:
                _clear_step_denial(state, index)
            if kind == "tool" and (state.step_tools.get(index) == "invoke_subagent"
                                   or (isinstance(step.get("tool_info"), dict)
                                       and step["tool_info"].get("name") == "invoke_subagent")):
                # Delegation is detected at its first frame; the run stops here.
                state.subagents.add(index)
            if kind == "agent_response":
                delta = step.get("text_delta", "")
                if not isinstance(delta, str):
                    raise _ProtocolError("invalid_text_delta")
                joined = state.text + delta
                state.text = _bounded_text(joined)
                state.text_truncated |= state.text != joined
            return False
        if event != "result" or not isinstance(item.get("result"), dict):
            raise _ProtocolError("unknown_event")
        result = item["result"]
        status = result.get("status")
        if not isinstance(status, str) or not isinstance(result.get("response"), str):
            raise _ProtocolError("invalid_result")
        cls._session(state, result.get("conversation_id"), allow_empty=status in {"ERROR", "INVALID"})
        for name in ("num_turns", "duration_seconds"):
            if name in result and not _number(result[name], integer=name == "num_turns"):
                raise _ProtocolError("invalid_result_counter")
        usage = result.get("usage")
        if usage is not None and (not isinstance(usage, dict) or any(not _number(v, integer=True) for v in usage.values())):
            raise _ProtocolError("invalid_usage")
        # Copy only understood aggregate counters. Never merge arbitrary native data.
        state.result = {"status": status, "response": result["response"]}
        for name in ("num_turns", "duration_seconds"):
            if name in result:
                state.result[name] = result[name]
        if usage is not None:
            state.result["usage"] = {k: v for k, v in usage.items() if k in USAGE_KEYS}
        error = result.get("error")
        if error:
            if not isinstance(error, str):
                raise _ProtocolError("invalid_result_error")
            state.result["has_error"] = True
        # 分类失败只跳过 provider_failure，不改已确认的结果终态。
        try:
            raw_error = result.get("error")
            raw_response = result.get("response")
            error_text = raw_error if isinstance(raw_error, str) else ""
            response_text = raw_response if isinstance(raw_response, str) else ""
            classified = classify_agy_error(status, error_text + "\n" + response_text)
            if classified is not None:
                state.provider_failure = _provider_failure(classified)
        except Exception:
            state.provider_failure = None
        state.text = _bounded_text(result["response"])
        state.text_truncated |= state.text != result["response"]
        return True

    @staticmethod
    def _snapshot(state: _Run, *, status: str = "running", summary: str | None = None,
                  terminated: bool = False, error_code: str | None = None) -> dict[str, Any]:
        result = {"status": status, "terminated": terminated,
                  "summary": state.text if summary is None else summary, "native": state.native()}
        if error_code:
            result["error_code"] = error_code
        return result

    def run(self, run_id: RunId, text: str, *, command: list[str], env: dict[str, str],
            cwd: Path, publish: Callable, stopping: threading.Event,
            expected_model: str | None = None) -> dict[str, Any]:
        state = _Run(run_id.value, expected_cwd=cwd.resolve(), expected_model=expected_model)
        with self._lock:
            if run_id.value in self._runs:
                raise RuntimeError("同一运行不可重复派发")
            self._runs[run_id.value] = state
            if run_id.value in self._cancelled:
                state.cancelled.set()
        try:
            if stopping.is_set() or self._closed.is_set():
                state.failure_type = "core_stopping"
                return self._unknown(state)
            if state.cancelled.is_set():
                state.failure_type = "cancelled_before_spawn"
                return self._snapshot(state, status="cancelled", terminated=True, summary="提交前已取消，未启动原生进程")
            state.stage = "spawn"
            try:
                state.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                              stderr=subprocess.PIPE, cwd=cwd, env=env,
                                              start_new_session=False, bufsize=0)
            except (OSError, ValueError, TypeError) as error:
                state.failure_type = type(error).__name__
                return self._snapshot(state, status="failed", terminated=True,
                                      error_code=BACKEND_UNAVAILABLE, summary="Antigravity 进程启动失败，未提交输入")
            self._exchange(state, text, publish, stopping)
            if state.subagents:
                return self._stop_delegated(state)
            if state.result is None:
                state.failure_type = "missing_result"
                return self._unknown(state)
            status = state.result["status"]
            if status in {"ERROR", "INVALID"}:
                return self._snapshot(state, status="failed", terminated=True,
                                      error_code="ANTIGRAVITY_RUN_FAILED", summary=state.text or "Antigravity 返回原生失败终态")
            if status in {"CANCELED", "INTERRUPTED"}:
                return self._snapshot(state, status="cancelled", terminated=True, summary=state.text or "Antigravity 已确认中断")
            if status == "SUCCESS" and not state.init_seen:
                state.failure_type = "missing_init"
                return self._unknown(state)
            if (status == "SUCCESS" and state.session_id and state.proc.returncode == 0
                    and not state.active_tools and all(value in {"DONE", "ERROR"} for value in state.step_states.values())):
                # agy >= 1.4.5 keeps the soft-denied confirmation in the
                # conversation DB. Consult it only when stderr set the broad
                # soft-deny flag and neither the stream nor stderr named a tool.
                if state.soft_denied and not state.review_evidence and not state.soft_denied_tools:
                    home_text = env.get("HOME")
                    if isinstance(home_text, str) and home_text and "\x00" not in home_text:
                        found = _review_steps_from_conversation_db(Path(home_text), state.session_id)
                        if found:
                            state._db_tools.update(found)
                            state.review_step_count = getattr(found, "step_count", len(found))
                            _publish_denial_flags(state)
                if state.soft_denied and (state.review_evidence or state.soft_denied_tools):
                    names = ", ".join(sorted(state.soft_denied_tools)) or "未知工具"
                    summary = (
                        f"Antigravity 遇到需要人工审批的工具调用（{names}），无人值守模式下被软拒绝，"
                        "原生 CLI 随即结束本轮，未产出完整结果。请把所需文件放进已授权的读取目录，或调整提示避免该调用后重试。"
                    )
                    # A soft denial ends the turn. Other errors are only counted;
                    # the error code stays REVIEW_REQUIRED.
                    other = (state.tool_errors | state.step_errors) - state.policy_denials - state.review_denials
                    if other:
                        summary += f"另有 {len(other)} 个与权限无关的工具或步骤错误，修正授权后仍可能失败。"
                    if state.text:
                        summary += "\n\n" + state.text
                    state.failure_type = "soft_denied_review"
                    return self._snapshot(state, status="failed", terminated=True,
                                          error_code=ANTIGRAVITY_PERMISSION_REVIEW_REQUIRED, summary=summary)
                if state.soft_denied or state.result.get("has_error") or (state.tool_errors | state.step_errors) - state.policy_denials - state.review_denials:
                    return self._snapshot(state, status="failed", terminated=True,
                                          error_code=ANTIGRAVITY_RUN_INCOMPLETE,
                                          summary=state.text or "Antigravity 步骤执行失败或权限被拒绝")
                if state.policy_denials and not state.text.strip():
                    state.failure_type = "no_final_response"
                    return self._snapshot(state, status="failed", terminated=True,
                                          error_code=ANTIGRAVITY_RUN_INCOMPLETE,
                                          summary="Antigravity 的工具调用被权限策略拒绝后没有产出最终结果")
                return self._snapshot(state, status="succeeded", terminated=True)
            state.failure_type = "contradictory_result" if status == "SUCCESS" else "nonterminal_result"
            return self._unknown(state)
        except _ProtocolError as error:
            state.failure_type = str(error)
            return self._unknown(state)
        except Exception as error:
            state.failure_type = type(error).__name__
            return self._unknown(state)
        finally:
            if state.proc is not None and not state.cleaned:
                self._cleanup(state.proc)
            with self._lock:
                self._runs.pop(run_id.value, None)
                self._cancelled.discard(run_id.value)

    @staticmethod
    def _group_gone(proc: subprocess.Popen) -> bool:
        if not _OWNS_PROCESS_GROUP:
            return False
        # Killed group members are reaped asynchronously; allow a short window.
        deadline = time.monotonic() + 1.0
        while True:
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                return True
            except PermissionError:
                return False
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)

    def _stop_delegated(self, state: _Run) -> dict[str, Any]:
        # Delegated work is never accepted. End the whole process group now and
        # report a terminal failure only once the group is confirmed gone, so
        # the scheduler slot is released immediately instead of after a timeout.
        assert state.proc is not None
        self._cleanup(state.proc)
        # Never signal this PGID again: once gone, the number may be reused.
        state.cleaned = True
        if state.proc.poll() is None or not self._group_gone(state.proc):
            state.failure_type = "subagent_process_alive"
            return self._unknown(state)
        state.failure_type = "subagent_delegated"
        return self._snapshot(state, status="failed", terminated=True,
                              error_code=ANTIGRAVITY_RUN_INCOMPLETE,
                              summary="Antigravity 派出了子代理；Asterun 不接受委派结果，已结束进程。请要求不使用子代理后重试")

    @classmethod
    def _unknown(cls, state: _Run) -> dict[str, Any]:
        return cls._snapshot(state, status="pending_reconcile", error_code=REMOTE_STATE_UNKNOWN,
                             summary=state.text or "Antigravity 结果未确认，保留原生引用且不自动重发")

    def _exchange(self, state: _Run, text: str, publish: Callable, stopping: threading.Event) -> None:
        proc = state.proc
        assert proc is not None and proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        prompt = (json.dumps({"event": "user", "message": {"content": text}}, ensure_ascii=False) + "\n").encode("utf-8")
        deadline = time.monotonic() + self.timeout
        offset, stdout_buffer, stderr_tail = 0, bytearray(), b""
        cancel_at: float | None = None
        last_publish = time.monotonic()
        last_text = ""
        with selectors.DefaultSelector() as selector:
            for stream, name, events in ((proc.stdin, "stdin", selectors.EVENT_WRITE),
                                         (proc.stdout, "stdout", selectors.EVENT_READ),
                                         (proc.stderr, "stderr", selectors.EVENT_READ)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, events, name)
            while selector.get_map() or proc.poll() is None:
                now = time.monotonic()
                if stopping.is_set() or self._closed.is_set():
                    raise _ProtocolError("core_stopping")
                if now >= deadline:
                    raise _ProtocolError("timeout")
                if state.cancelled.is_set() and cancel_at is None:
                    if not state.prompt_attempted:
                        # No prompt was attempted, but no native cancellation acknowledgement:
                        # returning unknown is conservative for a process already started.
                        raise _ProtocolError("cancel_before_input")
                    self._signal(proc, signal.SIGINT)
                    cancel_at = now
                if cancel_at is not None and now - cancel_at >= CANCEL_GRACE and proc.poll() is None:
                    raise _ProtocolError("cancel_unconfirmed")
                for key, _ in selector.select(min(0.05, max(0, deadline - now))):
                    stream, channel = key.fileobj, key.data
                    if channel == "stdin":
                        if not state.prompt_attempted:
                            state.stage = "prompt"
                            state.prompt_attempted = True
                            publish(self._snapshot(state, status="dispatching"))
                            if stopping.is_set() or self._closed.is_set() or state.cancelled.is_set():
                                raise _ProtocolError("stopped_before_write")
                        try:
                            offset += os.write(stream.fileno(), prompt[offset:offset + 16384])
                        except BlockingIOError:
                            continue
                        if offset == len(prompt):
                            selector.unregister(stream)
                            stream.close()
                            state.stage = "observe"
                        continue
                    try:
                        data = os.read(stream.fileno(), 16384)
                    except BlockingIOError:
                        continue
                    if not data:
                        selector.unregister(stream)
                        stream.close()
                        if channel == "stdout" and stdout_buffer:
                            raise _ProtocolError("unterminated_frame")
                        continue
                    if channel == "stderr":
                        state.stderr_bytes += len(data)
                        if state.stderr_bytes > MAX_STDERR_BYTES:
                            raise _ProtocolError("stderr_limit")
                        diagnostic = stderr_tail + data
                        decoded = diagnostic.decode("utf-8", errors="replace")
                        state._stderr_soft = state._stderr_soft or bool(_DENIED.search(decoded))
                        state._stderr_review = state._stderr_review or bool(_REVIEW_EVIDENCE.search(decoded))
                        state._stderr_tools.update(
                            _public_tool_name(name) for name in _SOFT_DENIED_TOOL.findall(decoded))
                        _publish_denial_flags(state)
                        stderr_tail = diagnostic[-512:]
                        continue
                    state.stdout_bytes += len(data)
                    if state.stdout_bytes > MAX_STDOUT_BYTES:
                        raise _ProtocolError("stdout_limit")
                    stdout_buffer.extend(data)
                    while b"\n" in stdout_buffer:
                        line, _, remainder = stdout_buffer.partition(b"\n")
                        stdout_buffer = bytearray(remainder)
                        if not line or len(line) > MAX_FRAME_BYTES:
                            raise _ProtocolError("frame_limit" if line else "empty_frame")
                        immediate = self._event(state, line)
                        if state.subagents:
                            # agy 1.4 may never send a result while a delegate
                            # runs, or keep running after it; stop at once.
                            state.stage = "result" if state.result is not None else "subagent"
                            return
                        if state.result is None and (immediate or (state.text != last_text and time.monotonic() - last_publish >= PROGRESS_INTERVAL)):
                            publish(self._snapshot(state))
                            last_publish, last_text = time.monotonic(), state.text
                    if len(stdout_buffer) > MAX_FRAME_BYTES:
                        raise _ProtocolError("frame_limit")
            proc.wait(timeout=max(0.01, deadline - time.monotonic()))
            state.stage = "result" if state.result is not None else "eof"
