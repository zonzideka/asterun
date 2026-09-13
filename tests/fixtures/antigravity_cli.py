"""独立进程协议替身；不导入 Asterun，也不调用任何真实后端。"""

import json
import os
from pathlib import Path
import signal
import sys
import time

MODE = os.environ.get("AGY_FIXTURE_MODE", "happy")
SESSION = "fixture-conversation-123"


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


if MODE == "before_input":
    init()
if MODE == "no_input":
    time.sleep(30)
    sys.exit(0)
prompt = sys.stdin.read()
message = json.loads(prompt)
assert message["event"] == "user" and isinstance(message["message"]["content"], str)
assert len(sys.argv) == 1, "prompt or non-fixture arguments appeared in argv"
if MODE == "result_only":
    result()
    sys.exit(0)
if MODE == "error_no_id":
    result("ERROR", response="", conversation_id="", error="fixture-native-secret")
    sys.exit(2)
if MODE != "before_input":
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
elif MODE == "large_response":
    result(response="\u4e2d" * 40000)
elif MODE == "result_no_exit":
    result("INTERRUPTED", "")
    time.sleep(30)
else:
    step(0, "user_input", text_delta=message["message"]["content"])
    step(1, "thinking", text_delta="private reasoning")
    step(2, "tool", tool_info={"name": "read_file", "output": "private content"})
    step(3, state="ACTIVE", text_delta="fixture ")
    step(3, text_delta="response")
    result()
