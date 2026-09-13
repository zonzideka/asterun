from __future__ import annotations

import io
import hashlib
import json
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from asterun.application import Application
from asterun.contracts import RunStatus
from asterun.ids import RunId
from asterun.mcp_server import _read_message, _write_message, handle_rpc
from asterun.policy import Grant, Principal


@pytest.fixture
def persistent_app(config_path, isolated_env):
    app = Application.from_paths(config_path, isolated_env / "state", principal=Principal("test:user", "test"))
    try:
        yield app
    finally:
        app.close()


def submit(app, text="输入", **extra):
    result = app.handle("task.submit", {"workspace": "demo", "text": text, **extra})
    assert result.ok, result.error
    return result


def test_persistent_repair_and_retry_at_budget(persistent_app):
    app = persistent_app
    app.config.workflow.max_repairs = 1
    original = submit(app)
    payload = {"task_id": original.ids["task_id"], "idempotency_key": "repair-once"}
    repaired = app.handle("workflow.repair", payload)
    assert repaired.ok, repaired.error
    repeated = app.handle("workflow.repair", payload)
    assert repeated.ok, repeated.error
    assert repeated.data["run"]["id"] == repaired.data["run"]["id"]
    assert app.store.get_run(RunId(original.ids["run_id"])).status == RunStatus.SUCCEEDED
    assert repaired.data["task"]["repair_count"] == 1
    assert app.backends["fake"].dispatch_count == 2


def test_repair_idempotency_is_scoped_to_task(app):
    first, second = submit(app), submit(app)
    repaired = []
    for item in (first, second):
        result = app.handle("workflow.repair", {"task_id": item.ids["task_id"], "idempotency_key": "same"})
        assert result.ok, result.error
        repaired.append(result)
    assert repaired[0].data["task"]["id"] != repaired[1].data["task"]["id"]
    assert app.backends["fake"].dispatch_count == 4


def test_request_cannot_raise_configured_repair_budget(app):
    app.config.workflow.max_repairs = 0
    original = submit(app)
    denied = app.handle("workflow.repair", {"task_id": original.ids["task_id"], "max_repairs": 99})
    assert not denied.ok
    assert app.backends["fake"].dispatch_count == 1


def test_cancelled_queued_run_never_dispatches(app):
    holder = submit(app, script="cancel_and_stop")
    queued = submit(app, script="cancel_and_stop")
    cancelled = app.handle("task.cancel", {"task_id": queued.ids["task_id"]})
    assert cancelled.ok and cancelled.data["terminated"]
    assert not [c for c in app.backends["fake"].calls if c.method == "request_cancel"]
    app.handle("task.cancel", {"task_id": holder.ids["task_id"]})
    assert app.backends["fake"].dispatch_count == 1
    assert app.handle("task.get", {"task_id": queued.ids["task_id"]}).data["run"]["status"] == "cancelled"


def test_queued_dispatch_checks_current_policy(app):
    from asterun.policy import AllowConfiguredWorkspaces, Decision

    class RevokedSubmission(AllowConfiguredWorkspaces):
        def authorize(self, principal, action, resource):
            if action == "task.submit":
                return Decision(False, "SCOPE_DENIED", "提交授权已撤回")
            return super().authorize(principal, action, resource)

    holder = submit(app, script="cancel_and_stop")
    queued = submit(app)
    app.policy = RevokedSubmission({"demo"})
    assert app.handle("task.cancel", {"task_id": holder.ids["task_id"]}).ok
    assert app.backends["fake"].dispatch_count == 1
    rejected = app.handle("task.get", {"task_id": queued.ids["task_id"]})
    assert rejected.data["run"]["error_code"] == "SCOPE_DENIED"


def test_waiting_approval_holds_slot_and_reply_drains(app):
    waiting = submit(app, script="wait_input")
    queued = submit(app)
    assert queued.data["queued"]
    assert app.backends["fake"].dispatch_count == 1
    result = app.handle("approval.respond", {"approval_id": waiting.data["dispatch"]["approval_id"], "decision": "approve"})
    assert result.ok
    assert app.backends["fake"].dispatch_count == 2
    assert app.handle("task.get", {"task_id": queued.ids["task_id"]}).data["run"]["status"] == "succeeded"


def test_review_availability_is_not_review_evidence(app):
    original = submit(app)
    result = app.handle("workflow.evaluate", {
        "task_id": original.ids["task_id"], "require_review": True, "review_backend": "fake",
        "checks": [{"kind": "file_exists", "path": "note.txt"}],
    })
    assert result.ok
    assert result.data["acceptance"] == "pending"
    assert result.data["review_status"] != "passed"
    assert result.data["paused_for_review"]


def test_contains_does_not_accept_prompt_as_output(app):
    original = submit(app, "期待但未输出的内容", script="events")
    result = app.handle("workflow.evaluate", {
        "task_id": original.ids["task_id"],
        "checks": [{"kind": "contains", "text": "期待但未输出的内容"}],
    })
    assert result.data["acceptance"] == "failed"


def test_limited_grant_does_not_cover_global_operations(app):
    app.grant = Grant(resources=("demo",))
    result = app.handle("config.apply", {"expected_revision": 1})
    assert not result.ok and result.error["code"] == "SCOPE_DENIED"


def test_mcp_uses_newline_delimited_unicode_messages():
    messages = [{"jsonrpc": "2.0", "id": i, "method": "ping", "params": {"text": "中文"}} for i in (1, 2)]
    stream = io.StringIO("".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages))
    assert [_read_message(stream), _read_message(stream)] == messages
    output = io.StringIO()
    _write_message(output, messages[0])
    assert json.loads(output.getvalue()) == messages[0]
    assert output.getvalue().endswith("\n")


@pytest.mark.parametrize("payload", [{"cursor": "bad"}, {"page_size": 0}, {"cursor": -1}, {"page_size": True}])
def test_invalid_event_arguments_are_structured_errors(app, payload):
    task = submit(app)
    result = app.handle("task.events", {"task_id": task.ids["task_id"], **payload})
    assert not result.ok and result.error["code"] == "INVALID_REQUEST"


def test_mcp_tool_schemas_describe_required_arguments(app):
    result = handle_rpc(app, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    schemas = {tool["name"]: tool["inputSchema"] for tool in result["result"]["tools"]}
    assert "workspace" in schemas["task_submit"]["required"]
    assert "task_id" in schemas["task_get"]["required"]
    assert schemas["task_events"]["properties"]["page_size"]["maximum"] == 500


def test_approval_cannot_complete_cancelled_run(app):
    original = submit(app, script="wait_input")
    run = app.store.get_run(RunId(original.ids["run_id"]))
    run.status = RunStatus.CANCELLED
    app.store.save_run(run)
    result = app.handle("approval.respond", {"approval_id": original.data["dispatch"]["approval_id"], "decision": "approve"})
    assert not result.ok
    assert not [c for c in app.backends["fake"].calls if c.method == "respond_approval"]


def test_repair_failed_intent_preserves_task_counters(persistent_app):
    app = persistent_app
    original = submit(app)
    app.store.fail_next_intent = True
    result = app.handle("workflow.repair", {"task_id": original.ids["task_id"]})
    assert not result.ok
    fetched = app.handle("task.get", {"task_id": original.ids["task_id"]})
    assert fetched.data["task"]["repair_count"] == 0
    assert fetched.data["run"]["id"] == original.ids["run_id"]
    assert app.backends["fake"].dispatch_count == 1


def test_timeout_reserves_slot_and_cannot_be_repaired(app):
    unknown = submit(app, script="timeout")
    queued = submit(app)
    assert queued.data["queued"]
    assert not app.handle("workflow.repair", {"task_id": unknown.ids["task_id"]}).ok
    assert app.backends["fake"].dispatch_count == 1


def test_restart_restores_queue_without_repeating_dispatched_run(config_path, isolated_env):
    state = isolated_env / "state"
    first = Application.from_paths(config_path, state)
    try:
        holder = submit(first, script="cancel_and_stop")
        queued = submit(first)
    finally:
        first.close()
    second = Application.from_paths(config_path, state)
    try:
        assert second.backends["fake"].dispatch_count == 0
        assert second.scheduler.snapshot()["queued"] == 1
        assert second.scheduler.snapshot()["inflight"] == {"fake": 1}
        assert second.handle("task.get", {"task_id": holder.ids["task_id"]}).data["run"]["status"] == "pending_reconcile"
        assert second.handle("task.cancel", {"task_id": holder.ids["task_id"]}).ok
        assert second.backends["fake"].dispatch_count == 1
        assert second.handle("task.get", {"task_id": queued.ids["task_id"]}).data["run"]["status"] == "succeeded"
    finally:
        second.close()


def test_resume_previously_paused_queue_drains(app):
    holder = submit(app, script="cancel_and_stop")
    queued = submit(app)
    assert app.handle("task.handoff", {"task_id": queued.ids["task_id"], "action": "pause"}).ok
    app.handle("task.cancel", {"task_id": holder.ids["task_id"]})
    assert app.backends["fake"].dispatch_count == 1
    resumed = app.handle("task.handoff", {"task_id": queued.ids["task_id"], "action": "resume"})
    assert resumed.ok and resumed.data["run"]["status"] == "succeeded"


def test_backend_exception_records_unknown_and_deduplicates_retry(app, monkeypatch):
    calls = []

    def lost(*args, **kwargs):
        calls.append(args)
        raise TimeoutError("测试回复丢失")

    monkeypatch.setattr(app.backends["fake"], "dispatch", lost)
    payload = {"workspace": "demo", "idempotency_key": "lost-once"}
    first = app.handle("task.submit", payload)
    assert first.error["code"] == "REMOTE_STATE_UNKNOWN"
    second = app.handle("task.submit", payload)
    assert second.ok and second.data["reused"]
    assert second.data["run"]["status"] == "pending_reconcile"
    assert len(calls) == 1


def test_grant_expiry_compares_instants_not_iso_strings():
    assert Grant(expires_at="2026-09-09T10:00:00+08:00").is_expired("2026-09-09T03:00:00Z")
    assert Grant(expires_at="not-a-date").is_expired("2026-09-09T03:00:00Z")


@pytest.mark.parametrize("damage", ["bad_json", "future_version", "wrong_hash", "bad_database", "bad_schema", "symlink", "duplicate"])
def test_invalid_backup_does_not_replace_existing_state(persistent_app, isolated_env, damage):
    app = persistent_app
    original = submit(app, text="必须保留的原任务")
    archive = isolated_env / "original.tar.gz"
    assert app.handle("state.backup", {"output": str(archive)}).ok
    with tarfile.open(archive) as stream:
        contents = {m.name: stream.extractfile(m).read() for m in stream.getmembers()}
    manifest = json.loads(contents["manifest.json"])
    if damage == "bad_json":
        contents["manifest.json"] = b"not json"
    elif damage == "future_version":
        manifest["schema_version"] = 999
        contents["manifest.json"] = json.dumps(manifest).encode()
    elif damage == "wrong_hash":
        manifest["sha256"] = "0" * 64
        contents["manifest.json"] = json.dumps(manifest).encode()
    elif damage == "bad_database":
        contents["asterun.sqlite"] = b"not a database"
        manifest.pop("sha256")
        contents["manifest.json"] = json.dumps(manifest).encode()
    elif damage == "bad_schema":
        malformed = isolated_env / "malformed.sqlite"
        malformed.write_bytes(contents["asterun.sqlite"])
        with sqlite3.connect(malformed) as conn:
            conn.execute("DROP TABLE operations")
            conn.execute("CREATE TABLE operations(id TEXT)")
        conn.close()
        contents["asterun.sqlite"] = malformed.read_bytes()
        manifest["sha256"] = hashlib.sha256(contents["asterun.sqlite"]).hexdigest()
        contents["manifest.json"] = json.dumps(manifest).encode()
    damaged = isolated_env / "damaged.tar.gz"
    with tarfile.open(damaged, "w:gz") as stream:
        for name, body in contents.items():
            member = tarfile.TarInfo(name)
            member.size = len(body)
            if damage == "symlink" and name == "asterun.sqlite":
                member.type = tarfile.SYMTYPE
                member.linkname = "../outside.sqlite"
                member.size = 0
            stream.addfile(member, io.BytesIO(body))
        if damage == "duplicate":
            stream.addfile(tarfile.TarInfo("manifest.json"), io.BytesIO())
    result = app.handle("state.restore", {"input": str(damaged)})
    assert not result.ok
    preserved = app.handle("task.get", {"task_id": original.ids["task_id"]})
    assert preserved.ok and preserved.data["task"]["text"] == "必须保留的原任务"
    assert not list(app.state_dir.glob(".asterun-restore-*"))


def test_backup_cannot_overwrite_database_and_active_restore_is_refused(persistent_app, isolated_env):
    app = persistent_app
    original = submit(app)
    assert not app.handle("state.backup", {"output": str(app.state_dir / "asterun.sqlite")}).ok
    archive = isolated_env / "state.tar.gz"
    assert app.handle("state.backup", {"output": str(archive)}).ok
    assert archive.stat().st_mode & 0o777 == 0o600
    submit(app, script="cancel_and_stop")
    assert not app.handle("state.restore", {"input": str(archive)}).ok
    assert app.handle("task.get", {"task_id": original.ids["task_id"]}).ok


def test_cli_can_restore_over_corrupt_database(config_path, persistent_app, isolated_env, capsys):
    from asterun.cli import main

    original = submit(persistent_app)
    archive = isolated_env / "state.tar.gz"
    assert persistent_app.handle("state.backup", {"output": str(archive)}).ok
    target = isolated_env / "damaged-state"
    target.mkdir()
    (target / "asterun.sqlite").write_bytes(b"not a database")
    assert main(["--config", str(config_path), "--state-dir", str(target), "state-restore", "--input", str(archive)]) == 0
    app = Application.from_paths(config_path, target)
    try:
        assert app.handle("task.get", {"task_id": original.ids["task_id"]}).ok
    finally:
        app.close()


def test_failed_start_releases_lock_and_does_not_migrate_future_schema(config_path, isolated_env):
    from asterun.errors import AsterunError
    from asterun.instance import InstanceLock

    state = isolated_env / "future-state"
    state.mkdir()
    with sqlite3.connect(state / "asterun.sqlite") as conn:
        conn.execute("CREATE TABLE meta(key TEXT, value TEXT)")
        conn.execute("INSERT INTO meta VALUES ('schema_version', '999')")
    conn.close()
    with pytest.raises(AsterunError):
        Application.from_paths(config_path, state)
    lock = InstanceLock(state / "asterun.lock")
    lock.acquire()
    lock.release()
    with sqlite3.connect(state / "asterun.sqlite") as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("meta",)]
    conn.close()


def test_mcp_subprocess_handles_unicode_invalid_messages_and_continues(config_path, isolated_env):
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "task_submit", "arguments": {"workspace": "demo", "text": "中文输入"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "task_events", "arguments": []}},
        {"jsonrpc": "2.0", "id": 4, "method": "ping"},
    ]
    wire = "not-json\nnull\n" + "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages)
    result = subprocess.run([sys.executable, "-m", "asterun", "--config", str(config_path),
                             "--state-dir", str(isolated_env / "mcp-state"), "mcp"],
                            input=wire, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    replies = [json.loads(line) for line in result.stdout.splitlines()]
    assert [r.get("id") for r in replies] == [None, None, 1, 2, 3, 4]
    assert replies[0]["error"]["code"] == -32700
    assert replies[1]["error"]["code"] == -32600
    payload = json.loads(replies[3]["result"]["content"][0]["text"])
    assert payload["data"]["run"]["summary"] == "中文输入"
    assert replies[4]["error"]["code"] == -32602
    assert replies[5]["result"] == {}


def test_session_survives_failure_after_intent_commit(config_path, persistent_app, monkeypatch):
    app = persistent_app

    def write_error(*args):
        raise OSError("模拟意图提交之后的事件写入失败")

    monkeypatch.setattr(app.store, "append_event", write_error)
    request = {"workspace": "demo", "text": "输入", "idempotency_key": "committed"}
    failed = app.handle("task.submit", request)
    assert not failed.ok and app.backends["fake"].dispatch_count == 0
    state = app.state_dir
    app.close()
    recovered = Application.from_paths(config_path, state, principal=Principal("test:user", "test"))
    try:
        replay = recovered.handle("task.submit", request)
        assert replay.ok, replay.error
        assert replay.data["reused"] and replay.data["session"]
        resumed = recovered.handle("task.handoff", {"task_id": replay.ids["task_id"], "action": "resume"})
        assert resumed.ok and resumed.data["run"]["status"] == "succeeded"
        assert recovered.backends["fake"].dispatch_count == 1
    finally:
        recovered.close()


@pytest.mark.parametrize("path", ["missing.txt", "."])
def test_non_file_input_is_rejected_before_dispatch(app, path):
    result = app.handle("task.submit", {"workspace": "demo", "path": path})
    assert not result.ok and result.error["code"] == "PATH_INVALID"
    assert app.backends["fake"].dispatch_count == 0
