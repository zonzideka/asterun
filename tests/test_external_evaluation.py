from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from types import SimpleNamespace

import pytest

from asterun.contracts import RunStatus
from asterun.errors import AsterunError
from asterun.external_evaluation import (check_external_report, prepare_snapshot,
                                         validate_external_evaluation)
from asterun.ids import RunId, TaskId


@pytest.fixture
def bound(workspace_root):
    task = SimpleNamespace(id=TaskId("task_external"), current_run_id=RunId("run_external"),
                           config_revision=1, input_hash="input-hash", external_evaluation={})
    run = SimpleNamespace(id=task.current_run_id, task_id=task.id, status=RunStatus.SUCCEEDED)
    prepared = prepare_snapshot(task, run, workspace_root, ["note.txt"])
    report = {"task_id": task.id.value, "run_id": run.id.value, "input_hash": task.input_hash,
              "target_hash": prepared["target_hash"],
              "checks": [{"name": "unit tests", "passed": True, "detail": "3 passed"}]}
    return task, run, prepared, report


def write_report(root, report, *, raw=None):
    raw = json.dumps(report, ensure_ascii=False).encode() if raw is None else raw
    (root / "report.json").write_bytes(raw)
    return {"kind": "external_report", "path": "report.json", "sha256": hashlib.sha256(raw).hexdigest()}


def bind_evidence(task, prepared, spec):
    task.external_evaluation = {"expected_run_id": prepared["run_id"],
        "expected_revision": prepared["revision"], "expected_input_hash": prepared["input_hash"],
        "target_paths": prepared["target_paths"], "target_hash": prepared["target_hash"],
        "reports": [{key: spec[key] for key in ("path", "sha256")}]}


def test_snapshot_contains_manifest_without_source_and_does_not_mutate(bound, workspace_root):
    task, run, prepared, _ = bound
    before = deepcopy(vars(task))
    assert prepared["target"]["files"][0]["path"] == "note.txt"
    assert "content" not in prepared["target"]["files"][0]
    assert prepared["target_hash"] == prepared["target"]["hash"]
    assert prepare_snapshot(task, run, workspace_root, ["note.txt"]) == prepared
    assert vars(task) == before


@pytest.mark.parametrize("field", ["id", "task_id"])
def test_snapshot_rejects_wrong_run(bound, workspace_root, field):
    task, run, _, _ = bound
    setattr(run, field, RunId("run_other") if field == "id" else TaskId("task_other"))
    with pytest.raises(AsterunError):
        prepare_snapshot(task, run, workspace_root, ["note.txt"])


@pytest.mark.parametrize("status", [RunStatus.RUNNING, RunStatus.PENDING_RECONCILE])
def test_snapshot_and_report_reject_unconfirmed_run(bound, workspace_root, status):
    task, run, prepared, report = bound
    spec = write_report(workspace_root, report)
    bind_evidence(task, prepared, spec)
    run.status = status
    with pytest.raises(AsterunError):
        prepare_snapshot(task, run, workspace_root, ["note.txt"])
    with pytest.raises(AsterunError):
        check_external_report(spec, task, run, prepared["target_hash"], workspace_root)
    assert not validate_external_evaluation(task, run, workspace_root)


@pytest.mark.parametrize("passed", [True, False])
def test_report_preserves_external_source_and_check_verdict(bound, workspace_root, passed):
    task, run, prepared, report = bound
    report["checks"][0]["passed"] = passed
    spec = write_report(workspace_root, report)
    result = check_external_report(spec, task, run, prepared["target_hash"], workspace_root)
    assert result["passed"] is passed
    assert result["source"] == "external_reported"
    assert result["report"] == {"path": spec["path"], "sha256": spec["sha256"]}
    assert "review" not in result and "actor" not in result


@pytest.mark.parametrize("field", ["task_id", "run_id", "input_hash", "target_hash"])
def test_report_rejects_any_stale_binding(bound, workspace_root, field):
    task, run, prepared, report = bound
    report[field] = "stale"
    spec = write_report(workspace_root, report)
    with pytest.raises(AsterunError):
        check_external_report(spec, task, run, prepared["target_hash"], workspace_root)


@pytest.mark.parametrize("mutation", [
    lambda x: x.update(passed=True), lambda x: x.update(actor="trusted-reviewer"),
    lambda x: x.update(review={"passed": True}), lambda x: x.update(checks=[]),
    lambda x: x["checks"][0].update(passed=1), lambda x: x["checks"][0].update(passed="true"),
    lambda x: x["checks"][0].update(name=" "), lambda x: x["checks"][0].update(detail=1),
    lambda x: x["checks"][0].update(review=True), lambda x: x["checks"].append(dict(x["checks"][0])),
])
def test_report_rejects_self_reported_review_and_malformed_checks(bound, workspace_root, mutation):
    task, run, prepared, report = bound
    mutation(report)
    spec = write_report(workspace_root, report)
    with pytest.raises(AsterunError):
        check_external_report(spec, task, run, prepared["target_hash"], workspace_root)


@pytest.mark.parametrize("raw", [b'{"task_id":"a","task_id":"b"}', b'[]', b'null', b'\xff',
                                 b'[' * 1500 + b']' * 1500])
def test_report_rejects_invalid_json(bound, workspace_root, raw):
    task, run, prepared, report = bound
    spec = write_report(workspace_root, report, raw=raw)
    with pytest.raises(AsterunError):
        check_external_report(spec, task, run, prepared["target_hash"], workspace_root)


@pytest.mark.parametrize("path", ["../outside.json", "/tmp/report.json", "./report.json", ".git/report.json"])
def test_report_rejects_unsafe_paths(bound, workspace_root, path):
    task, run, prepared, report = bound
    spec = {**write_report(workspace_root, report), "path": path}
    with pytest.raises(AsterunError):
        check_external_report(spec, task, run, prepared["target_hash"], workspace_root)


@pytest.mark.parametrize("kind", ["symlink", "parent_symlink", "oversize", "fifo", "directory", "hash"])
def test_report_rejects_unsafe_file_or_changed_hash(bound, workspace_root, kind):
    task, run, prepared, report = bound
    spec = write_report(workspace_root, report)
    path = workspace_root / "report.json"
    if kind in {"symlink", "fifo", "directory"}:
        path.unlink()
    if kind == "symlink":
        path.symlink_to(workspace_root / "note.txt")
    elif kind == "parent_symlink":
        (workspace_root / "link").symlink_to(workspace_root, target_is_directory=True)
        spec["path"] = "link/report.json"
    elif kind == "oversize":
        path.write_bytes(b"x" * (256 * 1024 + 1))
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    else:
        spec["sha256"] = "0" * 64
    with pytest.raises(AsterunError):
        check_external_report(spec, task, run, prepared["target_hash"], workspace_root)


@pytest.mark.parametrize("change", ["target", "report", "run", "input", "revision", "record"])
def test_validation_detects_drift_without_mutating_task(bound, workspace_root, change):
    task, run, prepared, report = bound
    spec = write_report(workspace_root, report)
    bind_evidence(task, prepared, spec)
    assert validate_external_evaluation(task, run, workspace_root)
    if change == "target":
        (workspace_root / "note.txt").write_text("changed")
    elif change == "report":
        (workspace_root / "report.json").write_text("changed")
    elif change == "run":
        task.current_run_id = RunId("run_other")
    elif change == "input":
        task.input_hash = "changed"
    elif change == "revision":
        task.config_revision += 1
    else:
        task.external_evaluation.pop("reports")
    before = deepcopy(vars(task))
    assert not validate_external_evaluation(task, run, workspace_root)
    assert vars(task) == before


def test_validation_rechecks_target_after_report_read(bound, workspace_root, monkeypatch):
    import asterun.external_evaluation as module
    task, run, prepared, report = bound
    spec = write_report(workspace_root, report)
    bind_evidence(task, prepared, spec)
    original = module.check_external_report

    def mutate(*args, **kwargs):
        result = original(*args, **kwargs)
        (workspace_root / "note.txt").write_text("changed during report validation")
        return result

    monkeypatch.setattr(module, "check_external_report", mutate)
    assert not validate_external_evaluation(task, run, workspace_root)


def test_snapshot_rejects_state_targets(bound, workspace_root):
    task, run, _, _ = bound
    with pytest.raises(AsterunError):
        prepare_snapshot(task, run, workspace_root, ["note.txt"], state_dir=workspace_root)
