"""客户端展示与执行状态分离；所有原生进程均为本地协议夹具。"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from threading import Event, Thread
import time

import pytest

from asterun.application import Application
from asterun.backends.codex import CodexBackend
from asterun.ids import AccountRef, RunId, TaskId
from asterun.policy import DenyAll, Grant
from tests.test_native_lifecycle import native_config, submit, wait_for


@pytest.fixture
def presentation_config(native_config):
    raw = json.loads(native_config.read_text())
    raw["backends"]["codex"]["desktop_projects"] = True
    native_config.write_text(json.dumps(raw))
    return native_config


@pytest.fixture
def presentation(monkeypatch):
    state = {"calls": [], "registration": "registered", "association": "associated"}

    def present(self, transport, cwd, thread_id):
        if self.config.desktop_projects is not True:
            return None
        report = {"registration": state["registration"], "association": state["association"],
                  "client_visibility": "not_verified", "reason": "test_project_report",
                  "thread_id": thread_id, "native_cwd": str(cwd),
                  "project_id": "project-demo", "project_root": str(cwd)}
        state["calls"].append({"cwd": Path(cwd), "thread_id": thread_id, "report": deepcopy(report)})
        return report

    monkeypatch.setattr(CodexBackend, "_present_project", present)
    return state


@pytest.fixture
def applications(presentation_config, isolated_env):
    opened = []

    def open_app(*, background=True, entry="core", state_name="state"):
        app = Application.from_paths(presentation_config, isolated_env / state_name,
                                     background=background, entry=entry)
        opened.append(app)
        return app

    yield open_app
    for app in reversed(opened):
        app.close()


def _calls(root):
    path = root / "native-calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _execution_counts(root):
    calls = _calls(root)
    return {method: sum(call.get("method") == method for call in calls)
            for method in ("thread/start", "thread/resume", "turn/start", "turn/interrupt")}


def _finished(app, task_id, status="succeeded"):
    data = wait_for(app, task_id, lambda item: item["run"]["status"] == status)
    # A final result can be persisted just before its worker consumes the ack.
    # task.present correctly refuses a still-live worker, so wait for that ack.
    deadline = time.monotonic() + 5
    while app.executor and data["run"]["id"] in app.executor.workers:
        assert time.monotonic() < deadline
        app.poll()
        time.sleep(0.01)
    return app.handle("task.get", {"task_id": task_id}).data


def _execution_state(app, task_id):
    task = app.store.get_task(TaskId(task_id))
    run = app.store.get_run(task.current_run_id)
    encoded = deepcopy(run.to_dict())
    encoded["native"].pop("desktop_project", None)
    operation = app.store.find_operation_for_run(run.id)
    counts = {table: app.store._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
              for table in ("tasks", "runs", "sessions", "operations", "approvals")}
    approvals = [tuple(row) for row in app.store._conn.execute("SELECT id,payload FROM approvals ORDER BY id")]
    return {"task": deepcopy(task.to_dict()), "run": encoded, "operation": deepcopy(operation.to_dict()),
            "counts": counts, "approvals": approvals}


def test_async_presentation_preserves_original_project_cwd_and_execution_permissions(
        applications, presentation, workspace_root):
    app = applications()
    result = submit(app, "finish")
    final = _finished(app, result.ids["task_id"])
    report = final["run"]["native"]["desktop_project"]
    assert len(presentation["calls"]) == 1
    assert presentation["calls"][0]["cwd"] == workspace_root
    assert report == presentation["calls"][0]["report"]
    assert report["thread_id"] == final["run"]["native"]["thread_id"] == "native-thread-1"
    assert report["project_root"] == str(workspace_root)
    assert report["client_visibility"] == "not_verified"
    assert final["task"]["acceptance"] == "pending"
    calls = _calls(workspace_root)
    start = next(call for call in calls if call.get("method") == "thread/start")
    assert start["params"]["approvalPolicy"] == "untrusted"
    assert start["params"]["sandbox"] == "workspace-write"
    assert _execution_counts(workspace_root) == {
        "thread/start": 1, "thread/resume": 0, "turn/start": 1, "turn/interrupt": 0}


def test_project_presentation_follows_native_reference_persistence(
        applications, monkeypatch, workspace_root):
    app = applications()
    saved = Event()
    presented = []
    original = app.store.save_session

    def save(session):
        result = original(session)
        if session.backend_session_id:
            saved.set()
        return result

    def present(self, transport, cwd, thread_id):
        presented.append(saved.is_set())
        return {"registration": "registered", "thread_id": thread_id}

    monkeypatch.setattr(app.store, "save_session", save)
    monkeypatch.setattr(CodexBackend, "_present_project", present)
    result = submit(app, "finish")
    _finished(app, result.ids["task_id"])
    assert presented == [True]
    assert _execution_counts(workspace_root)["turn/start"] == 1


def test_native_reference_persistence_failure_prevents_presentation_and_turn(
        applications, presentation, workspace_root, monkeypatch):
    app = applications()
    original = app.store.save_session
    failures = []

    def save(session):
        if session.backend_session_id and not failures:
            failures.append(True)
            raise OSError("injected persistence failure")
        return original(session)

    monkeypatch.setattr(app.store, "save_session", save)
    result = submit(app, "must not start a turn")
    deadline = time.monotonic() + 5
    while not failures and time.monotonic() < deadline:
        try:
            app.poll()
        except OSError:
            pass
        time.sleep(0.01)
    assert failures
    _finished(app, result.ids["task_id"], "pending_reconcile")
    assert presentation["calls"] == []
    assert _execution_counts(workspace_root)["thread/start"] == 1
    assert _execution_counts(workspace_root)["turn/start"] == 0


def test_failed_presentation_does_not_change_approval_or_idempotency(
        applications, presentation, workspace_root):
    presentation.update(registration="unknown", association="unknown")
    app = applications()
    first = submit(app, "approval", idempotency_key="presentation-approval")
    waiting = wait_for(app, first.ids["task_id"], lambda data: bool(data["approval"]))
    assert waiting["run"]["native"]["desktop_project"]["association"] == "unknown"
    before = _execution_state(app, first.ids["task_id"])
    counts = _execution_counts(workspace_root)
    denied = app.handle("task.present", {"task_id": first.ids["task_id"]})
    assert not denied.ok and denied.error["code"] == "INVALID_REQUEST"
    assert _execution_state(app, first.ids["task_id"]) == before
    assert _execution_counts(workspace_root) == counts
    repeated = submit(app, "approval", idempotency_key="presentation-approval")
    assert repeated.ids == first.ids
    assert repeated.data["reused"] is True
    approval = waiting["approval"]
    response = app.handle("approval.respond", {"approval_id": approval["id"], "decision": "approve",
                                               "expected_target_hash": approval["target_hash"]})
    assert response.ok, response.error
    final = _finished(app, first.ids["task_id"])
    assert final["run"]["status"] == "succeeded"
    assert final["run"]["native"]["desktop_project"]["association"] == "unknown"
    assert final["run"]["approval_attempts"]
    assert _execution_counts(workspace_root)["turn/start"] == 1


def test_terminal_presentation_retry_only_updates_display_without_reexecution(
        applications, presentation, workspace_root):
    presentation.update(registration="unknown", association="unknown")
    app = applications()
    first = submit(app, "finish", idempotency_key="presentation-retry")
    _finished(app, first.ids["task_id"])
    before = _execution_state(app, first.ids["task_id"])
    counts = _execution_counts(workspace_root)
    presentation.update(registration="registered", association="associated")
    result = app.handle("task.present", {"task_id": first.ids["task_id"]})
    assert result.ok, result.error
    assert result.data["task_id"] == first.ids["task_id"]
    assert result.data["run_id"] == first.ids["run_id"]
    assert result.data["re_dispatched"] is False
    assert result.data["presentation"] == presentation["calls"][-1]["report"]
    stored = app.store.get_run(RunId(first.ids["run_id"]))
    assert stored.native["desktop_project"] == result.data["presentation"]
    assert _execution_state(app, first.ids["task_id"]) == before
    assert _execution_counts(workspace_root) == counts
    repeated = submit(app, "finish", idempotency_key="presentation-retry")
    assert repeated.ids == first.ids and repeated.data["reused"] is True
    assert _execution_counts(workspace_root) == counts


def test_failed_presentation_retry_preserves_successful_execution(
        applications, presentation, workspace_root):
    app = applications()
    result = submit(app, "finish")
    _finished(app, result.ids["task_id"])
    before = _execution_state(app, result.ids["task_id"])
    counts = _execution_counts(workspace_root)
    presentation.update(registration="unknown", association="unknown")
    shown = app.handle("task.present", {"task_id": result.ids["task_id"]})
    assert shown.ok, shown.error
    assert shown.data["presentation"]["association"] == "unknown"
    assert _execution_state(app, result.ids["task_id"]) == before
    assert _execution_counts(workspace_root) == counts


def test_finished_worker_index_does_not_prevent_display_retry(
        applications, presentation, workspace_root):
    app = applications()
    result = submit(app, "finish")
    _finished(app, result.ids["task_id"])
    before = _execution_state(app, result.ids["task_id"])
    counts = _execution_counts(workspace_root)
    completed_worker = Thread(target=lambda: None)
    completed_worker.start()
    completed_worker.join()
    app.executor.workers[result.ids["run_id"]] = completed_worker
    shown = app.handle("task.present", {"task_id": result.ids["task_id"]})
    assert shown.ok, shown.error
    assert shown.data["re_dispatched"] is False
    assert _execution_state(app, result.ids["task_id"]) == before
    assert _execution_counts(workspace_root) == counts


def test_presentation_connection_error_is_structured_and_does_not_poll_or_redispatch(
        applications, presentation, workspace_root, monkeypatch):
    app = applications()
    result = submit(app, "finish")
    _finished(app, result.ids["task_id"])
    before = _execution_state(app, result.ids["task_id"])
    calls = _calls(workspace_root)
    shown_before = len(presentation["calls"])

    def unavailable(*args, **kwargs):
        raise RuntimeError("PRIVATE_CONNECTION_DIAGNOSTIC")

    def unexpected_poll(*args, **kwargs):
        raise AssertionError("展示重试不得推进执行或质量流程")

    monkeypatch.setattr(app.backends["codex"].runtime, "_connect", unavailable)
    monkeypatch.setattr(app, "poll", unexpected_poll)
    shown = app.handle("task.present", {"task_id": result.ids["task_id"]})
    assert shown.ok, shown.error
    assert shown.data["presentation"]["reason"] == "presentation_connection_failed"
    assert shown.data["presentation"]["registration"] == "unknown"
    assert shown.data["presentation"]["association"] == "unknown"
    assert shown.data["re_dispatched"] is False
    assert "PRIVATE_CONNECTION_DIAGNOSTIC" not in json.dumps(shown.to_dict())
    assert _execution_state(app, result.ids["task_id"]) == before
    assert len(presentation["calls"]) == shown_before
    assert _calls(workspace_root) == calls


def test_native_helper_exception_is_a_display_report_not_execution_failure(
        applications, monkeypatch):
    class BrokenProjects:
        def __init__(self, *args, **kwargs):
            pass

        def present(self, thread_id):
            raise RuntimeError("PRIVATE_NATIVE_DIAGNOSTIC")

    monkeypatch.setattr("asterun.backends.codex_projects.CodexProjects", BrokenProjects)
    app = applications()
    result = submit(app, "finish")
    final = _finished(app, result.ids["task_id"])
    report = final["run"]["native"]["desktop_project"]
    assert report["reason"] == "presentation_failed"
    assert report["registration"] == "unknown" and report["association"] == "unknown"
    assert "PRIVATE_NATIVE_DIAGNOSTIC" not in json.dumps(final)
    assert final["run"]["summary"] == "隔离进程已完成"


def test_synchronous_dispatch_also_records_presentation_without_second_turn(
        applications, presentation, workspace_root):
    app = applications(background=False)
    result = submit(app, "finish")
    assert result.data["run"]["status"] == "succeeded"
    assert result.data["run"]["native"]["desktop_project"] == presentation["calls"][0]["report"]
    assert len(presentation["calls"]) == 1
    assert _execution_counts(workspace_root)["thread/start"] == 1
    assert _execution_counts(workspace_root)["turn/start"] == 1


@pytest.mark.parametrize("diagnostic_failure", [False, True])
def test_synchronous_display_save_failure_releases_slot_and_reports_independent_unknown(
        applications, presentation, workspace_root, monkeypatch, diagnostic_failure):
    app = applications(background=False)
    original_save = app.store.save_run
    original_event = app.store.append_event
    failures = []

    def save(run):
        if "desktop_project" in run.native:
            failures.append(run.id.value)
            raise OSError("PRIVATE_DISPLAY_STORAGE_DIAGNOSTIC")
        return original_save(run)

    def append(run_id, event):
        if diagnostic_failure and event.source == "native.presentation":
            raise OSError("PRIVATE_EVENT_STORAGE_DIAGNOSTIC")
        return original_event(run_id, event)

    monkeypatch.setattr(app.store, "save_run", save)
    monkeypatch.setattr(app.store, "append_event", append)
    for index in range(2):
        result = submit(app, "finish", idempotency_key=f"display-save-{index}")
        assert result.data["run"]["status"] == "succeeded"
        assert result.data["run"]["terminated"] is True
        assert result.data["task"]["acceptance"] == "pending"
        assert result.data["operation"]["status"] == "completed"
        assert result.data["dispatch"]["presentation"] == {
            "report": presentation["calls"][index]["report"], "receipt_persistence": "unknown"}
        assert "desktop_project" not in result.data["run"]["native"]
        persisted = app.store.get_run(RunId(result.ids["run_id"]))
        assert persisted.status == "succeeded" and persisted.native["turn_id"]
        assert "desktop_project" not in persisted.native
        assert not app.scheduler.inflight and not app.scheduler.queue
        assert "PRIVATE_" not in json.dumps(result.to_dict())
    assert len(failures) == 2
    assert _execution_counts(workspace_root) == {
        "thread/start": 2, "thread/resume": 0, "turn/start": 2, "turn/interrupt": 0}


def test_async_display_save_failure_preserves_approval_and_dispatches_queued_task(
        applications, presentation, workspace_root, monkeypatch):
    app = applications()
    original = app.store.save_run
    failures = []

    def save(run):
        if "desktop_project" in run.native:
            failures.append(run.id.value)
            raise OSError("PRIVATE_DISPLAY_STORAGE_DIAGNOSTIC")
        return original(run)

    monkeypatch.setattr(app.store, "save_run", save)
    first = submit(app, "approval", idempotency_key="display-save-approval")
    waiting = wait_for(app, first.ids["task_id"], lambda data: bool(data["approval"]))
    assert waiting["run"]["status"] == "waiting_input"
    assert waiting["run"]["native"]["thread_id"] and waiting["run"]["native"]["turn_id"]
    assert "desktop_project" not in waiting["run"]["native"]
    assert failures
    second = submit(app, "finish", idempotency_key="display-save-queued")
    assert second.data["queued"] is True
    assert app.scheduler.inflight == {"codex": 1}
    approval = waiting["approval"]
    response = app.handle("approval.respond", {"approval_id": approval["id"], "decision": "approve",
                                               "expected_target_hash": approval["target_hash"]})
    assert response.ok, response.error
    for result in (first, second):
        final = _finished(app, result.ids["task_id"])
        assert final["run"]["terminated"] is True
        assert final["run"]["error_code"] is None
        assert final["task"]["acceptance"] == "pending"
        assert "desktop_project" not in final["run"]["native"]
        operation = app.store.find_operation_for_run(RunId(result.ids["run_id"]))
        assert operation.status == "completed"
        events = app.store.list_events(RunId(result.ids["run_id"]))
        assert any(event.source == "native.presentation"
                   and event.payload.get("receipt_persistence") == "unknown" for event in events)
    assert not app.scheduler.inflight and not app.scheduler.queue
    assert _execution_counts(workspace_root) == {
        "thread/start": 2, "thread/resume": 0, "turn/start": 2, "turn/interrupt": 0}


def test_explicit_display_save_failure_does_not_poll_or_change_execution(
        applications, presentation, workspace_root, monkeypatch):
    app = applications()
    first = submit(app, "finish")
    final = _finished(app, first.ids["task_id"])
    before = _execution_state(app, first.ids["task_id"])
    saved_native = deepcopy(final["run"]["native"])
    counts = _execution_counts(workspace_root)
    presentation["registration"] = "unknown"

    def save(run):
        raise OSError("PRIVATE_DISPLAY_STORAGE_DIAGNOSTIC")

    def unexpected_poll(*args, **kwargs):
        raise AssertionError("展示失败不得推进执行")

    monkeypatch.setattr(app.store, "save_run", save)
    monkeypatch.setattr(app, "poll", unexpected_poll)
    result = app.handle("task.present", {"task_id": first.ids["task_id"]})
    assert not result.ok and result.error["code"] == "PATH_INVALID"
    assert "PRIVATE_" not in json.dumps(result.to_dict())
    assert _execution_state(app, first.ids["task_id"]) == before
    assert app.store.get_run(RunId(first.ids["run_id"])).native == saved_native
    assert _execution_counts(workspace_root) == counts


@pytest.mark.parametrize("method", ["save_run", "save_session", "save_operation"])
def test_synchronous_result_persistence_failure_prevents_additional_display_side_effect(
        applications, presentation, workspace_root, monkeypatch, method):
    app = applications(background=False)
    original = getattr(app.store, method)
    failures = []

    def save(record):
        ready = (bool(record.native) and record.status == "succeeded" if method == "save_run" else
                 bool(record.backend_session_id) if method == "save_session" else record.status == "completed")
        if ready and not failures:
            failures.append(True)
            raise OSError("injected persistence failure")
        return original(record)

    monkeypatch.setattr(app.store, method, save)
    result = app.handle("task.submit", {"workspace": "demo", "backend": "codex", "text": "finish"})
    assert not result.ok and failures
    assert presentation["calls"] == []
    # The synchronous native turn already happened. The failed persistence only
    # prevents adding an unrecorded presentation mutation, not that past turn.
    assert _execution_counts(workspace_root)["thread/start"] == 1
    assert _execution_counts(workspace_root)["turn/start"] == 1
    native = json.loads((workspace_root / "native.json").read_text())
    assert native["native-thread-1"]["turns"][0]["status"] == "completed"


def test_presentation_survives_restart_and_retries_without_native_resume(
        applications, presentation, workspace_root):
    app = applications()
    first = submit(app, "finish")
    previous = _finished(app, first.ids["task_id"])
    app.close()
    reopened = applications()
    loaded = reopened.handle("task.get", {"task_id": first.ids["task_id"]})
    assert loaded.ok
    assert loaded.data["run"]["native"]["desktop_project"] == previous["run"]["native"]["desktop_project"]
    counts = _execution_counts(workspace_root)
    shown = reopened.handle("task.present", {"task_id": first.ids["task_id"]})
    assert shown.ok and shown.data["re_dispatched"] is False
    assert _execution_counts(workspace_root) == counts


def test_display_retry_never_turns_unknown_execution_into_success(
        applications, presentation, workspace_root):
    app = applications()
    result = submit(app, "disconnect")
    _finished(app, result.ids["task_id"], "pending_reconcile")
    before = _execution_state(app, result.ids["task_id"])
    counts = _execution_counts(workspace_root)
    shown = app.handle("task.present", {"task_id": result.ids["task_id"]})
    assert shown.ok and shown.data["re_dispatched"] is False
    assert _execution_state(app, result.ids["task_id"]) == before
    assert _execution_counts(workspace_root) == counts


@pytest.mark.parametrize("desktop_projects", [None, False])
def test_disabled_presentation_never_implicitly_enables_registration(
        applications, presentation, workspace_root, desktop_projects):
    app = applications()
    app.config.backends["codex"].desktop_projects = desktop_projects
    result = submit(app, "finish")
    final = _finished(app, result.ids["task_id"])
    assert "desktop_project" not in final["run"]["native"]
    assert presentation["calls"] == []
    before = _execution_state(app, result.ids["task_id"])
    calls = _calls(workspace_root)
    shown = app.handle("task.present", {"task_id": result.ids["task_id"]})
    assert not shown.ok and shown.error["code"] == "CAPABILITY_UNSUPPORTED"
    assert presentation["calls"] == []
    assert _execution_state(app, result.ids["task_id"]) == before
    assert _calls(workspace_root) == calls


@pytest.mark.parametrize("native_reference", [False, True])
def test_non_codex_task_cannot_present_a_substitute_native_thread(
        applications, presentation, workspace_root, native_reference):
    app = applications()
    result = app.handle("task.submit", {"workspace": "demo", "backend": "fake", "script": "success"})
    assert result.ok, result.error
    if native_reference:
        run = app.store.get_run(RunId(result.ids["run_id"]))
        run.native = {"thread_id": "another-backend-thread"}
        app.store.save_run(run)
    before = _execution_state(app, result.ids["task_id"])
    shown = app.handle("task.present", {"task_id": result.ids["task_id"]})
    assert not shown.ok
    assert shown.error["code"] == ("CAPABILITY_UNSUPPORTED" if native_reference else "REMOTE_STATE_UNKNOWN")
    assert presentation["calls"] == []
    assert _execution_state(app, result.ids["task_id"]) == before
    assert not _calls(workspace_root)


@pytest.mark.parametrize("guard,code", [
    ("workspace", "BINDING_MISMATCH"), ("account", "BINDING_MISMATCH"),
    ("grant", "AUTH_REQUIRED"), ("cli_entry", "CAPABILITY_UNSUPPORTED"),
    ("mcp_entry", "CAPABILITY_UNSUPPORTED"), ("policy", "SCOPE_DENIED"),
])
def test_presentation_rechecks_scope_authority_and_entry_before_native_calls(
        applications, presentation, workspace_root, isolated_env, guard, code):
    app = applications()
    result = submit(app, "finish")
    _finished(app, result.ids["task_id"])
    before = _execution_state(app, result.ids["task_id"])
    calls = _calls(workspace_root)
    shown_before = len(presentation["calls"])
    if guard == "workspace":
        changed = isolated_env / "other-workspace"
        changed.mkdir()
        app.config.workspaces["demo"].root = changed
    elif guard == "account":
        app.account_ref = AccountRef("account:another")
    elif guard == "grant":
        app.grant = Grant(expires_at="2000-01-01T00:00:00Z")
    elif guard.endswith("_entry"):
        app.entry = guard.split("_")[0]
        setattr(app.config.entries, app.entry, False)
    elif guard == "policy":
        app.policy = DenyAll()
    shown = app.handle("task.present", {"task_id": result.ids["task_id"]})
    assert not shown.ok and shown.error["code"] == code
    assert _execution_state(app, result.ids["task_id"]) == before
    assert len(presentation["calls"]) == shown_before
    assert _calls(workspace_root) == calls


def test_missing_native_reference_is_not_replaced_by_new_thread(
        applications, presentation, workspace_root):
    app = applications()
    result = submit(app, "finish")
    _finished(app, result.ids["task_id"])
    run = app.store.get_run(RunId(result.ids["run_id"]))
    run.native.pop("thread_id", None)
    run.native.pop("backend_session_id", None)
    app.store.save_run(run)
    before = _execution_state(app, result.ids["task_id"])
    calls = _calls(workspace_root)
    shown = app.handle("task.present", {"task_id": result.ids["task_id"]})
    assert not shown.ok and shown.error["code"] == "REMOTE_STATE_UNKNOWN"
    assert _execution_state(app, result.ids["task_id"]) == before
    assert _calls(workspace_root) == calls


def test_cli_task_present_routes_only_display_and_returns_one_envelope(
        applications, presentation, workspace_root, monkeypatch, capsys):
    from asterun.cli import main

    app = applications(entry="cli")
    result = submit(app, "finish")
    _finished(app, result.ids["task_id"])
    before = _execution_state(app, result.ids["task_id"])
    counts = _execution_counts(workspace_root)

    class LocalClient:
        handle = app.handle

        def close(self):
            pass

    monkeypatch.setattr("asterun.cli._app", lambda *args, **kwargs: LocalClient())
    assert main(["task-present", result.ids["task_id"]]) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is True
    assert envelope["data"]["task_id"] == result.ids["task_id"]
    assert envelope["data"]["re_dispatched"] is False
    assert _execution_state(app, result.ids["task_id"]) == before
    assert _execution_counts(workspace_root) == counts


def test_mcp_task_present_is_discoverable_and_does_not_submit_another_turn(
        applications, presentation, workspace_root):
    from asterun.mcp_server import handle_rpc

    app = applications(entry="mcp")
    result = submit(app, "finish")
    _finished(app, result.ids["task_id"])
    before = _execution_state(app, result.ids["task_id"])
    counts = _execution_counts(workspace_root)
    listing = handle_rpc(app, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert any(tool["name"] == "task_present" for tool in listing["result"]["tools"])
    response = handle_rpc(app, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "task_present", "arguments": {"task_id": result.ids["task_id"]}}})
    assert response["result"]["isError"] is False
    envelope = json.loads(response["result"]["content"][0]["text"])
    assert envelope["ok"] is True
    assert envelope["data"]["re_dispatched"] is False
    assert _execution_state(app, result.ids["task_id"]) == before
    assert _execution_counts(workspace_root) == counts
