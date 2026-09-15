"""用临时 SQLite 和离线 fake 验证用量报告的入口、范围和只读边界。"""
from dataclasses import replace
import json
import sqlite3

import pytest

from asterun.application import Application
from asterun.cli import _app as open_cli_app, _load_request, build_parser
from asterun.ids import RunId, TaskId
from asterun.errors import AsterunError
from asterun.mcp_server import handle_rpc
from asterun.policy import DenyAll, Principal
from asterun.sqlite_store import SqliteStore
from tests.test_cli import _run
from tests.test_control_runtime import create_task, start_command


def _native():
    return {"usage_source": "native_headless_report", "usage": {
        "input_tokens": 10, "cache_read_input_tokens": 80, "output_tokens": 20,
        "total_tokens": 110, "reasoning_tokens": 4},
        "modelUsage": {"grok-fixture": {"inputTokens": 10, "cacheReadInputTokens": 80,
                                       "outputTokens": 20, "reasoningTokens": 4}},
        "total_cost_usd": 0.1}


def _seed_chain(app):
    submitted = app.handle("task.submit", {"workspace": "demo", "text": "offline usage fixture"})
    assert submitted.ok, submitted.error
    task = app.store.get_task(TaskId(submitted.ids["task_id"]))
    first = app.store.get_run(task.current_run_id)
    first.native = _native()
    app.store.save_run(first)
    repaired = replace(first, id=RunId("run_usage_repair"), native=_native(), role="repair")
    app.store.save_run(repaired)
    # A third attempt is persisted without provider counters: it must not vanish.
    unknown = replace(first, id=RunId("run_usage_unknown"), native={}, role="review")
    app.store.save_run(unknown)
    task.current_run_id = unknown.id
    task.repair_count = 1
    task.run_count = 3
    app.store.save_task(task)
    return task


def test_sqlite_reopen_reports_all_attempts_and_does_not_poll_or_write(config_path, isolated_env, monkeypatch):
    state = isolated_env / "usage-state"
    app = Application.from_paths(config_path, state)
    task = _seed_chain(app)
    app.close()
    app = Application.from_paths(config_path, state)
    try:
        def forbidden(*args, **kwargs):
            raise AssertionError("用量报告不应推进执行或调用后端")
        monkeypatch.setattr(app, "poll", forbidden)
        monkeypatch.setattr(app.backends["fake"], "dispatch", forbidden)
        before = app.store._conn.total_changes
        request = {"task_id": task.id.value, "include_runs": True}
        first = app.handle("usage.report", request)
        second = app.handle("usage.report", request)
        assert first.ok, first.error
        assert second.to_dict() == first.to_dict()
        assert app.store._conn.total_changes == before
        assert app.backends["fake"].dispatch_count == 0
        data = first.data
        assert data["readonly"] is True
        assert data["task_status_source"] == "persisted_snapshot"
        assert data["acceptance_revalidated"] is False
        assert data["totals"]["run_count"] == 3
        assert data["totals"]["tokens"]["total_tokens"] == {
            "known_sum": 220, "reported_runs": 2, "unknown_runs": 1, "partial": True}
        assert data["totals"]["tokens"]["cached_input_tokens"]["known_sum"] == 160
        assert {row["role"] for row in data["runs"]} == {"implementation", "repair", "review"}
        assert data["tasks"][0]["current_run_id"] == "run_usage_unknown"
        assert data["tasks"][0]["repair_count"] == 1
        assert data["tasks"][0]["run_count"] == 3
        brief = app.handle("usage.report", {"conversation_id": task.conversation_id.value})
        assert brief.ok and "runs" not in brief.data
        assert brief.data["totals"] == data["totals"]
    finally:
        app.close()


def test_report_scope_does_not_include_another_task(config_path, isolated_env):
    app = Application.from_paths(config_path, isolated_env / "usage-state")
    try:
        task = _seed_chain(app)
        other = app.handle("task.submit", {"workspace": "demo", "text": "other offline task"})
        assert other.ok, other.error
        report = app.handle("usage.report", {"task_id": task.id.value})
        assert report.ok and report.data["totals"]["run_count"] == 3
        mismatch = app.handle("usage.report", {"task_id": task.id.value,
            "conversation_id": other.ids["conversation_id"]})
        assert not mismatch.ok and mismatch.error["code"] == "INVALID_REQUEST"
        all_tasks = app.handle("usage.report", {"workspace": "demo"})
        assert all_tasks.ok and all_tasks.data["totals"]["run_count"] == 4
        assert all_tasks.data["totals"]["task_count"] == 2
    finally:
        app.close()


@pytest.mark.parametrize("scope", ["workspace", "task_id", "conversation_id"])
def test_all_scope_forms_enforce_workspace_read_authorization(config_path, isolated_env, monkeypatch, scope):
    app = Application.from_paths(config_path, isolated_env / "usage-state")
    try:
        task = _seed_chain(app)
        values = {"workspace": "demo", "task_id": task.id.value, "conversation_id": task.conversation_id.value}
        monkeypatch.setattr(app, "policy", DenyAll())
        result = app.handle("usage.report", {scope: values[scope], "include_runs": True})
        assert not result.ok and result.error["code"] == "SCOPE_DENIED"
        assert result.data is None
    finally:
        app.close()


@pytest.mark.parametrize("scope", ["workspace", "task_id", "conversation_id"])
def test_standard_control_owner_cannot_be_bypassed_by_usage_report(config_path, isolated_env, monkeypatch, scope):
    app = Application.from_paths(config_path, isolated_env / "usage-state")
    try:
        control = app.control
        task = create_task(control)
        started = control.command(start_command(control, task))
        control.advance()
        assert control.get("Run", started["result_ref"])["status"] == "SUCCEEDED"
        action = control.store.list("Action", control.namespace)[0]
        legacy = app.store.get_task(TaskId(action["extensions"]["asterun.legacy_task_id"]))
        values = {"workspace": legacy.workspace, "task_id": legacy.id.value,
                  "conversation_id": legacy.conversation_id.value}
        allowed = app.handle("usage.report", {scope: values[scope]})
        assert allowed.ok, allowed.error
        monkeypatch.setattr(app, "principal", Principal("another-principal", "test"))
        denied = app.handle("usage.report", {scope: values[scope], "include_runs": True})
        assert not denied.ok and denied.error["code"] == "FORBIDDEN"
        assert denied.data is None
    finally:
        app.close()


def test_mcp_schema_invocation_and_invalid_scope_are_consistent(config_path, isolated_env, monkeypatch):
    app = Application.from_paths(config_path, isolated_env / "usage-state")
    try:
        task = _seed_chain(app)
        monkeypatch.setattr(app, "poll", lambda **kwargs: pytest.fail("MCP 用量查询不能 poll"))
        listed = handle_rpc(app, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tool = next(tool for tool in listed["result"]["tools"] if tool["name"] == "usage_report")
        schema = tool["inputSchema"]
        assert set(schema["properties"]) == {"task_id", "workspace", "conversation_id", "include_runs"}
        assert schema["additionalProperties"] is False
        request = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "usage_report", "arguments": {"task_id": task.id.value, "include_runs": True}}}
        response = handle_rpc(app, request)["result"]
        assert response["isError"] is False
        assert json.loads(response["content"][0]["text"])["data"]["totals"]["run_count"] == 3
        for arguments in ({}, {"task_id": task.id.value, "include_runs": "true"}):
            request["params"]["arguments"] = arguments
            response = handle_rpc(app, request)["result"]
            assert response["isError"] is True
            assert json.loads(response["content"][0]["text"])["error"]["code"] == "INVALID_REQUEST"
    finally:
        app.close()


def test_cli_payload_and_sqlite_report_roundtrip(config_path, isolated_env):
    args = build_parser().parse_args(["usage-report", "--workspace", "demo", "--include-runs"])
    assert _load_request(args) == {"workspace": "demo", "include_runs": True}
    state = isolated_env / "usage-state"
    app = Application.from_paths(config_path, state)
    task = _seed_chain(app)
    app.close()
    result = _run(["--config", str(config_path), "--state-dir", str(state),
                   "usage-report", "--task-id", task.id.value, "--include-runs"])
    assert result.returncode == 0, result.stderr + result.stdout
    report = json.loads(result.stdout)
    assert report["ok"] and report["data"]["readonly"]
    assert report["data"]["totals"]["tokens"]["total_tokens"]["known_sum"] == 220
    assert len(report["data"]["runs"]) == 3


def test_standalone_cli_report_does_not_reconcile_an_unfinished_run(config_path, isolated_env):
    state = isolated_env / "usage-state"
    app = Application.from_paths(config_path, state)
    submitted = app.handle("task.submit", {"workspace": "demo", "script": "cancel_accept_only"})
    assert submitted.ok and submitted.data["run"]["status"] == "running"
    run_id = RunId(submitted.ids["run_id"])
    before = app.store.get_run(run_id).to_dict()
    operation_before = app.store.find_operation_for_run(run_id).to_dict()
    events_before = [event.to_dict() for event in app.store.list_events(run_id)]
    app.close()
    result = _run(["--config", str(config_path), "--state-dir", str(state),
                   "usage-report", "--task-id", submitted.ids["task_id"], "--include-runs"])
    assert result.returncode == 0, result.stderr + result.stdout
    store = SqliteStore(state / "asterun.sqlite")
    try:
        assert store.get_run(run_id).to_dict() == before
        assert store.find_operation_for_run(run_id).to_dict() == operation_before
        assert [event.to_dict() for event in store.list_events(run_id)] == events_before
    finally:
        store.close()


@pytest.mark.parametrize("command,args,restore,background", [
    ("usage-report", ["--workspace", "demo"], False, False),
    ("task-get", ["task-existing"], True, False),
    ("serve", [], True, True),
])
def test_readonly_open_is_specific_to_usage_cli(config_path, isolated_env, monkeypatch,
                                              command, args, restore, background):
    captured = {}
    sentinel = object()
    def factory(*positional, **kwargs):
        captured.update(kwargs)
        return sentinel
    monkeypatch.setattr(Application, "from_paths", factory)
    parsed = build_parser().parse_args(["--config", str(config_path), "--state-dir", str(isolated_env / "state"),
                                       command, *args])
    assert open_cli_app(parsed) is sentinel
    assert captured["restore_scheduler"] is restore
    assert captured["background"] is background


def test_readonly_application_open_preserves_database_bytes_and_rejects_writes(config_path, isolated_env):
    state = isolated_env / "usage-state"
    app = Application.from_paths(config_path, state)
    task = _seed_chain(app)
    app.close()
    path = state / "asterun.sqlite"
    before = path.read_bytes()
    app = Application.from_paths(config_path, state, restore_scheduler=False)
    try:
        assert app.store.readonly is True and app.executor is None
        assert app.store._conn.total_changes == 0
        report = app.handle("usage.report", {"task_id": task.id.value})
        assert report.ok, report.error
        assert app.store._conn.total_changes == 0
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            app.store._conn.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
        assert app.store._conn.total_changes == 0
        assert app.store.migration_backup is None
    finally:
        app.close()
    assert path.read_bytes() == before
    assert not list(state.glob("*.schema-*.sqlite"))


@pytest.mark.parametrize("version", ["5", "999"])
def test_readonly_old_or_future_schema_is_rejected_without_migration(tmp_path, version):
    path = tmp_path / "asterun.sqlite"
    store = SqliteStore(path)
    store._conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (version,))
    store.close()
    before = path.read_bytes()
    with pytest.raises(AsterunError) as error:
        SqliteStore(path, readonly=True)
    assert error.value.code == "INTENT_PERSIST_FAILED"
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.schema-*.sqlite"))


def test_readonly_missing_database_is_not_created(tmp_path):
    path = tmp_path / "absent" / "asterun.sqlite"
    with pytest.raises(AsterunError) as error:
        SqliteStore(path, readonly=True)
    assert error.value.code == "NOT_FOUND"
    assert not path.parent.exists()


def test_readonly_incomplete_current_schema_is_not_repaired(tmp_path):
    path = tmp_path / "asterun.sqlite"
    store = SqliteStore(path)
    store._conn.execute("DROP TABLE control_entities")
    store.close()
    before = path.read_bytes()
    with pytest.raises(AsterunError) as error:
        SqliteStore(path, readonly=True)
    assert error.value.code == "INTENT_PERSIST_FAILED"
    assert path.read_bytes() == before


def test_readonly_usage_never_constructs_backends(config_path, isolated_env, monkeypatch):
    state = isolated_env / "usage-state"
    app = Application.from_paths(config_path, state)
    task = _seed_chain(app)
    assert "fake" in app.backends
    app.close()

    def forbidden(*args, **kwargs):
        raise AssertionError("只读用量报告不能构造后端或触发原生认证")

    monkeypatch.setattr("asterun.application.build_backends", forbidden)
    app = Application.from_paths(config_path, state, restore_scheduler=False)
    try:
        assert app.backends == {}
        result = app.handle("usage.report", {"task_id": task.id.value})
        assert result.ok, result.error
        assert result.data["totals"]["tokens"]["total_tokens"]["known_sum"] == 220
        assert app.store._conn.total_changes == 0
    finally:
        app.close()
