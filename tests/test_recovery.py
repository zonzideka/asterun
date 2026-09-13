from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from asterun.application import Application
from asterun.contracts import AcceptanceStatus, ApprovalState, OperationStatus, RunStatus
from asterun.errors import CAPABILITY_UNSUPPORTED, INVALID_REQUEST, REMOTE_STATE_UNKNOWN
from asterun.ids import ApprovalId, TaskId
from asterun.config import WorkspaceBinding


def _independent_workspace(app):
    root = app.config.get_workspace("demo").root.parent / "other-workspace"
    root.mkdir()
    app.config.workspaces["other"] = WorkspaceBinding("other", root)
    app.policy.workspace_aliases.add("other")


def _submit(app: Application, script: str, **extra: object):
    payload = {"workspace": "demo", "script": script, "text": extra.pop("text", script)}
    payload.update(extra)
    return app.handle("task.submit", payload)


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "asterun", *args],
        check=False,
        capture_output=True,
        text=True,
        env={
            "HOME": str(Path.cwd() / "unused-home"),
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        },
    )


def test_lost_reply_does_not_redispatch(app: Application) -> None:
    fake = app.backends["fake"]
    submitted = _submit(app, "lost_reply")
    assert submitted.ok
    assert submitted.data["run"]["status"] == RunStatus.PENDING_RECONCILE
    assert submitted.data["run"]["error_code"] == REMOTE_STATE_UNKNOWN
    assert submitted.data["operation"]["status"] == OperationStatus.UNKNOWN
    first = fake.dispatch_count
    reconciled = app.handle("task.reconcile", {"task_id": submitted.ids["task_id"]})
    assert reconciled.ok
    assert reconciled.data["re_dispatched"] is False
    assert fake.dispatch_count == first
    row = reconciled.data["unknown_states"][0]
    assert row["locatable"] is True
    assert row["re_dispatched"] is False
    assert row["run_status"] == RunStatus.PENDING_RECONCILE
    again = app.handle("task.reconcile", {"task_id": submitted.ids["task_id"]})
    assert again.data["re_dispatched"] is False
    assert fake.dispatch_count == first


def test_timeout_and_running_reconcile_mark_unknown(app: Application) -> None:
    app.config.scheduler.per_backend_concurrency = 2
    _independent_workspace(app)
    timed = _submit(app, "timeout")
    assert timed.data["run"]["status"] == RunStatus.PENDING_RECONCILE
    running = _submit(app, "cancel_accept_only", workspace="other")
    fake = app.backends["fake"]
    before = fake.dispatch_count
    scanned = app.handle("task.reconcile", {})
    assert scanned.ok
    assert scanned.data["re_dispatched"] is False
    assert fake.dispatch_count == before
    by_id = {item["task_id"]: item for item in scanned.data["reports"]}
    running_row = by_id[running.ids["task_id"]]
    assert running_row["unknown"] is True
    assert running_row["run_status"] == RunStatus.PENDING_RECONCILE
    assert running_row["operation_status"] == OperationStatus.UNKNOWN
    assert running_row["error_code"] == REMOTE_STATE_UNKNOWN
    fetched = app.handle("task.get", {"task_id": running.ids["task_id"]})
    assert fetched.data["run"]["status"] == RunStatus.PENDING_RECONCILE
    assert timed.ids["task_id"] in by_id


def test_cancel_race_keeps_succeeded(app: Application) -> None:
    submitted = _submit(app, "cancel_race_complete")
    cancelled = app.handle("task.cancel", {"task_id": submitted.ids["task_id"]})
    assert cancelled.ok
    assert cancelled.data["run"]["status"] == RunStatus.SUCCEEDED
    assert cancelled.data["run"]["cancel_requested"] is True
    assert cancelled.data["run"]["terminated"] is True
    events = app.handle("task.events", {"task_id": submitted.ids["task_id"]})
    types = [item["type"] for item in events.data["events"]]
    assert "cancel_requested" in types
    assert "run_succeeded" in types
    assert cancelled.data["run"]["status"] != RunStatus.CANCELLED


def test_expired_or_mismatched_approval_is_not_forwarded(app: Application) -> None:
    app.config.scheduler.per_backend_concurrency = 2
    _independent_workspace(app)
    fake = app.backends["fake"]
    submitted = _submit(app, "wait_input")
    approval_id = submitted.data["dispatch"]["approval_id"]
    approval = app.store.get_approval(ApprovalId(approval_id))
    approval.expires_at = "2020-01-01T00:00:00Z"
    app.store.save_approval(approval)
    before = [item.method for item in fake.calls]
    expired = app.handle("approval.respond", {"approval_id": approval_id, "decision": "approve"})
    assert expired.ok is False
    assert expired.error["code"] == INVALID_REQUEST
    assert "过期" in expired.error["message"]
    assert [item.method for item in fake.calls] == before
    still = app.store.get_approval(ApprovalId(approval_id))
    assert still.state is ApprovalState.PENDING

    fresh = _submit(app, "wait_input", workspace="other")
    fresh_id = fresh.data["dispatch"]["approval_id"]
    target = fresh.data["task"]["input_hash"]
    for payload in (
        {"expected_run_id": "run_other"},
        {"expected_task_id": "tsk_other"},
        {"expected_target_hash": "not-the-hash"},
    ):
        wrong = app.handle(
            "approval.respond",
            {"approval_id": fresh_id, "decision": "approve", **payload},
        )
        assert wrong.error["code"] == INVALID_REQUEST
    assert [item.method for item in fake.calls if item.method == "respond_approval"] == []
    matched = app.handle(
        "approval.respond",
        {
            "approval_id": fresh_id,
            "decision": "approve",
            "expected_run_id": fresh.ids["run_id"],
            "expected_task_id": fresh.ids["task_id"],
            "expected_target_hash": target,
        },
    )
    assert matched.ok
    assert matched.data["grant_unchanged"] is True
    assert matched.data["run"]["status"] == RunStatus.SUCCEEDED


def test_pause_stops_auto_submit_on_same_conversation(app: Application) -> None:
    fake = app.backends["fake"]
    first = _submit(app, "success", text="first")
    conversation_id = first.ids["conversation_id"]
    paused = app.handle("task.handoff", {"task_id": first.ids["task_id"], "action": "pause"})
    assert paused.ok
    assert paused.data["paused"] is True
    assert paused.data["orchestration_owner"] == "human"
    assert paused.data["auto_advance_stopped"] is True
    before = fake.dispatch_count
    blocked = app.handle(
        "task.submit",
        {"workspace": "demo", "script": "success", "conversation_id": conversation_id},
    )
    assert blocked.ok is False
    assert blocked.error["code"] == CAPABILITY_UNSUPPORTED
    assert fake.dispatch_count == before
    resumed = app.handle("task.handoff", {"task_id": first.ids["task_id"], "action": "resume"})
    assert resumed.ok
    assert resumed.data["orchestration_owner"] == "core"
    again = app.handle(
        "task.submit",
        {"workspace": "demo", "script": "success", "conversation_id": conversation_id},
    )
    assert again.ok
    assert fake.dispatch_count == before + 1


def test_external_change_invalidates_passed_acceptance(app: Application) -> None:
    submitted = _submit(app, "success")
    task = app.store.get_task(TaskId(submitted.ids["task_id"]))
    task.acceptance = AcceptanceStatus.PASSED
    app.store.save_task(task)
    result = app.handle(
        "task.handoff",
        {
            "task_id": submitted.ids["task_id"],
            "action": "pause",
            "external_change": True,
        },
    )
    assert result.ok
    assert result.data["task"]["evidence_stale"] is True
    assert result.data["task"]["acceptance"] == AcceptanceStatus.PENDING
    events = app.handle("task.events", {"task_id": submitted.ids["task_id"]})
    types = [item["type"] for item in events.data["events"]]
    assert "evidence_invalidated" in types


def test_handoff_requires_action(app: Application) -> None:
    submitted = _submit(app, "success")
    missing = app.handle("task.handoff", {"task_id": submitted.ids["task_id"]})
    assert missing.error["code"] == INVALID_REQUEST


def test_cli_handoff_and_reconcile(config_path: Path, isolated_env: Path) -> None:
    state = isolated_env / "state"
    submit = _run(
        [
            "--config",
            str(config_path),
            "--state-dir",
            str(state),
            "task-submit",
            "--workspace",
            "demo",
            "--script",
            "lost_reply",
        ]
    )
    assert submit.returncode == 0, submit.stderr + submit.stdout
    task_id = json.loads(submit.stdout)["ids"]["task_id"]
    reconciled = _run(
        ["--config", str(config_path), "--state-dir", str(state), "task-reconcile", task_id]
    )
    assert reconciled.returncode == 0, reconciled.stderr + reconciled.stdout
    assert json.loads(reconciled.stdout)["data"]["re_dispatched"] is False
    paused = _run(
        [
            "--config",
            str(config_path),
            "--state-dir",
            str(state),
            "task-handoff",
            task_id,
            "--action",
            "pause",
            "--external-change",
        ]
    )
    assert paused.returncode == 0, paused.stderr + paused.stdout
    payload = json.loads(paused.stdout)
    assert payload["data"]["paused"] is True
    assert payload["data"]["evidence_stale"] is True
