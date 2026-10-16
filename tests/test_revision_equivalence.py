"""Scheduler-only config changes stay execution-equivalent for existing tasks."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from asterun.application import Application
from asterun.backends.fake import FakeBackend
from asterun.config import load_config
from asterun.config_equivalence import execution_digest
from asterun.contracts import OperationStatus, RunStatus
from asterun.errors import REVISION_CONFLICT, AsterunError
from asterun.ids import AccountRef, RunId, RuntimeRef, TaskId
from asterun.policy import Principal
from asterun.store import MemoryStore
from tests.conftest import write_config


def _app(config_path: Path, store=None, **kwargs) -> Application:
    defaults = {
        "store": store or MemoryStore(),
        "principal": Principal("test:user", "test"),
        "account_ref": AccountRef("account:test"),
        "runtime_ref": RuntimeRef("host:test"),
    }
    defaults.update(kwargs)
    return Application(load_config(config_path), **defaults)


def _rewrite(path: Path, *, revision: int, scheduler: dict | None = None, workspace_root: str | None = None) -> None:
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["revision"] = revision
    if scheduler is not None:
        raw["scheduler"] = scheduler
    if workspace_root is not None:
        raw["workspaces"]["demo"]["root"] = workspace_root
    path.write_text(json.dumps(raw), encoding="utf-8")


def _apply(path: Path, store: MemoryStore, fake: FakeBackend, expected: int) -> Application:
    app = _app(path, store=store, backends={"fake": fake})
    applied = app.handle("config.apply", {"expected_revision": expected})
    assert applied.ok, applied.error
    return app


def test_execution_digest_ignores_revision_and_capacity(isolated_env: Path, workspace_root: Path) -> None:
    path = write_config(isolated_env / "config.json", workspace_root)
    base = execution_digest(load_config(path))

    _rewrite(path, revision=9)
    assert execution_digest(load_config(path)) == base

    _rewrite(path, revision=9, scheduler={
        "max_queue": 64,
        "per_backend_concurrency": 3,
        "max_concurrency": 8,
        "local_check_concurrency": 4,
        "max_runs_per_task": 4,
    })
    assert execution_digest(load_config(path)) == base

    _rewrite(path, revision=9, scheduler={
        "max_queue": 64,
        "per_backend_concurrency": 3,
        "max_concurrency": 8,
        "local_check_concurrency": 4,
        "max_runs_per_task": 8,
    })
    assert execution_digest(load_config(path)) != base

    other = isolated_env / "other-workspace"
    other.mkdir()
    moved = write_config(isolated_env / "moved.json", other)
    assert execution_digest(load_config(moved)) != base


def test_created_task_stores_execution_digest(isolated_env: Path, workspace_root: Path) -> None:
    path = write_config(isolated_env / "config.json", workspace_root)
    store = MemoryStore()
    fake = FakeBackend(load_config(path).backends["fake"])
    app = _app(path, store=store, backends={"fake": fake})
    submitted = app.handle("task.submit", {"workspace": "demo", "script": "success"})
    assert submitted.ok
    task_id = submitted.ids["task_id"]
    fetched = app.handle("task.get", {"task_id": task_id})
    assert fetched.ok
    assert fetched.data["task"]["execution_digest"]
    stored = store.get_task(TaskId(task_id))
    assert stored.execution_digest == fetched.data["task"]["execution_digest"]
    assert stored.execution_digest == execution_digest(app.config)
    assert stored.config_revision == 1


def test_scheduler_only_change_keeps_cancel_reconcile_and_binding(isolated_env: Path, workspace_root: Path) -> None:
    path = write_config(isolated_env / "config.json", workspace_root)
    store = MemoryStore()
    fake = FakeBackend(load_config(path).backends["fake"])
    app = _app(path, store=store, backends={"fake": fake})
    submitted = app.handle("task.submit", {"workspace": "demo", "script": "wait_input"})
    assert submitted.ok
    assert submitted.data["run"]["status"] == "waiting_input"
    task_id = submitted.ids["task_id"]
    task = store.get_task(TaskId(task_id))
    assert task.execution_digest

    _rewrite(path, revision=2, scheduler={"per_backend_concurrency": 2})
    app2 = _apply(path, store, fake, 1)
    assert execution_digest(app2.config) == task.execution_digest

    cancelled = app2.handle("task.cancel", {"task_id": task_id})
    assert cancelled.ok or cancelled.error["code"] != REVISION_CONFLICT
    reconciled = app2.handle("task.reconcile", {"task_id": task_id})
    assert reconciled.ok or reconciled.error["code"] != REVISION_CONFLICT
    app2._assert_task_binding(store.get_task(TaskId(task_id)))


def test_semantic_change_conflicts_but_cancel_does_not(isolated_env: Path, workspace_root: Path) -> None:
    path = write_config(isolated_env / "config.json", workspace_root)
    store = MemoryStore()
    fake = FakeBackend(load_config(path).backends["fake"])
    app = _app(path, store=store, backends={"fake": fake})
    submitted = app.handle("task.submit", {"workspace": "demo", "script": "wait_input"})
    assert submitted.ok
    task_id = submitted.ids["task_id"]

    _rewrite(path, revision=2, scheduler={"max_runs_per_task": 8})
    app2 = _apply(path, store, fake, 1)
    task = store.get_task(TaskId(task_id))
    assert task.execution_digest != execution_digest(app2.config)
    with pytest.raises(AsterunError) as caught:
        app2._assert_task_binding(task)
    assert caught.value.code == REVISION_CONFLICT
    assert caught.value.details["equivalent"] is False

    cancelled = app2.handle("task.cancel", {"task_id": task_id})
    assert cancelled.ok or cancelled.error["code"] != REVISION_CONFLICT


def test_legacy_task_without_digest_conflicts_but_cancel_does_not(isolated_env: Path, workspace_root: Path) -> None:
    path = write_config(isolated_env / "config.json", workspace_root)
    store = MemoryStore()
    fake = FakeBackend(load_config(path).backends["fake"])
    app = _app(path, store=store, backends={"fake": fake})
    submitted = app.handle("task.submit", {"workspace": "demo", "script": "wait_input"})
    assert submitted.ok
    task_id = submitted.ids["task_id"]
    task = store.get_task(TaskId(task_id))
    task.execution_digest = ""
    store.save_task(task)

    _rewrite(path, revision=2, scheduler={"per_backend_concurrency": 3, "max_queue": 64})
    app2 = _apply(path, store, fake, 1)
    with pytest.raises(AsterunError) as caught:
        app2._assert_task_binding(store.get_task(TaskId(task_id)))
    assert caught.value.code == REVISION_CONFLICT
    assert caught.value.details["equivalent"] is False

    cancelled = app2.handle("task.cancel", {"task_id": task_id})
    assert cancelled.ok or cancelled.error["code"] != REVISION_CONFLICT


def test_queued_task_survives_scheduler_only_change(isolated_env: Path, workspace_root: Path) -> None:
    path = write_config(isolated_env / "config.json", workspace_root)
    state = isolated_env / "state"
    app = Application.from_paths(path, state)
    try:
        submitted = app.handle("task.submit", {"workspace": "demo", "script": "success"})
        assert submitted.ok, submitted.error
        task_id = submitted.ids["task_id"]
        run_id = submitted.ids["run_id"]
        run = app.store.get_run(RunId(run_id))
        assert run.status == RunStatus.SUCCEEDED
        run.status = RunStatus.QUEUED
        run.terminated = False
        run.error_code = None
        run.cancel_requested = False
        app.store.save_run(run)
        operation = app.store.find_operation_for_run(run.id)
        operation.status = OperationStatus.INTENDED
        app.store.save_operation(operation)
        paused = app.handle("task.handoff", {"task_id": task_id, "action": "pause"})
        assert paused.ok, paused.error
        assert app.store.get_run(RunId(run_id)).status == RunStatus.PAUSED
    finally:
        app.close()

    _rewrite(path, revision=2, scheduler={"per_backend_concurrency": 2})
    app2 = Application.from_paths(path, state)
    try:
        applied = app2.handle("config.apply", {"expected_revision": 1})
        assert applied.ok, applied.error
        task = app2.store.get_task(TaskId(task_id))
        app2._assert_task_binding(task)
        resumed = app2.handle("task.handoff", {"task_id": task_id, "action": "resume"})
        assert resumed.ok, resumed.error
        run = app2.store.get_run(RunId(run_id))
        assert run.error_code != REVISION_CONFLICT
        assert run.status == RunStatus.SUCCEEDED
        reconciled = app2.handle("task.reconcile", {"task_id": task_id})
        assert reconciled.ok, reconciled.error
        assert app2.store.get_run(RunId(run_id)).error_code != REVISION_CONFLICT
    finally:
        app2.close()


@pytest.mark.parametrize("background", [False, True], ids=["foreground", "resident"])
@pytest.mark.parametrize("change", ["capacity", "semantic", "legacy"])
def test_unpaused_queue_waits_for_config_apply_after_restart(
    isolated_env: Path, workspace_root: Path, background: bool, change: str,
) -> None:
    other = isolated_env / "other-workspace"
    other.mkdir()
    path = write_config(isolated_env / "config.json", workspace_root, extra={
        "workspaces": {
            "demo": {"root": str(workspace_root), "allow_non_git": True},
            "other": {"root": str(other), "allow_non_git": True},
        },
        "scheduler": {"per_backend_concurrency": 1},
    })
    state = isolated_env / "state"
    app = Application.from_paths(path, state, background=background)
    try:
        holder = app.handle("task.submit", {"workspace": "demo", "script": "wait_input"})
        assert holder.ok, holder.error
        submitted = app.handle("task.submit", {"workspace": "other", "script": "success"})
        assert submitted.ok, submitted.error
        assert submitted.data["queued"] is True
        task_id, run_id = TaskId(submitted.ids["task_id"]), RunId(submitted.ids["run_id"])
        task = app.store.get_task(task_id)
        assert not task.paused
        if change == "legacy":
            task.execution_digest = ""
            app.store.save_task(task)
        assert app.backends["fake"].dispatch_count == 1
    finally:
        app.close()

    scheduler = {"per_backend_concurrency": 2}
    if change == "semantic":
        scheduler["max_runs_per_task"] = 8
    _rewrite(path, revision=2, scheduler=scheduler)
    app2 = Application.from_paths(path, state, background=background)
    try:
        fake = app2.backends["fake"]

        def assert_waiting() -> None:
            run = app2.store.get_run(run_id)
            assert run.status == RunStatus.QUEUED
            assert run.error_code is None
            assert app2.store.find_operation_for_run(run_id).status == OperationStatus.INTENDED
            assert any(item.run_id == run_id for item in app2.scheduler.queue)
            assert fake.dispatch_count == 0

        assert_waiting()
        # Read-only observation and a rejected CAS must not consume the queue.
        validated = app2.handle("config.validate")
        assert validated.ok and validated.data["config_apply_required"]
        assert_waiting()
        rejected = app2.handle("config.apply", {"expected_revision": 2})
        assert not rejected.ok and rejected.error["code"] == REVISION_CONFLICT
        assert_waiting()
        applied = app2.handle("config.apply", {"expected_revision": 1})
        assert applied.ok, applied.error
        app2.poll()
        run = app2.store.get_run(run_id)
        if change == "capacity":
            assert run.status == RunStatus.SUCCEEDED
            assert fake.dispatch_count == 1
        else:
            assert run.status == RunStatus.FAILED
            assert run.error_code == REVISION_CONFLICT
            assert fake.dispatch_count == 0
        app2.poll()
        assert fake.dispatch_count == (1 if change == "capacity" else 0)
        assert not any(item.run_id == run_id for item in app2.scheduler.queue)
    finally:
        app2.close()
