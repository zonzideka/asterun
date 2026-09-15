"""人工交接返回前复核外部验收证据；不依赖随后 task.get 才发现漂移。"""

import hashlib
import json

import pytest

from asterun.contracts import AcceptanceStatus
from asterun.ids import TaskId


def evaluate_report(app, workspace_root, *, require_review):
    submitted = app.handle("task.submit", {"workspace": "demo", "text": "handoff fixture"})
    assert submitted.ok, submitted.error
    task_id = submitted.ids["task_id"]
    snapshot = app.handle("workflow.snapshot", {"task_id": task_id, "target_paths": ["note.txt"]})
    assert snapshot.ok, snapshot.error
    target = snapshot.data
    report = {"task_id": task_id, "run_id": target["run_id"], "input_hash": target["input_hash"],
              "target_hash": target["target_hash"],
              "checks": [{"name": "fixed handoff fixture", "passed": True, "detail": "original version"}]}
    raw = json.dumps(report).encode()
    (workspace_root / "report.json").write_bytes(raw)
    evaluated = app.handle("workflow.evaluate", {
        "task_id": task_id, "expected_run_id": target["run_id"], "expected_revision": target["revision"],
        "expected_input_hash": target["input_hash"], "expected_target_hash": target["target_hash"],
        "target_paths": target["target_paths"], "require_review": require_review,
        "checks": [{"kind": "external_report", "path": "report.json", "sha256": hashlib.sha256(raw).hexdigest()}],
    })
    assert evaluated.ok, evaluated.error
    assert evaluated.data["acceptance"] == ("pending" if require_review else "passed")
    return task_id


@pytest.mark.parametrize("action", ["pause", "resume"])
@pytest.mark.parametrize("changed", ["note.txt", "report.json"])
@pytest.mark.parametrize("require_review", [False, True])
def test_handoff_response_invalidates_drift_and_preserves_review_requirement(
        app, workspace_root, action, changed, require_review):
    task_id = evaluate_report(app, workspace_root, require_review=require_review)
    before = app.store.get_task(TaskId(task_id))
    original_review_status = before.review_status
    original_run_id = before.current_run_id
    if action == "resume":
        paused = app.handle("task.handoff", {"task_id": task_id, "action": "pause"})
        assert paused.ok, paused.error
    (workspace_root / changed).write_text("changed while under human ownership")

    result = app.handle("task.handoff", {"task_id": task_id, "action": action})

    assert result.ok, result.error
    # Check the handoff response itself before any task.get can invalidate it.
    returned = result.data["task"]
    assert returned["acceptance"] == "pending"
    assert returned["evidence_stale"] is True and result.data["evidence_stale"] is True
    assert returned["evidence_input_hash"] == ""
    assert returned.get("external_require_review", False) is require_review
    assert returned["review_status"] == original_review_status
    assert returned["external_evaluation"]["source"] == "external_reported"
    assert returned["paused"] is (action == "pause")
    assert result.data["run"]["id"] == original_run_id.value
    saved = app.store.get_task(TaskId(task_id))
    assert saved.acceptance == AcceptanceStatus.PENDING and saved.evidence_stale is True
    assert saved.external_require_review is require_review
    assert saved.repair_count == 0 and saved.run_count == 1


@pytest.mark.parametrize("action", ["pause", "resume"])
def test_handoff_without_external_binding_keeps_existing_behavior(app, workspace_root, action):
    submitted = app.handle("task.submit", {"workspace": "demo", "text": "unbound handoff fixture"})
    assert submitted.ok, submitted.error
    task_id = submitted.ids["task_id"]
    evaluated = app.handle("workflow.evaluate", {"task_id": task_id,
                            "checks": [{"kind": "file_exists", "path": "note.txt"}]})
    assert evaluated.ok and evaluated.data["acceptance"] == "passed"
    if action == "resume":
        assert app.handle("task.handoff", {"task_id": task_id, "action": "pause"}).ok
    (workspace_root / "note.txt").unlink()
    result = app.handle("task.handoff", {"task_id": task_id, "action": action})
    assert result.ok, result.error
    returned = result.data["task"]
    assert returned["acceptance"] == "passed" and returned["evidence_stale"] is False
    assert returned["paused"] is (action == "pause")
    assert "external_evaluation" not in returned
