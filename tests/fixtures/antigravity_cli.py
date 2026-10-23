"""独立进程协议替身；不导入 Asterun，也不调用任何真实后端。"""

import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import time

MODE = os.environ.get("AGY_FIXTURE_MODE", "happy")
SESSION = "fixture-conversation-123"
# Real agy 1.4.5 soft-denied confirmation step (idx 2, type 132, status 6).
_REVIEW_CONVERSATION_ID = "c31a1601-21d5-4e6c-89c1-5596f85e698f"
_AGY145_STEP_HEX = "08840120062aac030a0c08dcf6e7d606108adda2d703180222630a23746f6f6c755f767274785f30314a753642756831724e787243704e4e4a54546e7672561209726561645f66696c651a317b2250617468223a222f776f726b73706163652f6173746572756e2d6167792d6669782f70726f67726573732e6d64227d320b08ddf6e7d60610aae8e7143a0b08ddf6e7d60610aae8e714420b08ddf6e7d60610a5ce831758fa0a622463333161313630312d323164352d346536632d383963312d353539366638356536393866a2014e0a2465636565373530312d323863662d343838322d613831392d6136303962393430656637621002222430616631376366612d363330372d343664642d616336322d303234393565626431633831d201550a0f0801120b08ddf6e7d60610e2c7eb140a0f0802120b08ddf6e7d60610f89fdf150a0f0809120b08ddf6e7d60610d7a0af160a0f0802120b08ddf6e7d60610b4c3ef160a0f0806120b08ddf6e7d606109df6831782020b08ddf6e7d60610fdd9de159202072205616c6c6f779a02203534666661333734346231663432653661623264386261343864626438613333b00201e208300a2e0a045061746812262f776f726b73706163652f6173746572756e2d6167792d6669782f70726f67726573732e6d64"
_AGY145_STEP_PAYLOAD = bytes.fromhex(_AGY145_STEP_HEX)

if sys.argv[1:] == ["--version"]:
    print(os.environ.get("AGY_FIXTURE_VERSION", "1.3.2"))
    raise SystemExit(0)
if sys.argv[1:] == ["--help"]:
    print("Usage of agy:", file=sys.stderr)
    for flag in ("--add-dir", "--disable-slash-commands", "--input-format", "--model",
                 "--output-format", "--print-timeout"):
        print(f"  {flag}  fixture", file=sys.stderr)
    raise SystemExit(0)


def emit(value):
    print(json.dumps(value), flush=True)


def init():
    values = {"cwd": os.getcwd(), "tools": ["read_file"], "permission_mode": "request-review", "model": "fixture-model"}
    if MODE == "cwd_mismatch":
        values["cwd"] = "/different-workspace"
    elif MODE == "unsafe_permission":
        values["permission_mode"] = "always-proceed"
    elif MODE == "unknown_permission":
        values["permission_mode"] = "future-mode"
    elif MODE == "strict_permission":
        values["permission_mode"] = "strict"
    elif MODE == "yolo_permission":
        values["permission_mode"] = "yolo"
    elif MODE == "missing_permission":
        values.pop("permission_mode")
    elif MODE == "missing_model":
        values.pop("model")
    emit({"event": "init", "conversation_id": SESSION,
          "init": values})


def step(index, kind="agent_response", state="DONE", **values):
    emit({"event": "step_update", "step_update": {"conversation_id": SESSION,
          "step_index": index, "state": state, "step_type": kind, **values}})


def result(status="SUCCESS", response="fixture response", **values):
    emit({"event": "result", "result": {"conversation_id": SESSION, "status": status,
          "response": response, "num_turns": 1, "duration_seconds": 0.01,
          "usage": {"input_tokens": 7, "output_tokens": 3, "thinking_tokens": 1,
                    "cache_read_tokens": 0, "total_tokens": 10}, **values}})


_POLICY_DENY_MESSAGE = (
    'permission check failed for unsandboxed "private diagnostic": '
    "Permission denied for unsandboxed(private diagnostic). "
    "Matches user-configured deny rule."
)
# Go %q puts newlines in the quoted argument as the two characters \n, and the
# parenthesised argument keeps the real newlines.
_POLICY_DENY_MULTILINE = (
    'permission check failed for unsandboxed "cd /tmp && python3 - <<\'EOF\'\\nimport json\\nprint(\\"x\\")\\nEOF": '
    'Permission denied for unsandboxed(cd /tmp && python3 - <<\'EOF\'\nimport json\nprint("x")\nEOF). '
    "Matches user-configured deny rule."
)


def policy_denied_command():
    step(0, "tool", "ACTIVE", tool_info={"name": "run_command"})
    step(0, "tool", "ERROR", tool_info={"name": "run_command", "error": {
        "type": "TOOL_ERROR", "message": _POLICY_DENY_MESSAGE}})


_REVIEW_DENY_SECOND = (
    "Do not attempt to circumvent this denial by rephrasing the command, "
    "using alternative tools/scripts (e.g. python, sh, curl), or accessing "
    "the same target resource. Proceed without performing this action."
)


def policy_deny_message(tool, quoted, raw):
    return (
        f'permission check failed for {tool} "{quoted}": '
        f"Permission denied for {tool}({raw}). "
        "Matches user-configured deny rule."
    )


# \xNN is the UTF-8 bytes of é, not the code points U+00C3 U+00A9.
_UTF8_E_QUOTED = r"\xc3\xa9"
_UTF8_E = "é"
_UTF8_E_AS_CODEPOINTS = "\u00c3\u00a9"


def review_deny_message(tool, quoted, raw):
    return (
        f'permission check failed for {tool} "{quoted}": '
        f"user denied permission for {tool}({raw})\n{_REVIEW_DENY_SECOND}"
    )


_REVIEW_OUTSIDE = "/outside/private diagnostic.md"
_REVIEW_STREAM_MESSAGE = review_deny_message("read_file", _REVIEW_OUTSIDE, _REVIEW_OUTSIDE)
# Quoted ARG and parenthesized ARG2 disagree, so the sentence shape is not
# agy's denial. A path that embeds the denial wording must not be accepted.
_REVIEW_FORGED_MESSAGE = review_deny_message(
    "read_file", "/x", '/y": user denied permission for read_file(/z',
)


def _fill_review_db(path, payload, step_type, status):
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute(
            "CREATE TABLE steps ("
            "idx integer PRIMARY KEY, step_type integer, status integer, "
            "has_subtrajectory, metadata blob, error_details blob, "
            "permissions blob, task_details blob, render_info blob, "
            "step_payload blob, step_format integer)"
        )
        connection.executemany(
            "INSERT INTO steps (idx, step_type, status, step_payload) VALUES (?, ?, ?, ?)",
            (
                (0, 14, 3, None),
                (1, 15, 3, None),
                (2, step_type, status, payload),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    # Drop sidecars this fixture created so a later reader can be checked for
    # leaving the agy directory untouched.
    for suffix in ("-journal", "-shm", "-wal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.is_symlink():
            continue
        if sidecar.is_file():
            sidecar.unlink()


def _prepare_review_db(payload, step_type, status, *, write, symlink):
    raw = os.environ.get("HOME")
    if not raw:
        return
    directory = Path(raw) / ".gemini" / "antigravity-cli" / "conversations"
    directory.mkdir(parents=True, exist_ok=True)
    if not write:
        return
    db_path = directory / (_REVIEW_CONVERSATION_ID + ".db")
    if symlink:
        real = directory / (_REVIEW_CONVERSATION_ID + ".real.db")
        _fill_review_db(real, payload, step_type, status)
        db_path.symlink_to(real.name)
        return
    _fill_review_db(db_path, payload, step_type, status)


def _emit_review_step_transcript(conversation_id):
    # agy 1.4.5 print mode: init, user, planner, agent response, SUCCESS.
    # The denied read is absent from the stream and recorded only in the DB.
    emit({"event": "init", "conversation_id": conversation_id, "init": {
        "cwd": os.getcwd(), "tools": ["read_file"],
        "permission_mode": "request-review", "model": "fixture-model",
    }})
    for index, kind in ((0, "user"), (1, "planner")):
        emit({"event": "step_update", "step_update": {
            "conversation_id": conversation_id, "step_index": index,
            "state": "DONE", "step_type": kind,
        }})
    emit({"event": "step_update", "step_update": {
        "conversation_id": conversation_id, "step_index": 2, "state": "DONE",
        "step_type": "agent_response", "text_delta": "fixture response",
    }})
    os.write(2, b"permission denied\n")
    emit({"event": "result", "result": {
        "conversation_id": conversation_id, "status": "SUCCESS",
        "response": "fixture response", "num_turns": 1, "duration_seconds": 0.01,
        "usage": {"input_tokens": 7, "output_tokens": 3, "thinking_tokens": 1,
                  "cache_read_tokens": 0, "total_tokens": 10},
    }})


def _emit_review_step_db(mode):
    payload = _AGY145_STEP_PAYLOAD
    step_type, status = 132, 6
    write, symlink = True, False
    if mode == "review_step_db_status":
        status = 3
    elif mode == "review_step_db_type":
        step_type = 14
    elif mode == "review_step_db_unknown_tool":
        payload = payload.replace(b"read_file", b"fake_tool", 1)
    elif mode == "review_step_db_truncated":
        payload = payload[:24]
    elif mode == "review_step_db_garbage":
        payload = b"\xff\x00not-a-protobuf"
    elif mode == "review_step_db_missing":
        write = False
    elif mode == "review_step_db_symlink":
        symlink = True
    elif mode != "review_step_db":
        write = False
    _prepare_review_db(payload, step_type, status, write=write, symlink=symlink)
    _emit_review_step_transcript(_REVIEW_CONVERSATION_ID)


if MODE == "before_input":
    init()
if MODE == "no_input":
    time.sleep(30)
    sys.exit(0)
prompt = sys.stdin.read()
_prompt_log = os.environ.get("AGY_FIXTURE_PROMPT_LOG")
if _prompt_log:
    Path(_prompt_log).write_text(prompt, encoding="utf-8")
message = json.loads(prompt)
assert message["event"] == "user" and isinstance(message["message"]["content"], str)
assert len(sys.argv) == 1, "prompt or non-fixture arguments appeared in argv"
if MODE == "result_only":
    result()
    sys.exit(0)
if MODE == "error_no_id":
    result("ERROR", response="", conversation_id="", error="fixture-native-secret")
    sys.exit(2)
if MODE != "before_input" and not MODE.startswith("review_step_db"):
    init()
if MODE in {"cancel_ack", "cancel_nack", "timeout", "core_stop"}:
    if MODE == "cancel_ack":
        def interrupted(_signum, _frame):
            result("INTERRUPTED", "")
            sys.exit(130)
        signal.signal(signal.SIGINT, interrupted)
    else:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    marker = os.environ.get("AGY_FIXTURE_READY")
    if marker:
        Path(marker).write_text(str(os.getpid()))
    time.sleep(30)
    sys.exit(0)
if MODE == "malformed":
    print("not JSON", flush=True)
elif MODE == "eof":
    pass
elif MODE == "oversize":
    print("x" * (300 * 1024), flush=True)
elif MODE == "stderr_flood":
    os.write(2, b"private diagnostic\n" * (400 * 1024 // 19))
elif MODE == "stdout_flood":
    for i in range(8194):
        step(i, "checkpoint", padding="x" * 768)
elif MODE == "event_flood":
    for i in range(8194):
        step(i, "checkpoint")
elif MODE == "unterminated":
    sys.stdout.write('{"event":"result"}')
    sys.stdout.flush()
elif MODE == "duplicate_json":
    print('{"event":"result","event":"result","result":{}}', flush=True)
elif MODE == "id_mismatch":
    result(conversation_id="different-conversation")
elif MODE == "step_mismatch":
    step(0, conversation_id="different-conversation")
elif MODE == "double_init":
    init()
elif MODE == "unknown_event":
    emit({"event": "future_event", "secret": "not public"})
elif MODE == "bad_usage":
    result(usage={"input_tokens": "private diagnostic"})
elif MODE == "bad_counter":
    result(num_turns=True)
elif MODE == "duplicate_result":
    result()
    result()
elif MODE == "active_tool":
    step(0, "tool", "ACTIVE", tool_info={"name": "read_file"})
    result()
elif MODE == "active_response":
    step(0, "agent_response", "ACTIVE", text_delta="unfinished")
    result()
elif MODE == "changed_step":
    step(0, "agent_response", "ACTIVE", text_delta="unfinished")
    step(0, "checkpoint")
    result()
elif MODE == "double_done":
    step(0, text_delta="a")
    step(0, text_delta="b")
    result()
elif MODE == "error":
    result("ERROR", "", error="private diagnostic")
    sys.exit(1)
elif MODE in {"INVALID", "WAITING", "RUNNING", "FUTURE", "CANCELED"}:
    result(MODE, "")
elif MODE == "success_nonzero":
    result()
    sys.exit(7)
elif MODE == "success_error":
    result(error="private diagnostic")
elif MODE == "tool_error":
    step(0, "tool", tool_info={"name": "read_file", "parameters": {"path": "private path"},
                              "output": "private content", "error": {"type": "permission", "message": "private diagnostic"}})
    result()
elif MODE in {"tool_state_error", "tool_state_error_no_info", "response_state_error", "checkpoint_state_error", "event_after_error"}:
    kind = "agent_response" if MODE == "response_state_error" else "checkpoint" if MODE == "checkpoint_state_error" else "tool"
    step(0, kind, "ACTIVE")
    fields = {"tool_info": {"name": "read_file", "error": {"type": "TOOL_ERROR", "message": "private diagnostic"}}} if MODE == "tool_state_error" else {}
    step(0, kind, "ERROR", **fields)
    if MODE == "event_after_error":
        step(0, kind, "DONE")
    result(response="")
elif MODE in {"soft_deny", "split_soft_deny", "benign_stderr"}:
    if MODE == "split_soft_deny":
        os.write(2, b"tool run_command soft-de")
        time.sleep(0.02)
        os.write(2, b"nied: private diagnostic\n")
    else:
        os.write(2, b"private diagnostic\n" if MODE == "benign_stderr" else b"tool run_command requires approval: private diagnostic\n")
    result()
elif MODE == "stderr_eacces":
    os.write(2, b"open /x: permission denied\n")
    result()
elif MODE == "subagent":
    step(0, "tool", "ACTIVE", tool_name="invoke_subagent", tool_info={"name": "invoke_subagent"})
    step(0, "subagent", "DONE", tool_name="invoke_subagent")
    step(1, text_delta="waiting for the delegate")
    result()
elif MODE == "subagent_no_result":
    step(0, "tool", "ACTIVE", tool_name="invoke_subagent", tool_info={"name": "invoke_subagent"})
    time.sleep(30)
elif MODE == "subagent_step_only":
    step(0, "subagent", "ACTIVE", tool_name="invoke_subagent")
    time.sleep(30)
elif MODE == "subagent_with_child":
    import subprocess
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    marker = os.environ.get("AGY_FIXTURE_READY")
    if marker:
        Path(marker).write_text(str(child.pid))
    step(0, "tool", "ACTIVE", tool_name="invoke_subagent", tool_info={"name": "invoke_subagent"})
    time.sleep(30)
elif MODE == "subagent_lingers":
    step(0, "tool", "ACTIVE", tool_name="invoke_subagent", tool_info={"name": "invoke_subagent"})
    step(0, "subagent", "DONE", tool_name="invoke_subagent")
    result(response="waiting for the delegate")
    time.sleep(30)
elif MODE == "retyped_tool":
    step(0, "tool", "ACTIVE", tool_name="read_file", tool_info={"name": "read_file"})
    step(0, "subagent", "DONE", tool_name="read_file")
    result()
elif MODE == "renamed_subagent":
    step(0, "tool", "ACTIVE", tool_name="read_file", tool_info={"name": "read_file"})
    step(0, "subagent", "DONE", tool_name="invoke_subagent")
    result()
elif MODE == "tool_renamed":
    step(0, "tool", "ACTIVE", tool_name="read_file", tool_info={"name": "read_file"})
    step(0, "tool", "DONE", tool_name="run_command", tool_info={"name": "run_command"})
    result()
elif MODE == "self_update":
    # Simulate the CLI replacing its own executable while this run is active.
    me = Path(sys.argv[0])
    staged = me.with_name(me.name + ".new")
    staged.write_bytes(me.read_bytes())
    staged.chmod(0o700)
    os.replace(staged, me)
    step(0, text_delta="fixture response")
    result()
elif MODE == "large_response":
    result(response="\u4e2d" * 40000)
elif MODE == "result_no_exit":
    result("INTERRUPTED", "")
    time.sleep(30)
elif MODE == "policy_denied_recovered":
    policy_denied_command()
    step(1, "tool", "DONE", tool_info={"name": "read_file"})
    step(2, "agent_response", "DONE", text_delta="final plan")
    result(response="final plan")
elif MODE == "policy_denied_multiline":
    step(0, "tool", "ACTIVE", tool_info={"name": "run_command"})
    step(0, "tool", "ERROR", tool_info={"name": "run_command", "error": {
        "type": "TOOL_ERROR", "message": _POLICY_DENY_MULTILINE}})
    step(1, "agent_response", "DONE", text_delta="final plan")
    result(response="final plan")
elif MODE == "policy_denied_no_answer":
    policy_denied_command()
    result(response="")
elif MODE == "policy_denied_plus_error":
    policy_denied_command()
    step(1, "tool", "ERROR", tool_info={"name": "read_file", "error": {
        "type": "TOOL_ERROR", "message": "private diagnostic"}})
    result(response="final plan")
elif MODE == "soft_deny_review":
    step(0, "tool", "DONE", tool_info={"name": "read_file"})
    os.write(2, b'Print mode: soft-denying tool confirmation "ReadFile" at step 6\n')
    result(response="partial")
elif MODE == "soft_deny_with_tool_error":
    step(0, "tool", "ERROR", tool_info={"name": "read_file", "error": {
        "type": "TOOL_ERROR", "message": "private diagnostic"}})
    os.write(2, b'Print mode: soft-denying tool confirmation "ReadFile" at step 6\n')
    result(response="partial")
elif MODE == "policy_lookalike_path":
    step(0, "tool", "ERROR", tool_info={"name": "read_file", "error": {
        "type": "TOOL_ERROR",
        "message": "File not found: /workspace/permission check failed/input.txt"}})
    step(1, "agent_response", "DONE", text_delta="final plan")
    result(response="final plan")
elif MODE == "policy_denied_extra_field":
    step(0, "tool", "ERROR", tool_info={"name": "run_command", "error": {
        "type": "TOOL_ERROR", "message": _POLICY_DENY_MESSAGE,
        "execution_error": "private diagnostic"}})
    step(1, "agent_response", "DONE", text_delta="final plan")
    result(response="final plan")
elif MODE == "policy_denied_prefix_suffix":
    step(0, "tool", "ERROR", tool_info={"name": "run_command", "error": {
        "type": "TOOL_ERROR", "message": "note: " + _POLICY_DENY_MESSAGE + " trailing"}})
    step(1, "agent_response", "DONE", text_delta="final plan")
    result(response="final plan")
elif MODE == "policy_denied_secret_tool":
    step(0, "tool", "ERROR", tool_info={"name": "PRIVATE_CUSTOMER_TOKEN_123", "error": {
        "type": "TOOL_ERROR", "message": _POLICY_DENY_MESSAGE}})
    step(1, "agent_response", "DONE", text_delta="final plan")
    result(response="final plan")
elif MODE == "soft_deny_secret_tool":
    step(0, "tool", "DONE", tool_info={"name": "PRIVATE_CUSTOMER_TOKEN_123"})
    os.write(2, b'Print mode: soft-denying tool confirmation "PRIVATE_CUSTOMER_TOKEN_123" at step 6\n')
    result(response="partial")
elif MODE == "review_denied_stream":
    step(0, "tool", "ACTIVE", tool_name="read_file", tool_info={"name": "read_file"})
    step(0, "tool", "ERROR", tool_name="read_file", tool_info={
        "name": "read_file",
        "error": {"type": "TOOL_ERROR", "message": _REVIEW_STREAM_MESSAGE}})
    os.write(2, b"permission denied\n")
    result(response="partial")
elif MODE == "review_denied_forged":
    step(0, "tool", "ACTIVE", tool_name="read_file", tool_info={"name": "read_file"})
    step(0, "tool", "ERROR", tool_name="read_file", tool_info={
        "name": "read_file",
        "error": {"type": "TOOL_ERROR", "message": _REVIEW_FORGED_MESSAGE}})
    result(response="partial")
elif MODE == "review_denied_suffix":
    step(0, "tool", "ACTIVE", tool_name="read_file", tool_info={"name": "read_file"})
    step(0, "tool", "ERROR", tool_name="read_file", tool_info={
        "name": "read_file",
        "error": {"type": "TOOL_ERROR", "message": _REVIEW_STREAM_MESSAGE + "\nextra line"}})
    result(response="partial")
elif MODE == "review_plus_tool_error":
    step(0, "tool", "ACTIVE", tool_name="read_file", tool_info={"name": "read_file"})
    step(0, "tool", "ERROR", tool_name="read_file", tool_info={
        "name": "read_file",
        "error": {"type": "TOOL_ERROR", "message": _REVIEW_STREAM_MESSAGE}})
    step(1, "tool", "ERROR", tool_info={"name": "read_file", "error": {
        "type": "TOOL_ERROR", "message": "private diagnostic"}})
    result(response="partial")
elif MODE == "policy_denied_bad_escape":
    # \0 is not three octal digits. Accepting it would mark this hard denial
    # and, with a final answer, report success.
    step(0, "tool", "ACTIVE", tool_info={"name": "run_command"})
    step(0, "tool", "ERROR", tool_info={"name": "run_command", "error": {
        "type": "TOOL_ERROR", "message": (
            'permission check failed for unsandboxed "\\0": '
            "Permission denied for unsandboxed(\x00). "
            "Matches user-configured deny rule."
        )}})
    step(1, "agent_response", "DONE", text_delta="final plan")
    result(response="final plan")
elif MODE == "review_denied_bad_escape":
    # \x-1 is not two hex digits. Decoding it used to raise and leave the run pending.
    step(0, "tool", "ACTIVE", tool_name="read_file", tool_info={"name": "read_file"})
    step(0, "tool", "ERROR", tool_name="read_file", tool_info={
        "name": "read_file",
        "error": {"type": "TOOL_ERROR", "message": review_deny_message(
            "read_file", "ls\\x-1", "ls\\x-1")}})
    result(response="partial")
elif MODE == "policy_utf8_match":
    step(0, "tool", "ACTIVE", tool_info={"name": "run_command"})
    step(0, "tool", "ERROR", tool_info={"name": "run_command", "error": {
        "type": "TOOL_ERROR", "message": policy_deny_message(
            "unsandboxed", _UTF8_E_QUOTED, _UTF8_E)}})
    step(1, "agent_response", "DONE", text_delta="final plan")
    result(response="final plan")
elif MODE == "policy_utf8_mojibake":
    step(0, "tool", "ACTIVE", tool_info={"name": "run_command"})
    step(0, "tool", "ERROR", tool_info={"name": "run_command", "error": {
        "type": "TOOL_ERROR", "message": policy_deny_message(
            "unsandboxed", _UTF8_E_QUOTED, _UTF8_E_AS_CODEPOINTS)}})
    step(1, "agent_response", "DONE", text_delta="final plan")
    result(response="final plan")
elif MODE == "review_utf8_match":
    step(0, "tool", "ACTIVE", tool_name="read_file", tool_info={"name": "read_file"})
    step(0, "tool", "ERROR", tool_name="read_file", tool_info={
        "name": "read_file",
        "error": {"type": "TOOL_ERROR", "message": review_deny_message(
            "read_file", _UTF8_E_QUOTED, _UTF8_E)}})
    result(response="partial")
elif MODE == "review_utf8_mojibake":
    step(0, "tool", "ACTIVE", tool_name="read_file", tool_info={"name": "read_file"})
    step(0, "tool", "ERROR", tool_name="read_file", tool_info={
        "name": "read_file",
        "error": {"type": "TOOL_ERROR", "message": review_deny_message(
            "read_file", _UTF8_E_QUOTED, _UTF8_E_AS_CODEPOINTS)}})
    result(response="partial")
elif MODE == "policy_active_deny_then_error":
    # The deny sentence arrives on ACTIVE. The terminal ERROR is ordinary.
    step(0, "tool", "ACTIVE", tool_info={"name": "run_command", "error": {
        "type": "TOOL_ERROR", "message": _POLICY_DENY_MESSAGE}})
    step(0, "tool", "ERROR", tool_info={"name": "run_command", "error": {
        "type": "TOOL_ERROR", "message": "private diagnostic"}})
    step(1, "agent_response", "DONE", text_delta="final plan")
    result(response="final plan")
elif MODE == "review_active_deny_then_error":
    step(0, "tool", "ACTIVE", tool_name="read_file", tool_info={
        "name": "read_file",
        "error": {"type": "TOOL_ERROR", "message": _REVIEW_STREAM_MESSAGE}})
    step(0, "tool", "ERROR", tool_name="read_file", tool_info={
        "name": "read_file",
        "error": {"type": "TOOL_ERROR", "message": "private diagnostic"}})
    step(1, "agent_response", "DONE", text_delta="final plan")
    result(response="final plan")
elif MODE == "soft_and_hard_deny":
    policy_denied_command()
    step(1, "tool", "ACTIVE", tool_name="read_file", tool_info={"name": "read_file"})
    step(1, "tool", "ERROR", tool_name="read_file", tool_info={
        "name": "read_file",
        "error": {"type": "TOOL_ERROR", "message": _REVIEW_STREAM_MESSAGE}})
    step(2, "agent_response", "DONE", text_delta="final plan")
    result(response="final plan")
elif MODE.startswith("review_step_db"):
    _emit_review_step_db(MODE)
else:
    step(0, "user_input", text_delta=message["message"]["content"])
    step(1, "thinking", text_delta="private reasoning")
    step(2, "tool", tool_info={"name": "read_file", "output": "private content"})
    step(3, state="ACTIVE", text_delta="fixture ")
    step(3, text_delta="response")
    result()
