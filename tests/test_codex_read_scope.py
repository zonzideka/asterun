from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from asterun.backends.codex import CodexBackend, CodexAppServerTransport, build_codex_app_server_command
from asterun.backends.codex_protocol import READ_SCOPE_CONFIG, verify_read_scope_session
from asterun.config import BackendConfig
from asterun.errors import AsterunError
from asterun.read_scope import FixedReadScope


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "note.txt").write_text("fixed evidence\n")
    data = (root / "note.txt").read_bytes()
    manifest = {"schema": "asterun-read-scope/v1", "files": [{"name": "note.txt", "path": "note.txt",
                "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}]}
    (root / "scope.json").write_text(json.dumps(manifest))
    binding = {"manifest_path": "scope.json", "manifest_sha256": hashlib.sha256((root / "scope.json").read_bytes()).hexdigest()}
    reader = FixedReadScope(root, binding)
    binary = tmp_path / "codex"
    source = Path(__file__).parent / "fixtures/codex_read_scope_server.py"
    binary.write_text(f"#!{sys.executable}\n" + source.read_text())
    binary.chmod(0o700)
    monkeypatch.setenv("ASTERUN_RUN_CODEX", "1")
    backend = CodexBackend(BackendConfig(name="codex", kind="codex", enabled=True, bin=str(binary)))
    yield root, binary, reader, backend
    backend.close()


def run(fixture, text="read", *, native=None, stopping=None, on_publish=None):
    root, binary, reader, backend = fixture
    updates = []
    def publish(update):
        updates.append(update)
        if on_publish:
            on_publish(update)
    result = backend.dispatch_stream(SimpleNamespace(value="task-test"), SimpleNamespace(value="run-test"),
        "", text, cwd=root, native=native or {}, publish=publish,
        stopping=stopping or Event(), read_scope=reader)
    return result, updates


def calls(root):
    return [json.loads(line) for line in (root / "scope-calls.jsonl").read_text().splitlines()]


def test_fixed_reader_rpc_finishes_without_approval_and_keeps_native_home(fixture):
    root, _, reader, _ = fixture
    result, updates = run(fixture)
    assert result["status"] == "succeeded", result
    assert all(not item.get("approvals") for item in updates)
    assert result["native"]["read_scope_binding"] == reader.binding
    rows = calls(root)
    assert not any(row.get("method") == "account/read" for row in rows)
    start = next(row["params"] for row in rows if row.get("method") == "thread/start")
    turn = next(row["params"] for row in rows if row.get("method") == "turn/start")
    assert start["environments"] == turn["environments"] == []
    assert start["cwd"] == str(root)
    assert start["config"]["mcp_servers"] == {"fixture": {"enabled": False}}
    assert "private-credential" not in json.dumps(rows + updates + [result])
    output = next(row["result"] for row in rows if row.get("id") == "request")
    assert output["success"] is True and "fixed evidence" in output["contentItems"][0]["text"]


@pytest.mark.parametrize("marker", ["bad-mcp", "bad-permissions", "bad-instructions"])
def test_native_profile_mismatch_fails_before_turn_start(fixture, marker):
    root, _, _, _ = fixture
    (root / marker).touch()
    result, _ = run(fixture)
    assert result["status"] == "failed" and result["terminated"]
    assert result["error_code"] == "CAPABILITY_UNSUPPORTED"
    assert not any(row.get("method") == "turn/start" for row in calls(root))
    assert result["native"]["asterun.codex_dispatch_v1"]["delivery"] == "not_sent"


@pytest.mark.parametrize("version", ["codex-cli 0.153.5", "codex-cli 0.154.0-alpha.6.2", "codex-cli 99.0.0"])
def test_version_label_does_not_gate_compatible_protocol(fixture, version):
    root, binary, _, _ = fixture
    binary.write_text(binary.read_text().replace("codex-cli 0.153.4", version))
    result, _ = run(fixture)
    assert result["status"] == "succeeded", result
    assert sum(row.get("method") == "turn/start" for row in calls(root)) == 1


def test_compatible_protocol_does_not_require_version_command(fixture):
    _, binary, _, _ = fixture
    binary.write_text(binary.read_text().replace('print("codex-cli 0.153.4")', "raise SystemExit(1)"))
    result, _ = run(fixture)
    assert result["status"] == "succeeded", result


@pytest.mark.parametrize("old,new", [
    ('"environments": {"type": ["array", "null"]}', '"removedEnvironments": {}'),
    ('"dynamicTools": {}', '"removedDynamicTools": {}'),
    ('"config": {}', '"removedConfig": {}'),
    ('"arguments", "callId", "threadId", "tool", "turnId"', '"arguments", "callId", "tool", "turnId"'),
    ('["contentItems", "success"]', '["contentItems", "success", "newRequiredField"]'),
])
def test_incompatible_protocol_never_starts_app_server(fixture, old, new):
    root, binary, _, _ = fixture
    original = binary.read_text()
    assert old in original
    binary.write_text(original.replace(old, new))
    result, _ = run(fixture)
    assert result["status"] == "failed" and result["error_code"] == "CAPABILITY_UNSUPPORTED"
    assert not (root / "scope-calls.jsonl").exists()
    assert result["native"]["asterun.codex_dispatch_v1"]["delivery"] == "not_sent"


@pytest.mark.parametrize("text", ["shell", "wrong-thread", "wrong-tool", "wrong-path"])
def test_scope_violation_cannot_become_success_or_manual_approval(fixture, text):
    result, updates = run(fixture, text)
    assert result["status"] == "failed", result
    assert result["native"]["read_scope_violation"] is True
    assert all(not update.get("approvals") for update in updates)
    reconciled = fixture[-1].reconcile_native(result["native"], fixture[0])
    assert reconciled["status"] == "failed"
    assert sum(row.get("method") == "turn/start" for row in calls(fixture[0])) == 1


def test_native_resume_reuses_thread_and_persisted_tools(fixture):
    result, _ = run(fixture)
    resumed = fixture[-1].resume_native(result["native"], fixture[0], read_scope=fixture[2])
    assert resumed["native_resumed"]
    second, _ = run(fixture, native=result["native"])
    assert second["status"] == "succeeded", second
    assert second["native"]["thread_id"] == result["native"]["thread_id"]
    assert second["native"]["turn_id"] != result["native"]["turn_id"]
    assert sum(row.get("method") == "thread/start" for row in calls(fixture[0])) == 1


def test_reader_tampering_fails_entire_run(fixture):
    (fixture[0] / "note.txt").write_text("changed evidence")
    result, _ = run(fixture)
    assert result["status"] == "failed" and result["native"]["read_scope_violation"]


def test_violation_is_durable_before_native_reply(fixture):
    checkpoints = []
    def published(update):
        if update.get("native", {}).get("read_scope_violation") and not checkpoints:
            checkpoints.append(True)
            assert not any(row.get("id") == "request" and "result" in row for row in calls(fixture[0]))
    result, _ = run(fixture, "shell", on_publish=published)
    assert checkpoints == [True] and result["status"] == "failed"


def test_violation_with_lost_start_reply_survives_restart_reconcile(fixture):
    result, _ = run(fixture, "violation-lost-reply")
    assert result["status"] == "pending_reconcile" and not result["terminated"]
    assert result["native"]["read_scope_violation"] is True
    assert result["native"]["turn_id"] == "scope-turn-1"
    # Persist/reload the result, and use a new backend/transport process.
    native = json.loads(json.dumps(result["native"]))
    backend = CodexBackend(fixture[-1].config)
    try:
        reconciled = backend.reconcile_native(native, fixture[0])
    finally:
        backend.close()
    assert reconciled["status"] == "failed"
    assert sum(row.get("method") == "turn/start" for row in calls(fixture[0])) == 1
    assert not any(row.get("id") == "unanswered" and "result" in row for row in calls(fixture[0]))


def test_pending_violation_and_observer_stop_keep_reconcile_failure(fixture):
    stopping = Event()
    injected = []
    def published(update):
        if update["status"] == "running" and not injected:
            injected.append(True)
            session = fixture[-1].runtime.sessions["run-test"]
            session.transport._transport._handle_message({"id": "late-request", "method": "item/tool/call",
                "params": {"threadId": session.session_id, "turnId": session.current_turn_id,
                           "callId": "late-call", "tool": "execute", "arguments": {}}})
            stopping.set()
    result, _ = run(fixture, "slow", stopping=stopping, on_publish=published)
    assert result["status"] == "pending_reconcile" and result["native"]["read_scope_violation"]
    path = fixture[0] / "scope-native.json"
    data = json.loads(path.read_text())
    data[result["native"]["thread_id"]]["turns"][-1]["status"] = "completed"
    path.write_text(json.dumps(data))
    assert fixture[-1].reconcile_native(result["native"], fixture[0])["status"] == "failed"


@pytest.mark.parametrize("request_order", ["before_running_publish", "after_running_publish"])
def test_request_callback_race_never_exposes_manual_approval(fixture, monkeypatch, request_order):
    from threading import Timer
    from asterun.backends.codex import CodexSession

    entered, release, running_published, stopping = Event(), Event(), Event(), Event()
    failures, windows = [], []
    original_register = CodexAppServerTransport.on_request
    original_start = CodexAppServerTransport.start_session_turn
    original_handle = CodexSession.handle_message
    original_snapshot = CodexSession.to_status_dict

    def wait_for(event, description):
        if not event.wait(5):
            failures.append(description)

    def register(transport, callback):
        def deferred(message):
            entered.set()
            try:
                wait_for(release, "pending request was not observed before callback timeout")
            finally:
                # Transport callbacks swallow exceptions; always enqueue the request
                # and report synchronization failures from the main test instead.
                callback(message)
        original_register(transport, deferred)

    def handle(session, message):
        if request_order == "after_running_publish" and message.get("method") == "item/tool/call":
            wait_for(running_published, "initial running update was not published")
        original_handle(session, message)

    def start(transport, session, **kwargs):
        result = original_start(transport, session, **kwargs)
        if request_order == "before_running_publish":
            wait_for(entered, "request callback did not enter before runtime observation")
        return result

    def snapshot(session):
        result = original_snapshot(session)
        if result["pending_requests"] and entered.is_set() and not release.is_set():
            windows.append({"status": result["status"],
                            "running_published": running_published.is_set()})
            # The runtime receives a real pending snapshot while its reader queue
            # is still empty. Release independently of deduplicated public updates.
            release.set()
        return result

    monkeypatch.setattr(CodexAppServerTransport, "on_request", register)
    monkeypatch.setattr(CodexAppServerTransport, "start_session_turn", start)
    monkeypatch.setattr(CodexSession, "handle_message", handle)
    monkeypatch.setattr(CodexSession, "to_status_dict", snapshot)

    def published(update):
        if update["status"] == "running":
            running_published.set()

    def timed_out():
        failures.append("runtime did not finish within the test deadline")
        stopping.set()
        running_published.set()
        release.set()

    timer = Timer(20, timed_out)
    timer.daemon = True
    timer.start()
    try:
        result, updates = run(fixture, on_publish=published, stopping=stopping)
    finally:
        release.set()
        running_published.set()
        stopping.set()
        timer.cancel()
        timer.join(1)

    assert not failures, failures
    assert entered.is_set() and windows == [{"status": "waiting_input",
        "running_published": request_order == "after_running_publish"}]
    assert result["status"] == "succeeded", result
    assert all(not update.get("approvals") and update["status"] != "waiting_input" for update in updates)


def test_native_cancel_retains_confirmed_interruption(fixture):
    backend = fixture[-1]
    result = {}
    def execute():
        result["run"], _ = run(fixture, "slow")
    thread = Thread(target=execute)
    thread.start()
    import time
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with backend.runtime.lock:
            session = backend.runtime.sessions.get("run-test")
        if session and session.current_turn_id:
            break
        time.sleep(.02)
    backend.runtime.cancel(SimpleNamespace(value="run-test"))
    thread.join(10)
    assert not thread.is_alive()
    assert result["run"]["status"] == "cancelled"


def test_ordinary_command_unchanged_and_scoped_command_has_no_shell_wrapper():
    assert build_codex_app_server_command("codex") == ["codex", "app-server", "--listen", "stdio://"]
    scoped = build_codex_app_server_command("codex", config_overrides=READ_SCOPE_CONFIG)
    assert "--strict-config" in scoped and "features.shell_tool=false" in scoped
    assert "-lc" not in scoped


@pytest.mark.parametrize("name", ["AGENTS.md", "AGENTS.override.md"])
def test_only_exact_native_home_user_instruction_paths_are_allowed(tmp_path, name):
    home = tmp_path / "native-home"
    transport = SimpleNamespace(native_home=home, request=lambda *args: {"data": [], "nextCursor": None})
    session = SimpleNamespace(session_id="thread", thread={"approvalPolicy": "never",
        "sandbox": {"type": "readOnly"}, "instructionSources": [str(home / name)]})
    verify_read_scope_session(transport, session)
    session.thread["instructionSources"] = [str(tmp_path / "workspace" / name)]
    with pytest.raises(AsterunError):
        verify_read_scope_session(transport, session)
