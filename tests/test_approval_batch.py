from __future__ import annotations

import hashlib

import pytest

from asterun.application import Application
from asterun.backends.codex_runtime import request_hash
from asterun.contracts import ApprovalState, Run
from asterun.errors import AsterunError, REMOTE_STATE_UNKNOWN
from asterun.ids import ApprovalId, RunId, TaskId
from asterun.policy import DenyAll, Grant
from asterun.requests import validate_request


def pending(app):
    result = app.handle("task.submit", {"workspace": "demo", "script": "wait_input", "text": "review"})
    assert result.ok, result.error
    data = app.handle("task.get", {"task_id": result.ids["task_id"]}).data
    approval = data["approval"]
    assert approval is not None
    return {"approval_id": approval["id"], "expected_task_id": result.ids["task_id"],
            "expected_run_id": result.ids["run_id"], "expected_target_hash": approval["target_hash"]}


def test_batch_requires_complete_snapshot_and_limits():
    with pytest.raises(AsterunError): validate_request("approval.batch", {"items": []})
    with pytest.raises(AsterunError): validate_request("approval.batch", {"items": [{"approval_id": "a", "decision": "approve"}]})
    item = {"approval_id": "a", "expected_task_id": "t", "expected_run_id": "r", "expected_target_hash": "h", "decision": "approve"}
    with pytest.raises(AsterunError): validate_request("approval.batch", {"items": [item] * 101})


def test_batch_results_are_per_item_and_repeat_never_forwards(app):
    item = pending(app)
    result = app.handle("approval.batch", {"items": [{**item, "decision": "approve"}, {**item, "decision": "approve"}]})
    assert result.ok and not result.data["all_succeeded"]
    assert [r["ok"] for r in result.data["results"]] == [True, False]
    calls = [c for c in app.backends["fake"].calls if c.method == "respond_approval"]
    assert len(calls) == 1


@pytest.mark.parametrize("field", ["expected_task_id", "expected_run_id", "expected_target_hash"])
def test_snapshot_conflict_keeps_pending(app, field):
    item = pending(app)
    result = app.handle("approval.batch", {"items": [{**item, field: "wrong", "decision": "approve"}]})
    assert result.ok and not result.data["results"][0]["ok"]
    assert app.store.get_approval(ApprovalId(item["approval_id"])).state == ApprovalState.PENDING


@pytest.mark.parametrize("restriction", ["grant", "policy", "expired", "review", "paused", "cancelled", "revision"])
def test_existing_authority_and_current_run_checks_still_apply(app, restriction):
    item = pending(app)
    if restriction == "grant": app.grant = Grant(expires_at="2000-01-01T00:00:00Z")
    if restriction == "policy": app.policy = DenyAll()
    if restriction == "expired": app.store.get_approval(ApprovalId(item["approval_id"])).expires_at = "2000-01-01T00:00:00Z"
    if restriction == "review": app.store.get_run(RunId(item["expected_run_id"])).role = "review"
    if restriction == "paused": app.store.get_task(TaskId(item["expected_task_id"])).paused = True
    if restriction == "cancelled": app.store.get_run(RunId(item["expected_run_id"])).cancel_requested = True
    if restriction == "revision": app.config.revision += 1
    result = app.handle("approval.batch", {"items": [{**item, "decision": "approve"}]})
    assert result.ok and not result.data["results"][0]["ok"]
    assert not any(c.method == "respond_approval" for c in app.backends["fake"].calls)


def test_assess_and_policy_apply_default_to_manual_without_dispatch(app):
    item = pending(app)
    assessed = app.handle("approval.assess", item)
    assert assessed.ok and assessed.data["assessment"]["decision"] == "manual"
    applied = app.handle("approval.apply-policy", item)
    assert applied.ok and applied.data["manual_required"] and not applied.data["forwarded"]
    assert app.store.get_approval(ApprovalId(item["approval_id"])).state == ApprovalState.PENDING
    assert not any(c.method == "respond_approval" for c in app.backends["fake"].calls)


def test_disconnect_persists_intent_and_cannot_create_new_approval_for_same_request(app, monkeypatch):
    item = pending(app)
    calls = []
    def lost(*args, **kwargs):
        calls.append(True)
        raise RuntimeError("sensitive backend detail")
    monkeypatch.setattr(app.backends["fake"], "respond_approval", lost)
    first = app.handle("approval.batch", {"items": [{**item, "decision": "approve"}]})
    assert first.data["results"][0]["error"]["code"] == REMOTE_STATE_UNKNOWN
    assert "sensitive" not in str(first.to_dict())
    approval = app.store.get_approval(ApprovalId(item["approval_id"]))
    assert approval.state == ApprovalState.FORWARDING and approval.response_status == "send_result_unknown"
    run = app.store.get_run(RunId(item["expected_run_id"]))
    restored = Run.from_dict(run.to_dict())
    assert restored.approval_attempts == [item["expected_target_hash"]]
    app._record_result(run.id, {"status": "waiting_input", "needs_approval": True})
    assert app.store.find_pending_approval(run.id) is None
    repeated = app.handle("approval.respond", {**item, "decision": "approve"})
    assert not repeated.ok and len(calls) == 1


def test_explicit_policy_apply_audits_binding_and_unconfirmed_send(app, workspace_root, tmp_path, monkeypatch):
    item = pending(app)
    approval = app.store.get_approval(ApprovalId(item["approval_id"]))
    task = app.store.get_task(TaskId(item["expected_task_id"]))
    run = app.store.get_run(RunId(item["expected_run_id"]))
    binary = tmp_path / "cat"
    binary.write_text("trusted fake executable; never executed")
    argv = [str(binary), "note.txt"]
    approval.native_request = {"id": "native-request", "method": "item/commandExecution/requestApproval",
                               "params": {"command": argv, "cwd": str(workspace_root)}}
    approval.target_hash = request_hash(task.input_hash, run.id.value, approval.native_request)
    item["expected_target_hash"] = approval.target_hash
    app.config.approval_policy = {"mode": "bounded", "rules": [{
        "id": "review-note", "workspace": "demo", "backend": "fake", "cwd": str(workspace_root),
        "input_hash": task.input_hash, "argv": argv, "executable_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "expires_at": "2099-01-01T00:00:00Z"}]}
    calls = []
    def respond(*args, **kwargs):
        calls.append(kwargs)
        return {"status": "running", "terminated": False}
    monkeypatch.setattr(app.backends["fake"], "respond_approval", respond)
    app.poll()
    assert not calls  # 仅配置策略不自动审批。
    assessment = app.handle("approval.assess", item)
    assert assessment.ok and assessment.data["assessment"]["decision"] == "approve" and not calls
    applied = app.handle("approval.apply-policy", item)
    assert applied.ok and applied.data["forwarded"] and len(calls) == 1
    response = applied.data["approval"]
    assert response["decision_source"] == "policy" and response["response_status"] == "sent_unconfirmed"
    assert not response["native_confirmed"]
    events = app.store.list_events(run.id, page_size=500)
    assert any(e.type == "approval_forwarding" and e.payload["assessment"]["policy_id"] == "review-note" for e in events)
    assert not app.handle("approval.apply-policy", item).ok and len(calls) == 1


def test_forwarding_tombstone_survives_sqlite_core_restart(config_path, tmp_path, monkeypatch):
    state = tmp_path / "state"
    app = Application.from_paths(config_path, state)
    item = pending(app)
    def lost(*args, **kwargs):
        raise RuntimeError("lost")
    monkeypatch.setattr(app.backends["fake"], "respond_approval", lost)
    assert not app.handle("approval.respond", {**item, "decision": "approve"}).ok
    app.close()
    app = Application.from_paths(config_path, state)
    try:
        app._record_result(RunId(item["expected_run_id"]), {"status": "waiting_input", "needs_approval": True})
        assert app.store.find_pending_approval(RunId(item["expected_run_id"])) is None
        assert not app.handle("approval.respond", {**item, "decision": "approve"}).ok
        assert not any(c.method == "respond_approval" for c in app.backends["fake"].calls)
        assert app.store.get_approval(ApprovalId(item["approval_id"])).state == ApprovalState.FORWARDING
    finally:
        app.close()


def test_config_cas_explains_loaded_versus_applied_revision(app):
    app.config.revision = 2
    validated = app.handle("config.validate", {}).data
    assert validated["loaded_config_revision"] == 2 and validated["applied_config_revision"] == 1
    wrong = app.handle("config.apply", {"expected_revision": 2})
    assert not wrong.ok and wrong.error["code"] == "REVISION_CONFLICT"
    assert wrong.error["details"]["loaded_config_revision"] == 2
    assert wrong.error["details"]["applied"] == 1
    assert "--expected-applied-revision" in wrong.error["next_action"]
    applied = app.handle("config.apply", {"expected_revision": 1})
    assert applied.ok and applied.data["file_revision"] == applied.data["loaded_config_revision"] == 2
