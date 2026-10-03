from copy import deepcopy
import json

import pytest

from asterun.application import Application
from asterun.backends.grok_code import RECEIPT_KEY, reconcile_recorded_terminal
from asterun.contracts import OperationStatus, RunStatus
from asterun.ids import RunId, TaskId
from tests.conftest import write_config
from tests.test_grok_resume import options


def recorded(task_id="task", run_id="run"):
    return {"execution_profile": "workspace-code-v1", "transport": "headless",
            "session_id": "native-session", "stop_reason": "cancelled",
            "provider_failure": {"kind": "turn_limit", "source": "native_event"},
            RECEIPT_KEY: {"task_id": task_id, "run_id": run_id, "stage": "prompt",
                          "prompt_attempted": True, "delivery": "may_have_been_sent",
                          "failure_type": "nonzero_exit", "process_exit_code": 1}}


@pytest.mark.parametrize("field,value", [
    ("task_id", "other"), ("run_id", "other"), ("stage", "spawn"),
    ("prompt_attempted", False), ("delivery", "not_sent"),
    ("failure_type", "event_after_end"), ("failure_type", "invalid_stdout_json"),
    ("failure_type", "runtime_timeout"), ("failure_type", "stream_not_closed"),
    ("process_exit_code", None), ("process_exit_code", -9), ("process_exit_code", True),
])
def test_uncertain_or_wrong_binding_cannot_release_a_run(field, value):
    native = recorded()
    native[RECEIPT_KEY][field] = value
    assert reconcile_recorded_terminal(native, task_id="task", run_id="run") is None


@pytest.mark.parametrize("field,value", [
    ("execution_profile", "text-only-v1"), ("transport", "acp"),
    ("session_id", ""), ("stop_reason", "unknown"), ("cleanup_incomplete", True),
])
def test_missing_native_terminal_evidence_remains_unknown(field, value):
    native = recorded()
    native[field] = value
    assert reconcile_recorded_terminal(native, task_id="task", run_id="run") is None


def test_nonzero_success_end_is_not_upgraded_to_success():
    native = recorded()
    native.update(stop_reason="end_turn", provider_failure={})
    assert reconcile_recorded_terminal(native, task_id="task", run_id="run") is None


def test_bound_recovery_preserves_original_evidence():
    native = recorded()
    original = deepcopy(native)
    result = reconcile_recorded_terminal(native, task_id="task", run_id="run")
    assert result["status"] == "failed" and result["terminated"] is True
    assert result["error_code"] == "GROK_RUN_INCOMPLETE"
    assert native == original


@pytest.mark.parametrize("recoverable", [True, False])
def test_reconcile_after_restart_releases_only_proven_slot_and_drains_queue(isolated_env, workspace_root, monkeypatch, recoverable):
    config = write_config(isolated_env / "cfg.json", workspace_root,
                          {"grok": options(isolated_env, monkeypatch)})
    state = isolated_env / "state"
    app = Application.from_paths(config, state)
    first = app.handle("task.submit", {"workspace": "demo", "backend": "grok", "text": "limit"})
    assert first.ok
    run = app.store.get_run(RunId(first.ids["run_id"]))
    task = app.store.get_task(TaskId(first.ids["task_id"]))
    session_id = run.native["session_id"]
    run.native.update(recorded(task.id.value, run.id.value), session_id=session_id)
    if not recoverable:
        run.native[RECEIPT_KEY]["failure_type"] = "runtime_timeout"
    original_native = deepcopy(run.native)
    run.status, run.error_code, run.terminated, run.cancel_requested = RunStatus.PAUSED, "REMOTE_STATE_UNKNOWN", False, True
    app.store.save_run(run)
    task.paused, task.orchestration_owner = True, "human"
    app.store.save_task(task)
    operation = app.store.find_operation_for_run(run.id)
    operation.status = OperationStatus.UNKNOWN
    app.store.save_operation(operation)
    app.close()
    app = Application.from_paths(config, state)
    try:
        assert app.scheduler.snapshot()["inflight"] == {"grok": 1}
        diagnostic = app.handle("scheduler.status", {})
        assert diagnostic.ok and diagnostic.data["active_runs"][0]["run_id"] == run.id.value
        from asterun.policy import Decision
        original_policy = app.policy
        class DenyTaskReads:
            def authorize(self, principal, action, resource):
                return Decision(False, "SCOPE_DENIED") if action == "task.get" else original_policy.authorize(principal, action, resource)
        app.policy = DenyTaskReads()
        assert app.handle("scheduler.status", {}).data["active_runs"] == []
        app.policy = original_policy
        second = app.handle("task.submit", {"workspace": "demo", "backend": "grok", "text": "new"})
        assert second.ok and second.data["queued"]
        diagnostic = app.handle("scheduler.status", {}).data
        assert diagnostic["queue"][0]["task_id"] == second.ids["task_id"]
        app.policy = DenyTaskReads()
        restricted = app.handle("scheduler.status", {}).data
        assert restricted["queue"] == [] and restricted["active_runs"] == []
        assert restricted["queued"] == 1  # 授权的容量总览保留，但不泄露无权任务引用。
        for reference in (task.id.value, run.id.value, second.ids["task_id"], second.ids["run_id"]):
            assert reference not in json.dumps(restricted)
        app.policy = original_policy
        result = app.handle("task.reconcile", {"task_id": task.id.value})
        if not recoverable:
            assert result.ok and result.data["unknown_states"]
            assert not result.data["reports"][0]["terminal"]
            assert app.store.get_run(RunId(second.ids["run_id"])).status == RunStatus.QUEUED
            assert app.scheduler.snapshot()["inflight"] == {"grok": 1}
            assert len((workspace_root / "calls.jsonl").read_text().splitlines()) == 1
            return
        assert result.ok and result.data["reports"][0]["terminal"]
        assert not result.data["unknown_states"]
        recovered = app.store.get_run(run.id)
        assert recovered.status == RunStatus.FAILED and recovered.terminated and recovered.cancel_requested
        assert recovered.native[RECEIPT_KEY] == original_native[RECEIPT_KEY]
        assert recovered.native["session_id"] == session_id
        assert app.store.get_task(task.id).paused
        assert app.store.get_run(RunId(second.ids["run_id"])).status == RunStatus.SUCCEEDED
        assert app.scheduler.snapshot()["inflight"] == {}
        assert app.handle("task.reconcile", {"task_id": task.id.value}).ok
        assert len((workspace_root / "calls.jsonl").read_text().splitlines()) == 2
    finally:
        app.close()
