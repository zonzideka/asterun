from __future__ import annotations

from pathlib import Path

from asterun.application import Application
from asterun.backends.fake import FakeBackend
from asterun.config import load_config
from asterun.contracts import RunStatus
from asterun.errors import RATE_LIMITED
from asterun.policy import AllowConfiguredWorkspaces, Principal
from asterun.store import MemoryStore
from tests.conftest import write_config


def _scheduler_app(isolated_env: Path, workspace_root: Path, **scheduler: int) -> tuple[Application, FakeBackend]:
    path = write_config(
        isolated_env / "config.json",
        workspace_root,
        extra={"scheduler": {"max_queue": 1, "per_backend_concurrency": 1, "max_runs_per_task": 4, **scheduler}},
    )
    config = load_config(path)
    fake = FakeBackend(config.backends["fake"])
    app = Application(
        config,
        store=MemoryStore(),
        backends={"fake": fake},
        policy=AllowConfiguredWorkspaces({"demo"}),
        principal=Principal("test:user", "test"),
    )
    return app, fake


def test_second_submit_queues_until_slot_frees(isolated_env: Path, workspace_root: Path) -> None:
    app, fake = _scheduler_app(isolated_env, workspace_root)
    first = app.handle("task.submit", {"workspace": "demo", "text": "占槽", "script": "cancel_accept_only"})
    assert first.data["run"]["status"] == RunStatus.RUNNING
    assert first.data["dispatched"] is True
    assert fake.dispatch_count == 1
    second = app.handle("task.submit", {"workspace": "demo", "text": "排队", "script": "success"})
    assert second.ok
    assert second.data["queued"] is True
    assert second.data["dispatched"] is False
    assert second.data["run"]["status"] == RunStatus.QUEUED
    assert fake.dispatch_count == 1
    events = app.handle("task.events", {"task_id": second.ids["task_id"]})
    types = [item["type"] for item in events.data["events"]]
    assert "queue_accepted" in types
    status = app.handle("scheduler.status", {})
    assert status.data["queued"] == 1
    assert status.data["notes"]["tuning_defaults_are_not_measured_slo"] is True


def test_cancel_terminate_drains_queue(isolated_env: Path, workspace_root: Path) -> None:
    app, fake = _scheduler_app(isolated_env, workspace_root)
    holder = app.handle("task.submit", {"workspace": "demo", "script": "cancel_and_stop"})
    queued = app.handle("task.submit", {"workspace": "demo", "text": "随后执行", "script": "success"})
    assert fake.dispatch_count == 1
    cancelled = app.handle("task.cancel", {"task_id": holder.ids["task_id"]})
    assert cancelled.data["terminated"] is True
    assert fake.dispatch_count == 2
    fetched = app.handle("task.get", {"task_id": queued.ids["task_id"]})
    assert fetched.data["run"]["status"] == RunStatus.SUCCEEDED
    assert fetched.data["task"]["acceptance"] == "pending"


def test_full_queue_does_not_create_task(isolated_env: Path, workspace_root: Path) -> None:
    app, fake = _scheduler_app(isolated_env, workspace_root, max_queue=1)
    app.handle("task.submit", {"workspace": "demo", "script": "cancel_accept_only"})
    app.handle("task.submit", {"workspace": "demo", "text": "入队", "script": "success"})
    before = list(app.store.list_task_ids())
    rejected = app.handle("task.submit", {"workspace": "demo", "text": "超员", "script": "success"})
    assert rejected.ok is False
    assert rejected.error["code"] == RATE_LIMITED
    assert "未创建新任务" in rejected.error["message"]
    assert list(app.store.list_task_ids()) == before
    assert fake.dispatch_count == 1


def test_human_owned_queued_task_is_not_auto_drained(isolated_env: Path, workspace_root: Path) -> None:
    app, fake = _scheduler_app(isolated_env, workspace_root)
    holder = app.handle("task.submit", {"workspace": "demo", "script": "cancel_and_stop"})
    queued = app.handle("task.submit", {"workspace": "demo", "text": "人工项", "script": "success"})
    paused = app.handle("task.handoff", {"task_id": queued.ids["task_id"], "action": "pause"})
    assert paused.data["orchestration_owner"] == "human"
    app.handle("task.cancel", {"task_id": holder.ids["task_id"]})
    assert fake.dispatch_count == 1
    fetched = app.handle("task.get", {"task_id": queued.ids["task_id"]})
    assert fetched.data["run"]["status"] in {RunStatus.QUEUED, RunStatus.PAUSED}
    assert fetched.data["run"]["status"] != RunStatus.SUCCEEDED
    assert fetched.data["task"]["paused"] is True
    snapshot = app.handle("scheduler.status", {})
    assert queued.ids["task_id"] in {item["task_id"] for item in snapshot.data["queue"]}
