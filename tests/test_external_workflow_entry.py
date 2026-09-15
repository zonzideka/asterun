from __future__ import annotations

from dataclasses import replace
import hashlib
import json

import pytest

from asterun.application import Application
from asterun.cli import _load_request, build_parser
from asterun.contracts import AcceptanceStatus, OperationStatus, RunStatus
from asterun.errors import IDEMPOTENCY_CONFLICT, INVALID_REQUEST, REVISION_CONFLICT
from asterun.ids import RunId, TaskId
from asterun.mcp_server import handle_rpc


def prepared(app):
    submitted = app.handle("task.submit", {"workspace": "demo", "text": "original objective"})
    assert submitted.ok
    result = app.handle("workflow.snapshot", {"task_id": submitted.ids["task_id"], "target_paths": ["note.txt"]})
    assert result.ok
    snap = result.data
    return {"task_id": snap["task_id"], "expected_run_id": snap["run_id"],
            "expected_revision": snap["revision"], "expected_input_hash": snap["input_hash"],
            "target_paths": snap["target_paths"], "expected_target_hash": snap["target_hash"]}


def report(root, bound, *, passed=True):
    value = {"task_id": bound["task_id"], "run_id": bound["expected_run_id"],
             "input_hash": bound["expected_input_hash"], "target_hash": bound["expected_target_hash"],
             "checks": [{"name": "isolated suite", "passed": passed, "detail": "fixed test result"}]}
    raw = json.dumps(value).encode()
    (root / "report.json").write_bytes(raw)
    return {"kind": "external_report", "path": "report.json", "sha256": hashlib.sha256(raw).hexdigest()}


@pytest.mark.parametrize("passed", [True, False])
def test_external_result_is_persisted_with_source_and_target(app, workspace_root, passed):
    bound = prepared(app)
    result = app.handle("workflow.evaluate", {**bound, "checks": [report(workspace_root, bound, passed=passed)]})
    assert result.ok
    assert result.data["acceptance"] == ("passed" if passed else "failed")
    assert result.data["checks"][0]["source"] == "external_reported"
    task = app.store.get_task(TaskId(bound["task_id"]))
    assert task.external_evaluation["target_hash"] == bound["expected_target_hash"]
    assert task.quality == {}
    assert task.review_status == "not_configured"


def test_report_does_not_satisfy_required_independent_review(app, workspace_root):
    bound = prepared(app)
    result = app.handle("workflow.evaluate", {**bound, "require_review": True,
                        "checks": [report(workspace_root, bound)]})
    assert result.ok and result.data["acceptance"] == "pending"
    assert result.data["review_status"] == "unavailable"
    assert result.data["task"]["paused"] is True


@pytest.mark.parametrize("kind", ["file_exists", "file_contains"])
def test_bound_file_check_cannot_extend_target_scope(app, workspace_root, kind):
    bound = prepared(app)
    (workspace_root / "outside-scope.txt").write_text("PASS")
    check = {"kind": kind, "path": "outside-scope.txt"}
    if kind == "file_contains":
        check["text"] = "PASS"
    result = app.handle("workflow.evaluate", {**bound, "checks": [check]})
    assert not result.ok and result.error["code"] == INVALID_REQUEST
    task = app.store.get_task(TaskId(bound["task_id"]))
    assert task.acceptance == AcceptanceStatus.PENDING and not task.external_evaluation


@pytest.mark.parametrize("override", [{}, {"require_review": False}])
def test_required_review_cannot_be_lowered_by_evaluation_or_repair(app, workspace_root, override):
    bound = prepared(app)
    check = report(workspace_root, bound)
    first = app.handle("workflow.evaluate", {**bound, "require_review": True, "checks": [check]})
    assert first.ok and first.data["acceptance"] == "pending"
    repeated = app.handle("workflow.evaluate", {**bound, **override, "checks": [check]})
    assert repeated.ok and repeated.data["acceptance"] == "pending"
    assert repeated.data["review_status"] != "not_configured"
    assert app.store.get_task(TaskId(bound["task_id"])).external_require_review is True

    assert app.handle("task.handoff", {"task_id": bound["task_id"], "action": "resume"}).ok
    repaired = app.handle("workflow.repair", {**bound, "instructions": "fix the bounded result",
                                              "idempotency_key": "review-preserving-repair"})
    assert repaired.ok
    assert app.store.get_task(TaskId(bound["task_id"])).external_require_review is True
    snap = app.handle("workflow.snapshot", {"task_id": bound["task_id"], "target_paths": ["note.txt"]}).data
    rebound = {**bound, "expected_run_id": snap["run_id"], "expected_target_hash": snap["target_hash"]}
    checked = app.handle("workflow.evaluate", {**rebound, **override,
                                              "checks": [report(workspace_root, rebound)]})
    assert checked.ok and checked.data["acceptance"] == "pending"
    assert checked.data["review_status"] != "not_configured"


def test_required_review_survives_sqlite_reopen(config_path, isolated_env, workspace_root):
    state = isolated_env / "review-state"
    first = Application.from_paths(config_path, state)
    try:
        bound = prepared(first)
        check = report(workspace_root, bound)
        result = first.handle("workflow.evaluate", {**bound, "require_review": True, "checks": [check]})
        assert result.ok and result.data["acceptance"] == "pending"
    finally:
        first.close()
    reopened = Application.from_paths(config_path, state)
    try:
        assert reopened.store.get_task(TaskId(bound["task_id"])).external_require_review is True
        checked = reopened.handle("workflow.evaluate", {**bound, "require_review": False, "checks": [check]})
        assert checked.ok and checked.data["acceptance"] == "pending"
    finally:
        reopened.close()


@pytest.mark.parametrize("field", ["expected_run_id", "expected_revision", "expected_input_hash", "expected_target_hash"])
def test_wrong_binding_cannot_change_acceptance(app, workspace_root, field):
    bound = prepared(app)
    check = report(workspace_root, bound)
    bad = {**bound, field: 2 if field == "expected_revision" else "stale"}
    result = app.handle("workflow.evaluate", {**bad, "checks": [check]})
    assert not result.ok
    assert app.store.get_task(TaskId(bound["task_id"])).acceptance == AcceptanceStatus.PENDING


def test_external_report_without_target_binding_is_rejected(app, workspace_root):
    bound = prepared(app)
    result = app.handle("workflow.evaluate", {"task_id": bound["task_id"], "checks": [report(workspace_root, bound)]})
    assert not result.ok and result.error["code"] == INVALID_REQUEST


@pytest.mark.parametrize("changed", ["note.txt", "report.json"])
def test_readback_invalidates_changed_source_or_report(app, workspace_root, changed):
    bound = prepared(app)
    assert app.handle("workflow.evaluate", {**bound, "checks": [report(workspace_root, bound)]}).ok
    (workspace_root / changed).write_text("changed")
    result = app.handle("task.get", {"task_id": bound["task_id"], "compact": True})
    assert result.data["task"]["acceptance"] == "pending"
    assert result.data["task"]["evidence_stale"] is True
    events = app.handle("task.events", {"task_id": bound["task_id"]}).data["events"]
    assert len([e for e in events if e["type"] == "evidence_invalidated"]) == 1
    app.handle("task.get", {"task_id": bound["task_id"]})
    assert len([e for e in app.handle("task.events", {"task_id": bound["task_id"]}).data["events"]
                if e["type"] == "evidence_invalidated"]) == 1


def test_sqlite_reopen_keeps_evaluation_and_invalidates_changed_target(config_path, isolated_env, workspace_root):
    state = isolated_env / "state"
    with_app = Application.from_paths(config_path, state)
    try:
        bound = prepared(with_app)
        result = with_app.handle("workflow.evaluate", {**bound, "checks": [report(workspace_root, bound)]})
        assert result.ok and result.data["acceptance"] == "passed"
    finally:
        with_app.close()
    reopened = Application.from_paths(config_path, state)
    try:
        assert reopened.handle("task.get", {"task_id": bound["task_id"]}).data["task"]["acceptance"] == "passed"
        (workspace_root / "note.txt").write_text("new version")
        assert reopened.handle("task.get", {"task_id": bound["task_id"]}).data["task"]["acceptance"] == "pending"
    finally:
        reopened.close()


def test_repair_keeps_task_and_prompt_goal_and_replay_is_readback(app, workspace_root, monkeypatch):
    bound = prepared(app)
    assert app.handle("workflow.evaluate", {**bound, "checks": [report(workspace_root, bound, passed=False)]}).ok
    request = {**bound, "instructions": "repair only the bounded failure", "idempotency_key": "repair-1"}
    first = app.handle("workflow.repair", request)
    assert first.ok
    assert first.ids["task_id"] == bound["task_id"]
    assert first.ids["run_id"] != bound["expected_run_id"]
    assert first.data["task"]["repair_count"] == 1 and first.data["task"]["run_count"] == 2
    assert first.data["task"]["text"] == "original objective"
    assert first.data["task"]["input_hash"] == bound["expected_input_hash"]
    assert "repair only the bounded failure" in first.data["run"]["prompt"]
    assert first.data["task"]["acceptance"] == "pending"
    assert "external_evaluation" not in first.data["task"]
    backend = app.backends["fake"]
    count = backend.dispatch_count
    monkeypatch.setattr(backend, "can_dispatch", lambda: False)
    (workspace_root / "note.txt").write_text("changed since dispatch")
    replay = app.handle("workflow.repair", request)
    assert replay.ok and replay.data["reused"]
    assert replay.ids["run_id"] == first.ids["run_id"]
    assert backend.dispatch_count == count
    changed = app.handle("workflow.repair", {**request, "instructions": "different repair"})
    assert not changed.ok and changed.error["code"] == IDEMPOTENCY_CONFLICT


def test_repair_same_key_different_budget_conflicts(app):
    bound = prepared(app)
    request = {"task_id": bound["task_id"], "idempotency_key": "same", "max_repairs": 1}
    assert app.handle("workflow.repair", request).ok
    result = app.handle("workflow.repair", {**request, "max_repairs": 0})
    assert not result.ok and result.error["code"] == IDEMPOTENCY_CONFLICT


def test_legacy_repair_record_replays_after_sqlite_reopen(config_path, isolated_env):
    state = isolated_env / "legacy-repair-state"
    original = Application.from_paths(config_path, state)
    try:
        bound = prepared(original)
        request = {"task_id": bound["task_id"], "idempotency_key": "legacy-key", "max_repairs": 1}
        repaired = original.handle("workflow.repair", request)
        assert repaired.ok
        operation = original.store.find_operation_for_run(RunId(repaired.ids["run_id"]))
        # 升级前持久记录只有原任务摘要，没有完整修复请求摘要。
        original.store.save_operation(replace(operation, input_hash=bound["expected_input_hash"]))
    finally:
        original.close()
    reopened = Application.from_paths(config_path, state)
    try:
        replay = reopened.handle("workflow.repair", request)
        assert replay.ok and replay.data["reused"]
        assert replay.ids["run_id"] == repaired.ids["run_id"]
        assert replay.ids["operation_id"] == repaired.ids["operation_id"]
        assert replay.data["task"]["repair_count"] == 1 and replay.data["task"]["run_count"] == 2
        assert reopened.backends["fake"].dispatch_count == 0
    finally:
        reopened.close()


@pytest.mark.parametrize("new_fields", ["instructions", "binding"])
def test_legacy_repair_key_cannot_authorize_new_inputs(app, new_fields):
    bound = prepared(app)
    request = {"task_id": bound["task_id"], "idempotency_key": "legacy-key"}
    repaired = app.handle("workflow.repair", request)
    assert repaired.ok
    operation = app.store.find_operation_for_run(RunId(repaired.ids["run_id"]))
    app.store.save_operation(replace(operation, input_hash=bound["expected_input_hash"]))
    changed = {**request, **({"instructions": "new repair"} if new_fields == "instructions" else bound)}
    counts = (len(app.store.operations), len(app.store.runs), app.backends["fake"].dispatch_count)
    rejected = app.handle("workflow.repair", changed)
    assert not rejected.ok and rejected.error["code"] == IDEMPOTENCY_CONFLICT
    assert (len(app.store.operations), len(app.store.runs), app.backends["fake"].dispatch_count) == counts


def test_oversized_repair_instructions_rejected_before_intent(app):
    bound = prepared(app)
    counts = (len(app.store.operations), len(app.store.runs), app.backends["fake"].dispatch_count)
    rejected = app.handle("workflow.repair", {**bound, "instructions": "x" * 32001,
                                              "idempotency_key": "oversized-repair"})
    assert not rejected.ok and rejected.error["code"] == INVALID_REQUEST
    assert (len(app.store.operations), len(app.store.runs), app.backends["fake"].dispatch_count) == counts


@pytest.mark.parametrize("bad_hash", ["a" * 63, "a" * 65, "A" * 64, "g" * 64])
def test_bad_report_hash_rejected_before_workflow_effects(app, workspace_root, bad_hash):
    bound = prepared(app)
    check = {**report(workspace_root, bound), "sha256": bad_hash}
    counts = (len(app.store.operations), len(app.store.runs), app.backends["fake"].dispatch_count)
    rejected = app.handle("workflow.evaluate", {**bound, "checks": [check]})
    assert not rejected.ok and rejected.error["code"] == INVALID_REQUEST
    assert (len(app.store.operations), len(app.store.runs), app.backends["fake"].dispatch_count) == counts
    assert app.store.get_task(TaskId(bound["task_id"])).acceptance == AcceptanceStatus.PENDING


def test_queued_repair_rechecks_target_before_dispatch(app, workspace_root):
    bound = prepared(app)
    blocker = app.handle("task.submit", {"workspace": "demo", "script": "cancel_and_stop"})
    assert blocker.ok
    request = {**bound, "instructions": "fix", "idempotency_key": "queued-repair"}
    queued = app.handle("workflow.repair", request)
    assert queued.ok and queued.data["queued"] is True
    count = app.backends["fake"].dispatch_count
    (workspace_root / "note.txt").write_text("changed while queued")
    app.handle("task.cancel", {"task_id": blocker.ids["task_id"]})
    result = app.handle("task.get", {"task_id": bound["task_id"]})
    assert result.data["run"]["status"] == "failed"
    assert result.data["run"]["error_code"] == REVISION_CONFLICT
    assert app.backends["fake"].dispatch_count == count


def test_direct_repair_target_drift_after_commit_finishes_unsent_operation(app, workspace_root, monkeypatch):
    bound = prepared(app)
    original = app.store.commit_intent

    def change_after_commit(operation, *args, **kwargs):
        result = original(operation, *args, **kwargs)
        if operation.action == "workflow.repair":
            (workspace_root / "note.txt").write_text("changed after intent commit")
        return result

    monkeypatch.setattr(app.store, "commit_intent", change_after_commit)
    count = app.backends["fake"].dispatch_count
    request = {**bound, "instructions": "fix", "idempotency_key": "direct-drift"}
    result = app.handle("workflow.repair", request)
    assert not result.ok and result.error["code"] == REVISION_CONFLICT
    task = app.store.get_task(TaskId(bound["task_id"]))
    run = app.store.get_run(task.current_run_id)
    operation = app.store.find_operation_for_run(run.id)
    assert run.id.value != bound["expected_run_id"]
    assert run.status == RunStatus.FAILED and run.terminated is True
    assert run.error_code == REVISION_CONFLICT
    assert operation.status == OperationStatus.FAILED
    assert task.repair_count == 1 and task.run_count == 2
    assert task.acceptance == AcceptanceStatus.PENDING
    assert app.scheduler.queue == []
    app.poll()
    assert app.backends["fake"].dispatch_count == count
    replay = app.handle("workflow.repair", request)
    assert replay.ok and replay.data["reused"]
    assert replay.ids["run_id"] == run.id.value
    assert replay.data["operation"]["status"] == "failed"
    assert app.backends["fake"].dispatch_count == count


def test_historical_evaluation_retains_report_reference_after_repair_and_new_evaluation(app, workspace_root):
    bound = prepared(app)
    first_check = report(workspace_root, bound, passed=False)
    first = app.handle("workflow.evaluate", {**bound, "checks": [first_check]})
    assert first.ok and first.data["acceptance"] == "failed"
    repaired = app.handle("workflow.repair", {**bound, "instructions": "fix the failed checks",
                                              "idempotency_key": "history-repair"})
    assert repaired.ok and "external_evaluation" not in repaired.data["task"]
    snap = app.handle("workflow.snapshot", {"task_id": bound["task_id"], "target_paths": ["note.txt"]}).data
    rebound = {**bound, "expected_run_id": snap["run_id"], "expected_target_hash": snap["target_hash"]}
    second_check = report(workspace_root, rebound)
    second = app.handle("workflow.evaluate", {**rebound, "checks": [second_check]})
    assert second.ok and second.data["acceptance"] == "passed"
    original_events = [event for event in app.store.list_events(RunId(bound["expected_run_id"]))
                       if str(event.type) == "acceptance_evaluated"]
    repair_events = [event for event in app.store.list_events(RunId(rebound["expected_run_id"]))
                     if str(event.type) == "acceptance_evaluated"]
    assert len(original_events) == len(repair_events) == 1
    for event, binding, check, passed in ((original_events[0], bound, first_check, False),
                                        (repair_events[0], rebound, second_check, True)):
        evidence = event.payload["external_evaluation"]
        assert evidence["expected_run_id"] == binding["expected_run_id"]
        assert evidence["expected_input_hash"] == binding["expected_input_hash"]
        assert evidence["target_hash"] == binding["expected_target_hash"]
        assert evidence["reports"] == [{"path": check["path"], "sha256": check["sha256"]}]
        assert event.payload["checks"][0]["passed"] is passed
        assert event.payload["checks"][0]["source"] == "external_reported"
    assert first_check["sha256"] != second_check["sha256"]


def test_invalidated_during_evaluation_does_not_mutate_memory_store(app, workspace_root, monkeypatch):
    bound = prepared(app)
    check = report(workspace_root, bound)
    monkeypatch.setattr("asterun.external_evaluation.validate_external_evaluation", lambda *a, **k: False)
    result = app.handle("workflow.evaluate", {**bound, "checks": [check]})
    assert not result.ok
    task = app.store.get_task(TaskId(bound["task_id"]))
    assert task.acceptance == "pending" and not task.external_evaluation


@pytest.mark.parametrize("status", [RunStatus.RUNNING, RunStatus.PENDING_RECONCILE])
def test_snapshot_cannot_bind_unfinished_source(app, status):
    bound = prepared(app)
    run = app.store.get_run(RunId(bound["expected_run_id"]))
    app.store.save_run(replace(run, status=status))
    result = app.handle("workflow.snapshot", {"task_id": bound["task_id"], "target_paths": ["note.txt"]})
    assert not result.ok


def test_cli_and_mcp_expose_snapshot_and_bound_repair(app, workspace_root, tmp_path):
    bound = prepared(app)
    request = {**bound, "instructions": "fix exactly", "idempotency_key": "entry-repair"}
    path = tmp_path / "repair.json"
    path.write_text(json.dumps(request))
    args = build_parser().parse_args(["workflow-repair", bound["task_id"], "--request", str(path)])
    assert _load_request(args) == request
    result = handle_rpc(app, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "workflow_snapshot", "arguments": {"task_id": bound["task_id"], "target_paths": ["note.txt"]}}})
    envelope = json.loads(result["result"]["content"][0]["text"])
    assert envelope["ok"] and envelope["data"]["run_id"] == bound["expected_run_id"]
