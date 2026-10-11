import json
import sys

import pytest

from asterun.application import Application
from asterun.grok_profile import CODE_PROFILE, prepare_home
from tests.conftest import write_config


PERMISSION = {"kind": "permission_denied", "source": "native_event", "tool": "run_terminal_command"}
REFUSAL = "User cancelled the execution for tool `run_terminal_command`"


def options(tmp_path, monkeypatch):
    home = tmp_path / "grok-home"
    prepare_home(home, execution_profile=CODE_PROFILE)
    binary = tmp_path / "grok-fake"
    binary.write_text(f"#!{sys.executable}\n" + '''
import json, pathlib, sys
args = sys.argv
sid = args[args.index("--session-id") + 1]
prompt = pathlib.Path(args[args.index("--prompt-file") + 1]).read_text()
refusal = "User cancelled the execution for tool `run_terminal_command`"

def emit(event):
    print(json.dumps(event), flush=True)

def call():
    emit({"type": "tool_call", "toolCallId": "c1", "toolName": "run_terminal_command", "status": "pending"})

def failed(text):
    emit({"type": "tool_call_update", "toolCallId": "c1", "status": "failed", "content": [
        {"type": "content", "content": {"type": "text", "text": text}}]})

def finish(reason, code=0):
    emit({"type": "text", "data": "model summary"})
    emit({"type": "end", "sessionId": sid, "stopReason": reason})
    raise SystemExit(code)

if prompt == "refuse":
    call(); failed(refusal); finish("cancelled")
elif prompt == "refuse-exit1":
    call(); failed(refusal); finish("cancelled", 1)
elif prompt == "cancel":
    finish("cancelled")
elif prompt == "other-fail":
    call(); failed("command exited 1"); finish("cancelled")
elif prompt == "recovered":
    call(); failed(refusal); finish("end_turn")
elif prompt == "budget":
    call(); failed(refusal)
    emit({"type": "max_turns_reached"})
    finish("cancelled")
else:
    emit({"type": "text", "data": "UNEXPECTED " + prompt})
    finish("end_turn", 2)
''')
    binary.chmod(0o755)
    monkeypatch.setenv("ASTERUN_RUN_GROK", "1")
    return {"kind": "grok", "home": str(home), "bin": str(binary),
            "execution_profile": CODE_PROFILE, "session_policy": "resume"}


@pytest.mark.parametrize(("prompt", "status", "code", "failure"), [
    ("refuse", "failed", "GROK_PERMISSION_DENIED", PERMISSION),
    ("refuse-exit1", "failed", "GROK_PERMISSION_DENIED", PERMISSION),
    ("cancel", "cancelled", None, None),
    ("other-fail", "cancelled", None, None),
    ("recovered", "succeeded", None, None),
    ("budget", "failed", "GROK_RUN_INCOMPLETE", {"kind": "turn_limit", "source": "native_event"}),
])
def test_permission_refusal_terminal(isolated_env, workspace_root, monkeypatch, prompt, status, code, failure):
    conf = write_config(isolated_env / "cfg.json", workspace_root, {"grok": options(isolated_env, monkeypatch)})
    app = Application.from_paths(conf, isolated_env / "state")
    try:
        result = app.handle("task.submit", {"workspace": "demo", "backend": "grok", "text": prompt})
        assert result.ok, result.error
        run = result.data["run"]
        assert run["status"] == status and run["terminated"] is True
        assert run["error_code"] == code
        assert run["native"].get("provider_failure") == failure
        assert run["summary"] == "model summary"
        assert REFUSAL not in json.dumps(run["native"])
        assert app.scheduler.snapshot()["inflight"] == {}
    finally:
        app.close()
