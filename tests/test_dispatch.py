from __future__ import annotations

from asterun.application import Application
from asterun.backends.fake import FakeBackend
from asterun.config import load_config
from asterun.mcp_server import handle_rpc
from asterun.policy import AllowConfiguredWorkspaces, DenyAll, Principal
from asterun.store import MemoryStore


def _app(config_path, policy=None):
    config = load_config(config_path)
    fake = FakeBackend(config.backends["fake"])
    app = Application(
        config,
        store=MemoryStore(),
        backends={"fake": fake},
        policy=policy or AllowConfiguredWorkspaces({"demo"}),
        principal=Principal("test:user", "test"),
    )
    return app, fake


def test_duplicate_idempotency_key_dispatches_once(config_path):
    app, fake = _app(config_path)
    payload = {"workspace": "demo", "script": "success", "text": "same", "idempotency_key": "k1"}
    first = app.handle("task.submit", payload)
    second = app.handle("task.submit", payload)
    assert first.ok and second.ok
    assert first.ids["task_id"] == second.ids["task_id"]
    assert second.data["reused"] is True
    assert fake.dispatch_count == 1


def test_same_key_different_input_conflicts(config_path):
    app, fake = _app(config_path)
    app.handle("task.submit", {"workspace": "demo", "script": "success", "text": "a", "idempotency_key": "k2"})
    conflict = app.handle(
        "task.submit",
        {"workspace": "demo", "script": "success", "text": "b", "idempotency_key": "k2"},
    )
    assert conflict.ok is False
    assert conflict.error["code"] == "IDEMPOTENCY_CONFLICT"
    assert fake.dispatch_count == 1


def test_intent_write_failure_does_not_dispatch(config_path):
    app, fake = _app(config_path)
    app.store.fail_next_intent = True
    result = app.handle("task.submit", {"workspace": "demo", "script": "success"})
    assert result.error["code"] == "INTENT_PERSIST_FAILED"
    assert fake.dispatch_count == 0
    assert app.store.tasks == {}


def test_cli_and_mcp_share_policy_decision(config_path):
    app, fake = _app(config_path, policy=DenyAll())
    payload = {"workspace": "demo", "script": "success"}
    cli = app.handle("task.submit", payload)
    mcp = handle_rpc(
        app,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "task_submit", "arguments": payload}},
    )
    assert cli.error["code"] == "SCOPE_DENIED"
    body = mcp["result"]
    assert body["isError"] is True
    assert "SCOPE_DENIED" in body["content"][0]["text"]
    assert fake.dispatch_count == 0
