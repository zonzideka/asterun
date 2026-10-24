import json
import sys

import pytest

from asterun.application import Application
from asterun.grok_profile import CODE_PROFILE, prepare_home
from tests.conftest import write_config


PERMISSION = {"kind": "permission_denied", "source": "native_event", "tool": "run_terminal_command"}
REFUSAL = "User cancelled the execution for tool `run_terminal_command`"
OPEN_REFUSAL = "User cancelled the execution for tool `FAKE_SECRET_TOKEN=example-value; this is an ordinary failed command"


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
open_refusal = "User cancelled the execution for tool `FAKE_SECRET_TOKEN=example-value; this is an ordinary failed command"

def emit(event):
    print(json.dumps(event), flush=True)

def call(call_id="c1", name="run_terminal_command"):
    emit({"type": "tool_call", "toolCallId": call_id, "toolName": name, "status": "pending"})

def failed(text, call_id="c1"):
    emit({"type": "tool_call_update", "toolCallId": call_id, "status": "failed", "content": [
        {"type": "content", "content": {"type": "text", "text": text}}]})

def completed(call_id, name):
    call(call_id, name)
    emit({"type": "tool_call_update", "toolCallId": call_id, "status": "completed"})

def started(call_id, name, inputs, kind=None, raw=False):
    event = {"type": "tool_call", "toolCallId": call_id, "toolName": name, "status": "pending"}
    event["rawInput" if raw else "input"] = inputs
    if kind:
        event["kind"] = kind
    emit(event)

def settled(call_id, status, as_result=False):
    emit({"type": "tool_result" if as_result else "tool_call_update", "toolCallId": call_id, "status": status})

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
elif prompt == "unclosed":
    call(); failed(open_refusal); finish("cancelled")
elif prompt == "wrong-tool":
    call(); failed("User cancelled the execution for tool `read_file`"); finish("cancelled")
elif prompt == "unknown-call":
    call("other", "run_terminal_command"); failed(refusal, "never-announced"); finish("cancelled")
elif prompt == "refusal-then-complete":
    call(); failed(refusal); completed("c2", "read_file"); finish("cancelled")
elif prompt == "edits-then-refuse":
    started("e1", "search_replace", {"path": "src/a.py"}, "edit", True)
    settled("e1", "completed")
    started("e2", "search_replace", {"path": "src/a.py"}, "edit")
    settled("e2", "completed")
    started("e3", "search_replace", {"file_path": "src/b.py"}, "edit")
    settled("e3", "completed", True)
    started("e4", "write", {"filePath": "src/c.py"})
    settled("e4", "completed")
    started("e5", "edit_file", {"path": 1, "target_file": "src/d.txt"}, "edit")
    settled("e5", "completed")
    started("e6", "create_file", {"path": "src/e.txt"})
    settled("e6", "completed")
    started("e7", "delete_file", {"path": "src/f.txt"}, "read")
    settled("e7", "completed")
    started("e8", "search_replace", {"path": {"nested": "no"}}, "edit")
    settled("e8", "completed")
    started("r1", "read_file", {"path": "src/ignored.py"}, "read")
    settled("r1", "completed")
    started("x0", "run_terminal_command", {"command": "pwd"})
    settled("x0", "completed")
    started("x0b", "run_terminal_command", {"command": "pwd"})
    settled("x0b", "completed", True)
    started("x0c", "run_terminal_cmd", {"command": "pwd"})
    settled("x0c", "completed")
    started("x0d", "run_terminal_cmd", {"command": "pwd"})
    settled("x0d", "completed", True)
    started("x1", "run_terminal_command", {"command": "true"}, "execute")
    settled("x1", "completed")
    started("x2", "run_terminal_command", {"command": "false"}, "execute")
    settled("x2", "failed")
    call(); failed(refusal); finish("cancelled")
elif prompt == "write-file-then-refuse":
    started("w1", "write_file", {"path": "src/written.py"})
    settled("w1", "completed")
    call(); failed(refusal); finish("cancelled")
elif prompt == "command-then-refuse":
    started("x1", "run_terminal_command", {"command": "true"}, "execute")
    settled("x1", "completed")
    started("x2", "bash", {"command": "true"}, "execute")
    settled("x2", "completed")
    call(); failed(refusal); finish("cancelled")
elif prompt == "edit-failed-then-refuse":
    started("e1", "search_replace", {"path": "p" * 600}, "edit")
    settled("e1", "failed")
    started("e2", "write", {"path": "src/b.py"})
    settled("e2", "in_progress")
    emit({"type": "tool_call", "toolCallId": "e3", "toolName": "search_replace", "kind": "edit",
          "status": "completed", "input": {"path": "src/c.py"}})
    started("x1", "run_terminal_command", {"command": "true"}, "execute")
    settled("x1", "failed")
    call(); failed(refusal); finish("cancelled")
elif prompt == "path-cap":
    for i in range(50):
        started("e%d" % i, "search_replace", {"path": "f%d.py" % i}, "edit")
        settled("e%d" % i, "completed")
    started("dup", "search_replace", {"path": "f0.py"}, "edit")
    settled("dup", "completed")
    started("over", "search_replace", {"path": "overflow.py"}, "edit")
    settled("over", "completed")
    call(); failed(refusal); finish("cancelled")
elif prompt == "path-long":
    started("e0", "search_replace", {"path": "p" * 600}, "edit")
    settled("e0", "completed")
    started("e1", "write", {"path": "q" * 512})
    settled("e1", "completed")
    call(); failed(refusal); finish("cancelled")
elif prompt == "edit-then-success":
    started("e1", "search_replace", {"path": "src/a.py"}, "edit")
    settled("e1", "completed")
    started("x1", "run_terminal_command", {"command": "true"}, "execute")
    settled("x1", "completed")
    finish("end_turn")
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
    ("unclosed", "cancelled", None, None),
    ("wrong-tool", "cancelled", None, None),
    ("unknown-call", "cancelled", None, None),
    ("refusal-then-complete", "cancelled", None, None),
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
        native = run["native"]
        native_json = json.dumps(native)
        assert REFUSAL not in native_json
        assert OPEN_REFUSAL not in native_json
        if code == "GROK_PERMISSION_DENIED":
            assert native["files_modified"] is False
            assert native["modified_paths"] == []
            assert native["modified_paths_truncated"] is False
            assert type(native["completed_commands"]) is int and native["completed_commands"] == 0
        else:
            assert "files_modified" not in native
            assert "modified_paths" not in native
            assert "modified_paths_truncated" not in native
            assert "completed_commands" not in native
        assert app.scheduler.snapshot()["inflight"] == {}
    finally:
        app.close()


def submit(isolated_env, workspace_root, monkeypatch, prompt):
    conf = write_config(isolated_env / "cfg.json", workspace_root, {"grok": options(isolated_env, monkeypatch)})
    app = Application.from_paths(conf, isolated_env / "state")
    try:
        result = app.handle("task.submit", {"workspace": "demo", "backend": "grok", "text": prompt})
        assert result.ok, result.error
        return result.data["run"]
    finally:
        app.close()


def assert_denial(run, *, files_modified, paths, truncated, commands):
    assert run["status"] == "failed" and run["terminated"] is True
    assert run["error_code"] == "GROK_PERMISSION_DENIED"
    native = run["native"]
    assert native.get("provider_failure") == PERMISSION
    assert native["files_modified"] is files_modified
    assert type(native["modified_paths"]) is list and native["modified_paths"] == paths
    assert native["modified_paths_truncated"] is truncated
    assert type(native["completed_commands"]) is int and native["completed_commands"] == commands


def test_permission_denied_after_completed_edits(isolated_env, workspace_root, monkeypatch):
    run = submit(isolated_env, workspace_root, monkeypatch, "edits-then-refuse")
    # Four kind-less shell completions (both names, update and result) plus one kind=execute.
    assert_denial(run, files_modified=True, truncated=False, commands=5, paths=[
        "src/a.py", "src/b.py", "src/c.py", "src/d.txt", "src/e.txt", "src/f.txt"])


def test_write_file_without_kind_counts_as_edit(isolated_env, workspace_root, monkeypatch):
    run = submit(isolated_env, workspace_root, monkeypatch, "write-file-then-refuse")
    assert_denial(run, files_modified=True, truncated=False, commands=0, paths=["src/written.py"])


def test_permission_denied_completed_command_is_not_an_edit(isolated_env, workspace_root, monkeypatch):
    run = submit(isolated_env, workspace_root, monkeypatch, "command-then-refuse")
    assert_denial(run, files_modified=False, paths=[], truncated=False, commands=2)


def test_failed_or_unfinished_edit_does_not_count(isolated_env, workspace_root, monkeypatch):
    run = submit(isolated_env, workspace_root, monkeypatch, "edit-failed-then-refuse")
    assert_denial(run, files_modified=False, paths=[], truncated=False, commands=0)


def test_modified_paths_cap(isolated_env, workspace_root, monkeypatch):
    run = submit(isolated_env, workspace_root, monkeypatch, "path-cap")
    assert_denial(run, files_modified=True, truncated=True, commands=0,
                  paths=["f%d.py" % i for i in range(50)])


def test_modified_path_truncated_to_512(isolated_env, workspace_root, monkeypatch):
    run = submit(isolated_env, workspace_root, monkeypatch, "path-long")
    assert_denial(run, files_modified=True, truncated=True, commands=0,
                  paths=["p" * 512, "q" * 512])


def test_success_does_not_report_files_modified(isolated_env, workspace_root, monkeypatch):
    run = submit(isolated_env, workspace_root, monkeypatch, "edit-then-success")
    assert run["status"] == "succeeded" and run["terminated"] is True
    assert run["error_code"] is None
    native = run["native"]
    assert "files_modified" not in native
    assert "modified_paths" not in native
    assert "modified_paths_truncated" not in native
    assert "completed_commands" not in native
