from pathlib import Path
from threading import Event
import json
import sys

import pytest

from asterun.application import Application
from asterun.backends.grok import (
    GrokBackend, CODE_TOOLS, DISPATCH_RECEIPT_KEY, build_grok_code_command,
)
from asterun.config import BackendConfig, load_config
from asterun.grok_profile import CODE_PROFILE, CODE_SANDBOX, prepare_home
from asterun.ids import RunId, TaskId
from tests.conftest import write_config


def test_coding_command_has_explicit_tool_scope_and_bounds(tmp_path):
    cmd = build_grok_code_command("grok", cwd=tmp_path, prompt_file=tmp_path / "prompt",
                                  session_id="native", model="grok-4.6", max_turns=8)
    assert cmd[cmd.index("--sandbox") + 1] == CODE_SANDBOX
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert cmd[cmd.index("--tools") + 1].split(",") == list(CODE_TOOLS)
    assert cmd[cmd.index("--max-turns") + 1] == "8"
    assert cmd[-2:] == ["--output-format", "streaming-json"]
    assert "agent" not in cmd and "--always-approve" not in cmd
    assert cmd[cmd.index("--disallowed-tools") + 1] == "search_tool,use_tool,Agent"
    assert "MCPTool(*)" in cmd


def setup_backend(tmp_path, monkeypatch):
    home = tmp_path / "native-home"
    prepare_home(home, execution_profile=CODE_PROFILE)
    binary = tmp_path / "grok-fixture"
    binary.write_text(f"#!{sys.executable}\n" + '''import json, pathlib, sys
args = sys.argv
sid = args[args.index('--session-id') + 1]
cwd = pathlib.Path(args[args.index('--cwd') + 1])
prompt = pathlib.Path(args[args.index('--prompt-file') + 1])
assert prompt.stat().st_mode & 0o077 == 0
assert prompt.read_text() == 'Implement the TR'
assert pathlib.Path.cwd() == cwd
(cwd / 'implemented.txt').write_text('done')
for event in [
    {'type': 'text', 'data': 'implemented'},
    {'type': 'end', 'sessionId': sid, 'stopReason': 'end_turn', 'num_turns': 3},
]:
    print(json.dumps(event), flush=True)
''')
    binary.chmod(0o755)
    monkeypatch.setenv("ASTERUN_RUN_GROK", "1")
    return {"kind": "grok", "bin": str(binary), "home": str(home),
            "execution_profile": CODE_PROFILE, "max_turns": 5, "timeout_seconds": 10}


@pytest.mark.parametrize("stream", [False, True])
def test_explicit_coding_dispatch_uses_actual_workspace(isolated_env, workspace_root, monkeypatch, stream):
    options = setup_backend(isolated_env, monkeypatch)
    backend = GrokBackend(BackendConfig(name="grok", **options))
    updates = []
    kwargs = {"publish": updates.append, "stopping": Event(), "native": {"session_id": "previous"}} if stream else {}
    dispatch = backend.dispatch_stream if stream else backend.dispatch
    result = dispatch(TaskId("task"), RunId("run"), "", "Implement the TR", cwd=workspace_root, **kwargs)
    assert result["status"] == "succeeded"
    assert (workspace_root / "implemented.txt").read_text() == "done"
    assert result["native"]["num_turns"] == 3
    assert result["native"]["native_resumed"] is False
    assert result["native"]["session_id"] != "previous"
    assert backend.dispatch_count == 1
    if stream:
        assert updates[0]["native"][DISPATCH_RECEIPT_KEY]["delivery"] == "not_sent"
        assert updates[0]["native"]["session_id"] == result["native"]["session_id"]
        assert result["events"] == []


def test_coding_application_preserves_session_and_native_usage(isolated_env, workspace_root, monkeypatch):
    options = setup_backend(isolated_env, monkeypatch)
    config = load_config(write_config(isolated_env / "config.json", workspace_root,
                                      extra_backends={"grok-code": options}))
    app = Application(config)
    try:
        result = app.handle("task.submit", {"workspace": "demo", "backend": "grok-code", "text": "Implement the TR"})
        assert result.ok, result.error
        assert result.data["run"]["status"] == "succeeded"
        assert result.data["run"]["native"]["num_turns"] == 3
        assert result.data["session"]["backend_session_id"] == result.data["run"]["native"]["session_id"]
        assert result.data["task"]["acceptance"] != "passed"
    finally:
        app.close()


def test_unexpected_runner_exception_never_claims_not_sent(isolated_env, workspace_root, monkeypatch):
    backend = GrokBackend(BackendConfig(name="grok", **setup_backend(isolated_env, monkeypatch)))
    def fail(**kwargs):
        raise OSError("private diagnostics")
    monkeypatch.setattr("asterun.backends.grok_code.run_code", fail)
    result = backend.dispatch(TaskId("task"), RunId("run"), "", "Implement the TR", cwd=workspace_root)
    assert result["status"] == "pending_reconcile"
    assert result["native"][DISPATCH_RECEIPT_KEY]["delivery"] == "may_have_been_sent"
    assert "private diagnostics" not in json.dumps(result)
