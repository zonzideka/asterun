from copy import deepcopy
import hashlib
import json

import pytest

from asterun.application import Application
from asterun.config import load_config
from asterun.ids import RunId, TaskId
from asterun.sqlite_store import SqliteStore
from tests.conftest import write_config
from tests.test_quality_runtime import RepairingFake, ReviewingFake, settle


def quality_app(config_path, workspace_root, *, store=None, reviewer_enabled=True):
    path = write_config(config_path, workspace_root, extra_backends={
        "reviewer": {"kind": "fake", "enabled": reviewer_enabled}}, extra={
        "workflow": {"require_review": False, "review_backend": "reviewer"}})
    config = load_config(path)
    reviewer = ReviewingFake(config.backends["reviewer"])
    implementer = RepairingFake(config.backends["fake"])
    app = Application(config, store=store,
                      backends={"fake": implementer, "reviewer": reviewer})
    return app, reviewer


def externally_evaluated(app, root, *, require_review=False):
    submit = app.handle("task.submit", {"workspace": "demo", "text": "修正 answer"})
    assert submit.ok, submit.error
    task_id = submit.ids["task_id"]
    snapshot = app.handle("workflow.snapshot", {
        "task_id": task_id, "target_paths": ["note.txt"]})
    assert snapshot.ok, snapshot.error
    bound = snapshot.data
    raw = json.dumps({"task_id": task_id, "run_id": bound["run_id"],
                      "input_hash": bound["input_hash"], "target_hash": bound["target_hash"],
                      "checks": [{"name": "external check", "passed": True,
                                  "detail": "fixed external result"}]}).encode()
    (root / "report.json").write_bytes(raw)
    checked = app.handle("workflow.evaluate", {
        "task_id": task_id, "expected_run_id": bound["run_id"],
        "expected_revision": bound["revision"], "expected_input_hash": bound["input_hash"],
        "target_paths": bound["target_paths"], "expected_target_hash": bound["target_hash"],
        "require_review": require_review,
        "checks": [{"kind": "external_report", "path": "report.json",
                    "sha256": hashlib.sha256(raw).hexdigest()}]})
    assert checked.ok, checked.error
    assert checked.data["acceptance"] == ("pending" if require_review else "passed")
    if require_review:
        assert app.handle("task.handoff", {"task_id": task_id, "action": "resume"}).ok
    request = {"task_id": task_id, "idempotency_key": "internal-quality",
               "target_paths": ["note.txt"],
               "checks": [{"kind": "file_exists", "path": "note.txt"}]}
    return bound, request


def external_history(app, run_id):
    return [deepcopy(event.payload) for event in app.store.list_events(RunId(run_id))
            if event.payload.get("source") == "external_reported"]


@pytest.mark.parametrize("require_review", [False, True])
@pytest.mark.parametrize("repair_before_review", [False, True])
def test_external_acceptance_hands_over_to_review_and_repair(
        config_path, workspace_root, require_review, repair_before_review):
    app, reviewer = quality_app(config_path, workspace_root)
    try:
        bound, request = externally_evaluated(app, workspace_root, require_review=require_review)
        history = external_history(app, bound["run_id"])
        assert len(history) == 1
        if repair_before_review:
            request["checks"] = [{"kind": "file_contains", "path": "note.txt", "text": "answer = 42"}]
        started = app.handle("workflow.start", request)
        assert started.ok, started.error
        task = started.data["task"]
        assert task["evidence_stale"] is False
        assert task["acceptance"] == "pending"
        assert task["evidence_input_hash"] == ""
        assert "external_evaluation" not in task
        assert app.store.get_task(TaskId(bound["task_id"])).external_require_review is require_review

        completed = settle(app, bound["task_id"])["task"]
        assert completed["acceptance"] == "passed" and completed["review_status"] == "passed"
        assert completed["quality"]["phase"] == "completed"
        assert completed["repair_count"] == int(repair_before_review)
        assert completed["run_count"] == 2 + int(repair_before_review)
        assert len(reviewer.contexts) == 1
        assert len(completed["quality"]["reviews"]) == 1
        assert external_history(app, bound["run_id"]) == history
        assert app.store.get_task(TaskId(bound["task_id"])).external_require_review is require_review
        replay = app.handle("workflow.start", request)
        assert replay.ok and replay.data["task"]["acceptance"] == "passed"
        assert replay.data["task"]["run_count"] == completed["run_count"]
        assert len(reviewer.contexts) == 1
    finally:
        app.close()


def test_required_external_review_cannot_be_bypassed_when_reviewer_unavailable(config_path, workspace_root):
    app, reviewer = quality_app(config_path, workspace_root, reviewer_enabled=False)
    try:
        bound, request = externally_evaluated(app, workspace_root, require_review=True)
        history = external_history(app, bound["run_id"])
        result = app.handle("workflow.start", request)
        assert result.ok, result.error
        task = result.data["task"]
        assert task["acceptance"] == "pending" and task["paused"] is True
        assert task["review_status"] == "unavailable"
        assert task["run_count"] == 1 and not task["quality"]["reviews"]
        assert task["external_require_review"] is True and not reviewer.contexts
        assert external_history(app, bound["run_id"]) == history
    finally:
        app.close()


@pytest.mark.parametrize("changed", [None, "report.json", "note.txt"])
def test_internal_quality_owns_fresh_evidence_after_sqlite_reopen(
        config_path, workspace_root, isolated_env, changed):
    database = isolated_env / "quality.sqlite"
    app, reviewer = quality_app(config_path, workspace_root, store=SqliteStore(database))
    try:
        bound, request = externally_evaluated(app, workspace_root, require_review=True)
        history = external_history(app, bound["run_id"])
        started = app.handle("workflow.start", request)
        assert started.ok, started.error
        assert started.data["task"]["quality"]["phase"] == "reviewing"
        assert len(reviewer.contexts) == 1
    finally:
        app.close()
    if changed:
        (workspace_root / changed).write_text("changed after internal review started")
    app, reviewer = quality_app(config_path, workspace_root, store=SqliteStore(database))
    try:
        task = settle(app, bound["task_id"])["task"]
        assert task["external_require_review"] is True
        assert "external_evaluation" not in task
        assert task["run_count"] == 2 and not reviewer.contexts
        assert external_history(app, bound["run_id"]) == history
        if changed == "note.txt":
            assert task["evidence_stale"] is True and task["paused"] is True
            assert task["acceptance"] == "pending" and task["quality"]["phase"] == "stale"
        else:
            assert task["evidence_stale"] is False and task["paused"] is False
            assert task["acceptance"] == "passed" and task["quality"]["phase"] == "completed"
            assert len(task["quality"]["reviews"]) == 1
    finally:
        app.close()


@pytest.mark.parametrize("invalid", [
    {"checks": [{"kind": "run_succeeded"}]},
    {"checks": [{"kind": "file_exists", "path": "outside-scope.txt"}]},
    {"target_paths": ["../outside.txt"],
     "checks": [{"kind": "file_exists", "path": "../outside.txt"}]},
])
def test_rejected_workflow_start_retains_external_acceptance(config_path, workspace_root, invalid):
    app, reviewer = quality_app(config_path, workspace_root)
    try:
        bound, request = externally_evaluated(app, workspace_root)
        original = app.store.get_task(TaskId(bound["task_id"])).to_dict()
        history = external_history(app, bound["run_id"])
        rejected = app.handle("workflow.start", {**request, **invalid})
        assert not rejected.ok
        assert app.store.get_task(TaskId(bound["task_id"])).to_dict() == original
        assert external_history(app, bound["run_id"]) == history
        assert not reviewer.contexts
    finally:
        app.close()
