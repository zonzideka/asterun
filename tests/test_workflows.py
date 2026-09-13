from __future__ import annotations

from pathlib import Path

from asterun.application import Application
from asterun.backends.fake import FakeBackend
from asterun.config import load_config
from asterun.contracts import AcceptanceStatus, RunStatus
from asterun.errors import BUDGET_EXHAUSTED, INVALID_REQUEST, REVISION_CONFLICT
from asterun.policy import AllowConfiguredWorkspaces, Principal
from asterun.store import MemoryStore
from tests.conftest import write_config


def _app(config_path: Path, fake: FakeBackend | None = None) -> tuple[Application, FakeBackend]:
    config = load_config(config_path)
    backend = fake or FakeBackend(config.backends["fake"])
    backends = {name: FakeBackend(item) if name != "fake" else backend for name, item in config.backends.items()}
    app = Application(
        config,
        store=MemoryStore(),
        backends=backends,
        policy=AllowConfiguredWorkspaces({"demo"}),
        principal=Principal("test:user", "test"),
    )
    return app, backend


def test_submit_success_keeps_acceptance_pending_until_evaluate(app: Application) -> None:
    submitted = app.handle("task.submit", {"workspace": "demo", "text": "整理说明", "script": "success"})
    assert submitted.data["task"]["acceptance"] == AcceptanceStatus.PENDING
    evaluated = app.handle(
        "workflow.evaluate",
        {
            "task_id": submitted.ids["task_id"],
            "expected_revision": 1,
            "expected_input_hash": submitted.data["task"]["input_hash"],
            "checks": [{"kind": "contains", "text": "整理说明"}],
        },
    )
    assert evaluated.ok
    assert evaluated.data["acceptance"] == AcceptanceStatus.PASSED
    assert evaluated.data["bound_revision"] == 1
    assert evaluated.data["bound_input_hash"] == submitted.data["task"]["input_hash"]
    fetched = app.handle("task.get", {"task_id": submitted.ids["task_id"]})
    assert fetched.data["task"]["acceptance"] == AcceptanceStatus.PASSED


def test_run_succeeded_alone_is_not_acceptance(app: Application) -> None:
    submitted = app.handle("task.submit", {"workspace": "demo", "text": "ok", "script": "success"})
    evaluated = app.handle(
        "workflow.evaluate",
        {
            "task_id": submitted.ids["task_id"],
            "checks": [{"kind": "run_succeeded"}],
        },
    )
    assert evaluated.ok
    assert evaluated.data["acceptance"] == AcceptanceStatus.NOT_CONFIGURED
    assert evaluated.data["run_success_is_not_acceptance"] is True
    assert app.handle("task.get", {"task_id": submitted.ids["task_id"]}).data["task"]["acceptance"] == (
        AcceptanceStatus.NOT_CONFIGURED
    )


def test_missing_checks_is_not_configured(app: Application) -> None:
    submitted = app.handle("task.submit", {"workspace": "demo", "script": "success"})
    evaluated = app.handle("workflow.evaluate", {"task_id": submitted.ids["task_id"]})
    assert evaluated.data["acceptance"] == AcceptanceStatus.NOT_CONFIGURED


def test_hash_and_revision_mismatch_reject_old_evidence(app: Application) -> None:
    submitted = app.handle("task.submit", {"workspace": "demo", "text": "v1", "script": "success"})
    task_id = submitted.ids["task_id"]
    digest = submitted.data["task"]["input_hash"]
    bad_hash = app.handle(
        "workflow.evaluate",
        {
            "task_id": task_id,
            "expected_input_hash": "not-the-hash",
            "checks": [{"kind": "contains", "text": "v1"}],
        },
    )
    assert bad_hash.ok is False
    assert bad_hash.error["code"] == INVALID_REQUEST
    bad_rev = app.handle(
        "workflow.evaluate",
        {
            "task_id": task_id,
            "expected_revision": 9,
            "expected_input_hash": digest,
            "checks": [{"kind": "contains", "text": "v1"}],
        },
    )
    assert bad_rev.error["code"] == REVISION_CONFLICT
    assert app.handle("task.get", {"task_id": task_id}).data["task"]["acceptance"] == AcceptanceStatus.PENDING


def test_failed_contains_and_file_exists(app: Application, workspace_root: Path) -> None:
    submitted = app.handle("task.submit", {"workspace": "demo", "text": "只有这段", "script": "success"})
    task_id = submitted.ids["task_id"]
    failed = app.handle(
        "workflow.evaluate",
        {
            "task_id": task_id,
            "checks": [{"kind": "contains", "text": "不存在的句子"}],
        },
    )
    assert failed.data["acceptance"] == AcceptanceStatus.FAILED
    assert failed.data["findings"]
    existed = app.handle(
        "workflow.evaluate",
        {
            "task_id": task_id,
            "checks": [{"kind": "file_exists", "path": "note.txt"}],
        },
    )
    assert existed.data["acceptance"] == AcceptanceStatus.PASSED
    missing = app.handle(
        "workflow.evaluate",
        {
            "task_id": task_id,
            "checks": [{"kind": "file_exists", "path": "missing.txt"}],
        },
    )
    assert missing.data["acceptance"] == AcceptanceStatus.FAILED
    assert (workspace_root / "note.txt").is_file()


def test_review_unavailable_pauses_without_passing(isolated_env: Path, workspace_root: Path) -> None:
    path = write_config(
        isolated_env / "config.json",
        workspace_root,
        extra_backends={"reviewer": {"kind": "fake", "enabled": False}},
        extra={
            "workflow": {
                "preset": "quality",
                "require_review": True,
                "review_backend": "reviewer",
                "max_repairs": 2,
            }
        },
    )
    app, _ = _app(path)
    submitted = app.handle("task.submit", {"workspace": "demo", "text": "需要审查", "script": "success"})
    evaluated = app.handle(
        "workflow.evaluate",
        {
            "task_id": submitted.ids["task_id"],
            "checks": [{"kind": "contains", "text": "需要审查"}],
        },
    )
    assert evaluated.ok
    assert evaluated.data["acceptance"] == AcceptanceStatus.PENDING
    assert evaluated.data["paused_for_review"] is True
    assert evaluated.data["review_status"] == "unavailable"
    fetched = app.handle("task.get", {"task_id": submitted.ids["task_id"]})
    assert fetched.data["task"]["acceptance"] == AcceptanceStatus.PENDING
    assert fetched.data["task"]["paused"] is True
    assert fetched.data["task"]["orchestration_owner"] == "human"
    events = app.handle("task.events", {"task_id": submitted.ids["task_id"]})
    types = [item["type"] for item in events.data["events"]]
    assert "review_unavailable" in types
    assert "acceptance_evaluated" in types


def test_approved_substitute_configuration_is_not_review_evidence(isolated_env: Path, workspace_root: Path) -> None:
    path = write_config(
        isolated_env / "config.json",
        workspace_root,
        extra_backends={"reviewer": {"kind": "fake", "enabled": False}},
        extra={
            "workflow": {
                "preset": "quality",
                "require_review": True,
                "review_backend": "reviewer",
                "approved_substitute": "human-checklist",
                "max_repairs": 2,
            }
        },
    )
    app, _ = _app(path)
    submitted = app.handle("task.submit", {"workspace": "demo", "text": "可用替代", "script": "success"})
    evaluated = app.handle(
        "workflow.evaluate",
        {
            "task_id": submitted.ids["task_id"],
            "checks": [{"kind": "contains", "text": "可用替代"}],
        },
    )
    assert evaluated.data["acceptance"] == AcceptanceStatus.PENDING
    assert evaluated.data["used_substitute"] is False
    assert evaluated.data["review_status"] == "pending"
    fetched = app.handle("task.get", {"task_id": submitted.ids["task_id"]})
    assert fetched.data["task"]["paused"] is True
    assert fetched.data["task"]["review_status"] == "pending"


def test_repair_stops_at_limit_without_new_dispatch(isolated_env: Path, workspace_root: Path) -> None:
    path = write_config(
        isolated_env / "config.json",
        workspace_root,
        extra={"workflow": {"preset": "quality", "max_repairs": 1}},
    )
    app, fake = _app(path)
    submitted = app.handle("task.submit", {"workspace": "demo", "text": "缺内容", "script": "success"})
    first_count = fake.dispatch_count
    app.handle(
        "workflow.evaluate",
        {
            "task_id": submitted.ids["task_id"],
            "checks": [{"kind": "contains", "text": "必须出现的句子"}],
        },
    )
    repaired = app.handle("workflow.repair", {"task_id": submitted.ids["task_id"]})
    assert repaired.ok
    assert repaired.data["task"]["repair_count"] == 1
    assert fake.dispatch_count == first_count + 1
    blocked = app.handle("workflow.repair", {"task_id": submitted.ids["task_id"]})
    assert blocked.ok is False
    assert blocked.error["code"] == BUDGET_EXHAUSTED
    assert fake.dispatch_count == first_count + 1
    events = app.handle("task.events", {"task_id": submitted.ids["task_id"]})
    types = [item["type"] for item in events.data["events"]]
    assert "repair_limit" in types
