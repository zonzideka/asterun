"""控制协议在真实 MCP/IPC 边界拒绝歧义输入，不产生执行请求。"""
from __future__ import annotations

from io import StringIO
import json
from pathlib import Path
import socket
import tempfile
from threading import Event, Thread

import pytest

from asterun.contracts import Envelope
from asterun.mcp_server import _read_message, handle_rpc, serve as serve_mcp
from asterun.service import LocalClient, serve as serve_local


class RecordingApplication:
    def __init__(self, state_dir=None):
        self.state_dir = state_dir
        self.instance_lock = object()
        self.entry = "core"
        self.calls = []
        self.closed = False

    def handle(self, method, payload):
        self.calls.append((method, payload))
        return Envelope(ok=True, data={"accepted": True})

    def poll(self):
        pass

    def close(self):
        self.closed = True


@pytest.mark.parametrize("bad", [
    '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"task_submit",'
    '"arguments":{"workspace":"demo","text":"first","text":"second"}}}',
    '{"jsonrpc":"2.0","id":9007199254740992,"method":"tools/call"}',
    '{"jsonrpc":"2.0","id":NaN,"method":"tools/call"}',
    '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"arguments":{"text":"\\ud800"}}}',
])
def test_mcp_rejects_ambiguous_json_and_continues(bad):
    app = RecordingApplication()
    output = StringIO()
    serve_mcp(app, StringIO(bad + '\n{"jsonrpc":"2.0","id":2,"method":"ping"}\n'), output)
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert replies[0]["error"]["code"] == -32700
    assert replies[1] == {"jsonrpc": "2.0", "id": 2, "result": {}}
    assert app.calls == []
    assert app.closed


def test_mcp_size_limit_counts_utf8_bytes():
    incoming = StringIO(json.dumps({"text": "中" * 350000}, ensure_ascii=False) + "\n")
    with pytest.raises(json.JSONDecodeError):
        _read_message(incoming)


def test_mcp_lists_and_routes_control_tools():
    app = RecordingApplication()
    names = {tool["name"] for tool in handle_rpc(app,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]}
    controls = {"control_discovery", "control_command", "control_get", "control_events", "control_resources"}
    assert controls <= names
    for index, name in enumerate(sorted(controls)):
        reply = handle_rpc(app, {"jsonrpc": "2.0", "id": index, "method": "tools/call",
            "params": {"name": name, "arguments": {}}})
        assert not reply["result"]["isError"]
    assert {method for method, _ in app.calls} == {name.replace("_", ".", 1) for name in controls}


@pytest.fixture
def local_server():
    # macOS 的 Unix socket 路径上限短于 pytest 默认临时路径。
    with tempfile.TemporaryDirectory(prefix="asterun-wire-") as directory:
        state = Path(directory)
        state.chmod(0o700)
        app = RecordingApplication(state)
        stop, ready = Event(), Event()
        path = state / "asterun.sock"
        server = Thread(target=serve_local, args=(app, path), kwargs={"stop": stop, "ready": ready})
        server.start()
        assert ready.wait(3)
        try:
            yield app, path
        finally:
            stop.set()
            server.join(3)
            assert not server.is_alive()


def exchange(path, raw):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(3)
        connection.connect(str(path))
        connection.sendall(raw + b"\n")
        result = bytearray()
        while b"\n" not in result:
            chunk = connection.recv(65536)
            assert chunk
            result.extend(chunk)
        return json.loads(result.partition(b"\n")[0])


@pytest.mark.parametrize("raw", [
    b'{"method":"task.submit","method":"control.command","payload":{},"entry":"cli"}',
    b'{"method":"task.submit","payload":{"workspace":"demo","number":9007199254740992},"entry":"cli"}',
    b'{"method":"task.submit","payload":{"workspace":"demo","number":NaN},"entry":"cli"}',
    b'{"method":"task.submit","payload":{"workspace":"demo","text":"\xff"},"entry":"cli"}',
    b'{"method":"task.submit","payload":{"workspace":"demo","text":"\\ud800"},"entry":"cli"}',
])
def test_local_server_rejects_input_without_dispatch_and_stays_available(local_server, raw):
    app, path = local_server
    rejected = exchange(path, raw)
    assert not rejected["ok"]
    assert rejected["error"]["code"] == "INVALID_REQUEST"
    assert app.calls == []
    assert LocalClient(path).handle("control.discovery", {}).ok
    assert app.calls == [("control.discovery", {})]


@pytest.mark.parametrize("response", [None, b"", b'{"ok":true,"ok":false}\n'])
def test_control_client_lost_or_ambiguous_response_requires_reconciliation(response):
    with tempfile.TemporaryDirectory(prefix="asterun-lost-") as directory:
        path = Path(directory) / "asterun.sock"
        received = []
        release = Event()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        path.chmod(0o600)
        listener.listen(1)

        def peer():
            with listener:
                connection, _ = listener.accept()
                with connection:
                    received.append(connection.recv(65536))
                    if response is None:
                        release.wait(3)
                    elif response:
                        connection.sendall(response)

        thread = Thread(target=peer)
        thread.start()
        try:
            result = LocalClient(path, timeout=0.05 if response is None else 1).handle("control.command",
                {"request_id": "req_transport001", "idempotency_key": "original-request-key"})
        finally:
            release.set()
        thread.join(3)
        assert not thread.is_alive()
        assert not result.ok
        assert result.error["code"] == "UNKNOWN_REMOTE"
        assert result.error["retry_class"] == "after_reconcile"
        assert result.error["request_id"] == "req_transport001"
        assert len(received) == 1  # 客户端没有自动重发。
