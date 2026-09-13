from __future__ import annotations

from io import StringIO
import json
import subprocess
import sys

from asterun.application import Application
from asterun.config import load_config
from asterun.mcp_server import _read_message, _write_message, handle_rpc
from asterun.policy import Principal
from asterun.store import MemoryStore


def test_mcp_lists_tools_and_submits(config_path):
    app = Application(
        load_config(config_path),
        store=MemoryStore(),
        principal=Principal("test:user", "test"),
    )
    listed = handle_rpc(app, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {item["name"] for item in listed["result"]["tools"]}
    assert "task_submit" in names
    assert "config_validate" in names
    assert "config_apply" in names
    assert "task_handoff" in names
    assert "task_reconcile" in names
    assert "workflow_evaluate" in names
    assert "workflow_repair" in names
    assert "scheduler_status" in names
    assert "diagnose" in names
    assert "state_backup" in names
    called = handle_rpc(
        app,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "task_submit", "arguments": {"workspace": "demo", "script": "success"}},
        },
    )
    assert called["result"]["isError"] is False


def test_stdio_json_line_roundtrip():
    outgoing = StringIO()
    _write_message(outgoing, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    incoming = StringIO(outgoing.getvalue())
    message = _read_message(incoming)
    assert message["method"] == "initialize"


def test_invalid_utf8_does_not_leave_mcp_waiting_forever(config_path, isolated_env):
    result = subprocess.run([sys.executable, "-m", "asterun", "--config", str(config_path),
        "--state-dir", str(isolated_env / "invalid-wire"), "mcp"], input=b"\xff\n", capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["error"]["code"] == -32700
