from __future__ import annotations

from pathlib import Path

from asterun.application import Application
from asterun.backends.fake import FakeBackend
from asterun.config import load_config
from asterun.contracts import AcceptanceStatus, RunStatus
from asterun.errors import (
    CAPABILITY_UNSUPPORTED,
    INVALID_REQUEST,
    REVISION_CONFLICT,
    SCOPE_DENIED,
    UNKNOWN_PROJECT,
)
from asterun.policy import AllowConfiguredWorkspaces, DenyAll, Principal
from asterun.store import MemoryStore
from tests.conftest import write_config


def _denied_app(config_path: Path) -> tuple[Application, FakeBackend]:
    config = load_config(config_path)
    fake = FakeBackend(config.backends["fake"])
    app = Application(
        config,
        store=MemoryStore(),
        backends={"fake": fake},
        policy=DenyAll(),
        principal=Principal("test:user", "test"),
    )
    return app, fake


def test_non_git_success_and_acceptance_stays_pending(app: Application) -> None:
    result = app.handle(
        "task.submit",
        {"workspace": "demo", "text": "整理一份非 Git 说明", "script": "success"},
    )
    assert result.ok
    assert result.data["run"]["status"] == RunStatus.SUCCEEDED
    assert result.data["task"]["acceptance"] == AcceptanceStatus.PENDING
    assert result.data["notes"]["run_success_is_not_acceptance"] is True
    events = app.handle("task.events", {"task_id": result.ids["task_id"]})
    types = [item["type"] for item in events.data["events"]]
    assert "run_succeeded" in types


def test_fake_failure_and_events_scripts(app: Application) -> None:
    failed = app.handle("task.submit", {"workspace": "demo", "script": "failure"})
    assert failed.ok
    assert failed.data["run"]["status"] == RunStatus.FAILED
    sequenced = app.handle("task.submit", {"workspace": "demo", "script": "events"})
    events = app.handle("task.events", {"task_id": sequenced.ids["task_id"]})
    messages = [item for item in events.data["events"] if item["type"] == "message"]
    assert len(messages) >= 2


def test_wait_input_and_matching_approval(app: Application) -> None:
    submitted = app.handle("task.submit", {"workspace": "demo", "script": "wait_input"})
    assert submitted.data["run"]["status"] == RunStatus.WAITING_INPUT
    approval_id = submitted.data["dispatch"]["approval_id"]
    wrong = app.handle("approval.respond", {"approval_id": "apr_missing", "decision": "approve"})
    assert wrong.ok is False
    approved = app.handle("approval.respond", {"approval_id": approval_id, "decision": "approve"})
    assert approved.ok
    assert approved.data["run"]["status"] == RunStatus.SUCCEEDED


def test_cancel_requested_is_not_terminated(app: Application) -> None:
    submitted = app.handle("task.submit", {"workspace": "demo", "script": "cancel_accept_only"})
    cancelled = app.handle("task.cancel", {"task_id": submitted.ids["task_id"]})
    assert cancelled.ok
    assert cancelled.data["cancel_requested"] is True
    assert cancelled.data["terminated"] is False
    assert cancelled.data["run"]["status"] == RunStatus.RUNNING
    events = app.handle("task.events", {"task_id": submitted.ids["task_id"]})
    types = [item["type"] for item in events.data["events"]]
    assert "cancel_requested" in types
    assert "run_terminated" not in types


def test_cancel_can_also_terminate(app: Application) -> None:
    submitted = app.handle("task.submit", {"workspace": "demo", "script": "cancel_and_stop"})
    cancelled = app.handle("task.cancel", {"task_id": submitted.ids["task_id"]})
    assert cancelled.data["cancel_requested"] is True
    assert cancelled.data["terminated"] is True
    assert cancelled.data["run"]["status"] == RunStatus.CANCELLED
    events = app.handle("task.events", {"task_id": submitted.ids["task_id"]})
    types = [item["type"] for item in events.data["events"]]
    assert "cancel_requested" in types
    assert "run_terminated" in types


def test_unsupported_capability(app: Application) -> None:
    result = app.handle("task.submit", {"workspace": "demo", "script": "unsupported"})
    assert result.ok is False
    assert result.error["code"] == CAPABILITY_UNSUPPORTED
    resume = app.handle("session.resume", {"workspace": "demo"})
    assert resume.error["code"] == INVALID_REQUEST


def test_denied_submit_does_not_reach_fake(config_path: Path) -> None:
    app, fake = _denied_app(config_path)
    result = app.handle("task.submit", {"workspace": "demo", "script": "success"})
    assert result.ok is False
    assert result.error["code"] == SCOPE_DENIED
    assert fake.dispatch_count == 0
    assert all(item.method != "dispatch" for item in fake.calls)


def test_self_reported_authority_does_not_reach_fake(config_path: Path) -> None:
    config = load_config(config_path)
    fake = FakeBackend(config.backends["fake"])
    app = Application(
        config,
        store=MemoryStore(),
        backends={"fake": fake},
        policy=AllowConfiguredWorkspaces({"demo"}),
        principal=Principal("test:user", "test"),
    )
    result = app.handle(
        "task.submit",
        {
            "workspace": "demo",
            "script": "success",
            "principal": {"id": "admin"},
            "granted": True,
            "set_ready": True,
        },
    )
    assert result.error["code"] == SCOPE_DENIED
    assert fake.dispatch_count == 0
    assert fake.calls == []


def test_unknown_project_and_revision_conflict(app: Application) -> None:
    missing = app.handle("task.submit", {"workspace": "author-lab", "script": "success"})
    assert missing.error["code"] == UNKNOWN_PROJECT
    conflict = app.handle(
        "task.submit",
        {"workspace": "demo", "script": "success", "expected_revision": 9},
    )
    assert conflict.error["code"] == REVISION_CONFLICT


def test_fake_inspect_is_not_real_connection(app: Application) -> None:
    inspected = app.handle("backend.inspect", {})
    row = inspected.data["backends"][0]
    assert row["kind"] == "fake"
    assert row["is_real_connection"] is False
    assert row["connection_display"] == "simulated"
    assert "真实" not in row["message"] or "不是真实" in row["message"]
    verified = app.handle("connection.verify", {"backend": "fake"})
    assert verified.data["is_real_connection"] is False
    assert verified.data["simulated"] is True
    assert verified.data["verified"] is False


def test_real_backend_execution_stays_disabled(
    isolated_env: Path, workspace_root: Path
) -> None:
    path = write_config(
        isolated_env / "config.json",
        workspace_root,
        extra_backends={"codex": {"kind": "codex", "enabled": True}},
    )
    app = Application(load_config(path), principal=Principal("test:user", "test"))
    inspected = app.handle("backend.inspect", {"backend": "codex"})
    row = inspected.data["backends"][0]
    assert row["kind"] == "codex"
    assert row["execution_enabled"] is False
    assert row["is_real_connection"] is False
    assert row["authentication"] == "not_tested"
    assert row["binary_found"] is False
    capabilities = {item["name"]: item for item in row["capabilities"]}
    assert capabilities["submit"]["adapter_support"] == "supported"
    assert capabilities["submit"]["verification_status"] == "not_tested"
    verify = app.handle("connection.verify", {"backend": "codex"})
    assert verify.ok
    assert verify.data["is_real_connection"] is False
    assert verify.data["authentication"] == "not_tested"
    assert verify.data["verified"] is False
    submitted = app.handle("task.submit", {"workspace": "demo", "backend": "codex"})
    assert submitted.ok is False
    assert submitted.error["code"] == "BACKEND_UNAVAILABLE"
    assert app.backends["codex"].dispatch_count == 0
    assert app.store.tasks == {}


def test_grok_and_claude_adapters_stay_gated(
    isolated_env: Path, workspace_root: Path
) -> None:
    path = write_config(
        isolated_env / "config.json",
        workspace_root,
        extra_backends={
            "grok": {"kind": "grok", "enabled": True},
            "claude": {"kind": "claude", "enabled": True},
        },
    )
    app = Application(load_config(path), principal=Principal("test:user", "test"))
    for name in ("grok", "claude"):
        inspected = app.handle("backend.inspect", {"backend": name})
        row = inspected.data["backends"][0]
        assert row["kind"] == name
        assert row["execution_enabled"] is False
        assert row["is_real_connection"] is False
        assert row["authentication"] == "not_tested"
        assert row["binary_found"] is False
        verify = app.handle("connection.verify", {"backend": name})
        assert verify.ok
        assert verify.data["verified"] is False
        submitted = app.handle("task.submit", {"workspace": "demo", "backend": name})
        assert submitted.ok is False
        assert submitted.error["code"] == "BACKEND_UNAVAILABLE"
        assert app.backends[name].dispatch_count == 0
        assert app.store.tasks == {}


def test_path_input_stays_inside_workspace(app: Application, workspace_root: Path) -> None:
    result = app.handle("task.submit", {"workspace": "demo", "path": "note.txt"})
    assert result.ok
    assert "非 Git 输入" in result.data["task"]["text"]
    outside = app.handle("task.submit", {"workspace": "demo", "path": "../secret"})
    assert outside.error["code"] == "PATH_OUT_OF_SCOPE"


def test_config_validate_roundtrip(app: Application) -> None:
    result = app.handle("config.validate", {})
    assert result.ok
    assert result.data["valid"] is True
    assert result.data["config"]["revision"] == 1
