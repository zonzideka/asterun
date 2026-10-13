"""Operator path: task.reconcile --terminate ends one stuck run and does not re-dispatch."""
from __future__ import annotations

import json

from asterun.application import Application
from asterun.cli import build_parser
from asterun.contracts import OperationStatus, RunStatus
from asterun.errors import INVALID_REQUEST, REMOTE_STATE_UNKNOWN, RUN_ALREADY_TERMINAL
from asterun.ids import RunId
from tests.test_grok_lost_process import park_pending
from tests.test_grok_lost_same_boot import NS, write_pid_ns

REUSED_STAT = "123 (x) S 1 123 123 0 -1 4194560 1 0 0 0 0 0 0 0 20 0 1 0 9999 0\n"


def _reuse_pid(isolated_env) -> None:
    (isolated_env / "proc" / "123" / "stat").write_text(REUSED_STAT)


def _terminate(app, task_id, *, reason="operator stop", confirm=False):
    payload = {"task_id": task_id, "terminate": True, "reason": reason}
    if confirm:
        payload["confirm_process_gone"] = True
    return app.handle("task.reconcile", payload)


def _stamp_same_boot(app, isolated_env, run_id) -> None:
    write_pid_ns(isolated_env / "proc")
    run = app.store.get_run(RunId(run_id))
    run.native["process"]["pid_ns"] = NS
    run.native["process"]["launch_marker"] = "run:s1"
    app.store.save_run(run)


def test_live_leader_stays_pending(isolated_env, workspace_root, monkeypatch):
    config, state, task_id, run_id, _boot_path = park_pending(isolated_env, workspace_root, monkeypatch)
    app = Application.from_paths(config, state)
    try:
        _stamp_same_boot(app, isolated_env, run_id)
        result = _terminate(app, task_id, reason="x", confirm=True)
        assert not result.ok
        assert result.error["code"] == REMOTE_STATE_UNKNOWN
        assert result.error["details"]["process_evidence"] == "alive"
        run = app.store.get_run(RunId(run_id))
        assert run.status == RunStatus.PENDING_RECONCILE
        assert run.terminated is False
        assert app.scheduler.snapshot()["inflight"] == {"grok": 1}
    finally:
        app.close()


def test_unproven_death_needs_confirm_then_releases_slot(isolated_env, workspace_root, monkeypatch):
    config, state, task_id, run_id, _boot_path = park_pending(isolated_env, workspace_root, monkeypatch)
    app = Application.from_paths(config, state)
    try:
        _reuse_pid(isolated_env)
        denied = _terminate(app, task_id, reason="operator stop")
        assert not denied.ok
        assert denied.error["code"] == INVALID_REQUEST
        assert denied.error["details"]["process_evidence"] == "unknown"
        assert app.store.get_run(RunId(run_id)).status == RunStatus.PENDING_RECONCILE

        done = _terminate(app, task_id, reason="operator stop", confirm=True)
        assert done.ok, done.error
        assert done.data["process_evidence"] == "operator_confirmed"
        assert done.data["re_dispatched"] is False
        assert done.data["terminated"] is True
        run = app.store.get_run(RunId(run_id))
        assert run.status == RunStatus.CANCELLED
        assert run.terminated is True
        assert run.native["operator_termination"]["reason"] == "operator stop"
        assert run.native["operator_termination"]["process_evidence"] == "operator_confirmed"
        operation = app.store.find_operation_for_run(run.id)
        assert operation.status != OperationStatus.UNKNOWN
        assert app.scheduler.snapshot()["inflight"] == {}

        second = app.handle("task.submit", {"workspace": "demo", "backend": "grok", "text": "continue"})
        assert second.ok, second.error
        assert second.data["queued"] is False
    finally:
        app.close()


def test_live_leader_unreadable_environ_blocks_confirm(isolated_env, workspace_root, monkeypatch):
    config, state, task_id, run_id, _boot_path = park_pending(isolated_env, workspace_root, monkeypatch)

    def unreadable(_entry):
        raise PermissionError("environ")

    monkeypatch.setattr("asterun.backends.grok_code._proc_environ", unreadable)
    app = Application.from_paths(config, state)
    try:
        _stamp_same_boot(app, isolated_env, run_id)
        result = _terminate(app, task_id, reason="operator stop", confirm=True)
        assert not result.ok
        assert result.error["code"] == REMOTE_STATE_UNKNOWN
        assert result.error["details"]["process_evidence"] == "alive"
        run = app.store.get_run(RunId(run_id))
        assert run.status == RunStatus.PENDING_RECONCILE
        assert run.terminated is False
        assert app.scheduler.snapshot()["inflight"] == {"grok": 1}
    finally:
        app.close()


def test_live_group_child_blocks_confirm(isolated_env, workspace_root, monkeypatch):
    config, state, task_id, run_id, _boot_path = park_pending(isolated_env, workspace_root, monkeypatch)
    proc = isolated_env / "proc"
    # Reused leader is not the recorded process and is not in the recorded group.
    (proc / "123" / "stat").write_text(
        "123 (x) S 1 1 1 0 -1 4194560 1 0 0 0 0 0 0 0 20 0 1 0 9999 0\n"
    )
    (proc / "456").mkdir(parents=True, exist_ok=True)
    (proc / "456" / "stat").write_text(
        "456 (tool) S 1 123 123 0 -1 4194560 1 0 0 0 0 0 0 0 20 0 1 0 6000 0\n"
    )
    app = Application.from_paths(config, state)
    try:
        _stamp_same_boot(app, isolated_env, run_id)
        result = _terminate(app, task_id, reason="operator stop", confirm=True)
        assert not result.ok
        assert result.error["code"] == REMOTE_STATE_UNKNOWN
        assert result.error["details"]["process_evidence"] == "alive"
        run = app.store.get_run(RunId(run_id))
        assert run.status == RunStatus.PENDING_RECONCILE
        assert run.terminated is False
        assert app.scheduler.snapshot()["inflight"] == {"grok": 1}
    finally:
        app.close()


def test_missing_reason_and_second_terminate(isolated_env, workspace_root, monkeypatch):
    config, state, task_id, run_id, _boot_path = park_pending(isolated_env, workspace_root, monkeypatch)
    app = Application.from_paths(config, state)
    try:
        missing = app.handle("task.reconcile", {"task_id": task_id, "terminate": True})
        assert not missing.ok
        assert missing.error["code"] == INVALID_REQUEST
        assert app.store.get_run(RunId(run_id)).status == RunStatus.PENDING_RECONCILE

        _reuse_pid(isolated_env)
        done = _terminate(app, task_id, reason="done once", confirm=True)
        assert done.ok, done.error
        again = _terminate(app, task_id, reason="done once", confirm=True)
        assert not again.ok
        assert again.error["code"] == RUN_ALREADY_TERMINAL
        assert app.store.get_run(RunId(run_id)).status == RunStatus.CANCELLED
    finally:
        app.close()


def test_semantic_revision_change_still_terminates(isolated_env, workspace_root, monkeypatch):
    config, state, task_id, run_id, _boot_path = park_pending(isolated_env, workspace_root, monkeypatch)
    raw = json.loads(config.read_text(encoding="utf-8"))
    raw["revision"] = 2
    scheduler = dict(raw.get("scheduler") or {})
    scheduler["max_runs_per_task"] = 8
    raw["scheduler"] = scheduler
    config.write_text(json.dumps(raw), encoding="utf-8")
    app = Application.from_paths(config, state)
    try:
        applied = app.handle("config.apply", {"expected_revision": 1})
        assert applied.ok, applied.error
        _reuse_pid(isolated_env)
        done = _terminate(app, task_id, reason="old revision", confirm=True)
        assert done.ok, done.error
        assert done.error is None
        assert done.data["process_evidence"] == "operator_confirmed"
        run = app.store.get_run(RunId(run_id))
        assert run.status == RunStatus.CANCELLED
        assert run.terminated is True
    finally:
        app.close()


def test_plain_reconcile_stays_unknown(isolated_env, workspace_root, monkeypatch):
    config, state, task_id, run_id, _boot_path = park_pending(isolated_env, workspace_root, monkeypatch)
    app = Application.from_paths(config, state)
    try:
        reconciled = app.handle("task.reconcile", {"task_id": task_id})
        assert reconciled.ok, reconciled.error
        assert reconciled.data["unknown_states"]
        assert reconciled.data["re_dispatched"] is False
        run = app.store.get_run(RunId(run_id))
        assert run.status == RunStatus.PENDING_RECONCILE
        assert run.terminated is False
        assert app.scheduler.snapshot()["inflight"] == {"grok": 1}
    finally:
        app.close()


def test_cli_parses_terminate_flags():
    args = build_parser().parse_args(
        ["task-reconcile", "T", "--terminate", "--reason", "r", "--confirm-process-gone"]
    )
    assert args.command == "task-reconcile"
    assert args.task_id == "T"
    assert args.terminate is True
    assert args.reason == "r"
    assert args.confirm_process_gone is True
