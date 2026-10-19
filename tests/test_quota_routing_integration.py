"""候选组额度优选：真实 Application + fake 后端，经 task.submit / task.get。"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from asterun.application import Application
from asterun.config import load_config
from asterun.config_v2 import _ROLE_KINDS
from asterun.errors import INVALID_CONFIG, AsterunError
from asterun.policy import AllowConfiguredWorkspaces, Principal
from asterun.quota import routing_entry


def _pool() -> dict:
    return {
        "billing_mode": "local",
        "remaining": None,
        "paid_overage_allowed": False,
        "overage_verified": False,
        "allow_unknown_subscription": False,
        "risk_acceptance": None,
        "meter": "requests",
        "local_limit": 100,
        "max_concurrency": 1,
        "window_id": "local",
        "estimate_per_action": 1,
    }


def _account() -> dict:
    return {
        "provider": "fake",
        "credential_ref": None,
        "credential_revision": 1,
        "revoked": False,
    }


def _connection(account: str, pool: str) -> dict:
    return {
        "plugin_id": "asterun.fake",
        "enabled": True,
        "provider_account_ref": account,
        "runtime_ref": "runtime:local",
        "auth_mode": "none",
        "billing_pool_refs": [pool],
        "options": {},
        "upstream_version": None,
    }


def _config(workspace: Path, *, mode: str, default_backend: str = "fa") -> dict:
    return {
        "schema_version": 2,
        "revision": 1,
        "workspaces": {"demo": {"root": str(workspace), "allow_non_git": True}},
        "default_backend": default_backend,
        "plugins": {"asterun.fake": {"enabled": True}},
        "provider_accounts": {"acct-fa": _account(), "acct-fb": _account()},
        "billing_pools": {"pool-fa": _pool(), "pool-fb": _pool()},
        "connections": {
            "conn-fa": _connection("acct-fa", "pool-fa"),
            "conn-fb": _connection("acct-fb", "pool-fb"),
        },
        "backends": {
            "fa": {"connection_ref": "conn-fa"},
            "fb": {"connection_ref": "conn-fb"},
        },
        "routing": {
            "automatic_paid_fallback": False,
            "automatic_account_switch": mode == "on",
            "default_order": [],
            "rules": [],
            "candidate_groups": {
                "g": {"role": "implementation", "members": ["fa", "fb"], "primary": "fa"},
            },
            "quota_preference": {"enabled": mode != "off", "mode": mode},
        },
    }


class _Choice:
    """替换 evaluate_group。改 chosen 即模拟额度变化后的选择。"""

    def __init__(self, chosen: str = "fb") -> None:
        self.chosen = chosen

    def __call__(self, app, group, sticky=None):
        return {
            "chosen": self.chosen,
            "primary": "fa",
            "reason_code": "lower_used",
            "candidates": [],
        }


def _allow_fake_implementation(monkeypatch: pytest.MonkeyPatch) -> None:
    allowed = set(_ROLE_KINDS["implementation"])
    allowed.add("fake")
    monkeypatch.setitem(_ROLE_KINDS, "implementation", allowed)


def _patch_evaluate(monkeypatch: pytest.MonkeyPatch, choice: _Choice) -> None:
    # resolve_backend 调用的是 routing_entry 模块里绑定的 evaluate_group。
    monkeypatch.setattr(routing_entry, "evaluate_group", choice)


def _write(path: Path, workspace: Path, *, mode: str, default_backend: str) -> Path:
    path.write_text(
        json.dumps(_config(workspace, mode=mode, default_backend=default_backend), ensure_ascii=False),
        encoding="utf-8",
    )
    return path


@contextmanager
def _open(workspace: Path, monkeypatch: pytest.MonkeyPatch, *, mode: str, default_backend: str = "fa", chosen: str = "fb"):
    _allow_fake_implementation(monkeypatch)
    choice = _Choice(chosen)
    _patch_evaluate(monkeypatch, choice)
    root = workspace.parent
    config_path = _write(root / "quota-routing.json", workspace, mode=mode, default_backend=default_backend)
    app = Application.from_paths(
        config_path,
        root / "state",
        policy=AllowConfiguredWorkspaces({"demo"}),
        principal=Principal("test:user", "test"),
    )
    try:
        yield app, choice
    finally:
        app.close()


def _ok(result):
    assert result.ok, result.error
    return result


def _submit(app, **overrides):
    payload = {
        "workspace": "demo",
        "text": "quota-route",
        "script": "success",
        "backend": "@g",
    }
    payload.update(overrides)
    if "backend" in overrides and overrides["backend"] is None:
        payload.pop("backend")
    return _ok(app.handle("task.submit", payload))


def _view(app, task_id: str):
    return _ok(app.handle("task.get", {"task_id": task_id}))


def _decision_count(app, task_id: str | None = None) -> int:
    if task_id is None:
        row = app.store._conn.execute("SELECT COUNT(*) FROM plugin_routing_decisions").fetchone()
    else:
        row = app.store._conn.execute(
            "SELECT COUNT(*) FROM plugin_routing_decisions WHERE task_id=?",
            (task_id,),
        ).fetchone()
    return int(row[0])


def test_mode_on_dispatches_chosen(workspace_root: Path, monkeypatch: pytest.MonkeyPatch):
    with _open(workspace_root, monkeypatch, mode="on") as (app, _choice):
        submitted = _submit(app)
        viewed = _view(app, submitted.ids["task_id"])
        assert submitted.data["task"]["backend"] == "fb"
        assert viewed.data["task"]["backend"] == "fb"
        assert viewed.data["routing"]["mode"] == "on"
        assert viewed.data["routing"]["chosen"] == "fb"


def test_dry_run_dispatches_primary_and_records_would_choose(workspace_root: Path, monkeypatch: pytest.MonkeyPatch):
    with _open(workspace_root, monkeypatch, mode="dry_run") as (app, _choice):
        submitted = _submit(app)
        viewed = _view(app, submitted.ids["task_id"])
        assert submitted.data["task"]["backend"] == "fa"
        assert viewed.data["task"]["backend"] == "fa"
        assert viewed.data["routing"]["mode"] == "dry_run"
        assert viewed.data["routing"]["would_choose"] == "fb"


def test_mode_off_dispatches_primary(workspace_root: Path, monkeypatch: pytest.MonkeyPatch):
    with _open(workspace_root, monkeypatch, mode="off") as (app, _choice):
        submitted = _submit(app)
        viewed = _view(app, submitted.ids["task_id"])
        assert submitted.data["task"]["backend"] == "fa"
        assert viewed.data["task"]["backend"] == "fa"
        assert viewed.data["routing"]["mode"] == "off"


def test_idempotent_replay_keeps_first_choice(workspace_root: Path, monkeypatch: pytest.MonkeyPatch):
    with _open(workspace_root, monkeypatch, mode="on") as (app, choice):
        payload = {
            "workspace": "demo",
            "text": "same-request",
            "script": "success",
            "backend": "@g",
            "idempotency_key": "idem-quota",
        }
        first = _ok(app.handle("task.submit", payload))
        assert first.data["task"]["backend"] == "fb"
        choice.chosen = "fa"
        second = _ok(app.handle("task.submit", dict(payload)))
        assert second.ids["task_id"] == first.ids["task_id"]
        assert second.data["task"]["backend"] == "fb"
        viewed = _view(app, first.ids["task_id"])
        assert viewed.data["task"]["backend"] == "fb"
        assert viewed.data["routing"]["mode"] == "on"
        assert viewed.data["routing"]["chosen"] == "fb"
        assert _decision_count(app, first.ids["task_id"]) == 1


def test_conversation_keeps_pinned_backend(workspace_root: Path, monkeypatch: pytest.MonkeyPatch):
    with _open(workspace_root, monkeypatch, mode="on") as (app, choice):
        first = _submit(app, text="first-turn", idempotency_key="turn-1")
        assert first.data["task"]["backend"] == "fb"
        assert first.data["run"]["status"] == "succeeded"
        choice.chosen = "fa"
        second = _submit(
            app,
            text="next-turn",
            idempotency_key="turn-2",
            conversation_id=first.ids["conversation_id"],
        )
        assert second.ok
        assert second.ids["task_id"] != first.ids["task_id"]
        assert second.data["task"]["backend"] == "fb"
        viewed = _view(app, second.ids["task_id"])
        assert viewed.data["task"]["backend"] == "fb"


def test_explicit_backend_skips_routing(workspace_root: Path, monkeypatch: pytest.MonkeyPatch):
    with _open(workspace_root, monkeypatch, mode="on") as (app, _choice):
        submitted = _submit(app, backend="fa", text="explicit")
        viewed = _view(app, submitted.ids["task_id"])
        assert submitted.data["task"]["backend"] == "fa"
        assert "routing" not in submitted.data
        assert "routing" not in viewed.data
        assert _decision_count(app) == 0


def test_default_backend_group_reference(workspace_root: Path, monkeypatch: pytest.MonkeyPatch):
    _allow_fake_implementation(monkeypatch)
    config_path = _write(
        workspace_root.parent / "quota-routing-default.json",
        workspace_root,
        mode="on",
        default_backend="@g",
    )
    try:
        load_config(config_path)
    except AsterunError as exc:
        if exc.code == INVALID_CONFIG and ("default_backend" in exc.message or "@g" in exc.message):
            pytest.skip(
                "1b 的配置放开尚未合入：default_backend 写成 @g 时配置被拒绝（"
                + exc.message
                + "）"
            )
        raise
    with _open(workspace_root, monkeypatch, mode="on", default_backend="@g") as (app, _choice):
        submitted = _submit(app, backend=None, text="default-group")
        viewed = _view(app, submitted.ids["task_id"])
        assert submitted.data["task"]["backend"] == "fb"
        assert viewed.data["routing"]["mode"] == "on"
        assert viewed.data["routing"]["chosen"] == "fb"
