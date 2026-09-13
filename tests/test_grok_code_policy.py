from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
from threading import Event
import time

import pytest

from asterun.application import Application
from asterun.backends.fake import FakeBackend
from asterun.config import load_config
from asterun.config_migration import migrate_v1_to_v2
from asterun.errors import SCOPE_DENIED
from asterun.ids import RunId
from asterun.policy import Decision, Principal
from asterun.sqlite_store import SqliteStore
from tests.conftest import write_config


CODE_ACTIONS = ("workspace.read", "workspace.write", "process.execute")


class ActionPolicy:
    def __init__(self, denied=()):
        self.denied = set(denied)
        self.calls = []

    def authorize(self, principal, action, resource):
        self.calls.append((principal, action, resource))
        return Decision(action not in self.denied, SCOPE_DENIED, "测试策略拒绝该动作")


class LocalBackend(FakeBackend):
    """仅模拟结果与后台等待，不启动任何原生进程。"""
    def __init__(self, config):
        super().__init__(config)
        self.started = Event()
        self.release = Event()
        self.attempted = []

    def dispatch(self, task_id, run_id, script, text, *, cwd=None):
        self.attempted.append(text)
        self.started.set()
        if text == "hold":
            assert self.release.wait(5), "测试必须释放首项运行"
        return super().dispatch(task_id, run_id, script, text, cwd=cwd)


def config_file(isolated_env, workspace_root, version, profile):
    backend = {"kind": "grok", **({"execution_profile": profile} if profile else {})}
    path = write_config(isolated_env / "config.json", workspace_root,
                        extra_backends={"worker": backend})
    if version == 2:
        raw = migrate_v1_to_v2(path)["config"]
        connection = raw["connections"][raw["backends"]["worker"]["connection_ref"]]
        raw["plugins"]["xai.grok"]["enabled"] = True
        connection["enabled"] = True
        raw["billing_pools"][connection["billing_pool_refs"][0]].update(
            billing_mode="subscription", meter="turns", local_limit=100,
            estimate_per_action=1, max_concurrency=2, window_id="test",
            overage_verified=True, allow_unknown_subscription=True,
        )
        path.write_text(json.dumps(raw))
    return path


def open_app(path, policy, *, background=False):
    config = load_config(path)
    state = path.parent / "state"
    state.mkdir()
    backend = LocalBackend(config.backends["worker"])
    app = Application(config, store=SqliteStore(state / "asterun.sqlite"),
                      backends={"worker": backend, "fake": FakeBackend(config.backends["fake"])},
                      principal=Principal("test:actual-caller", "test"), policy=policy,
                      state_dir=state, background=background)
    return app, backend


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("denied_action", CODE_ACTIONS)
def test_grok_code_submit_requires_every_action_before_creating_intent(isolated_env, workspace_root, version, denied_action):
    policy = ActionPolicy([denied_action])
    path = config_file(isolated_env, workspace_root, version, "workspace-code-v1")
    app, backend = open_app(path, policy)
    with closing(app):
        result = app.handle("task.submit", {"workspace": "demo", "backend": "worker", "text": "implement"})
        assert not result.ok and result.error["code"] == SCOPE_DENIED
        assert app.store.list_task_ids() == []
        assert backend.attempted == []
        assert (app.principal, denied_action, "demo") in policy.calls
        assert app.store._conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0


@pytest.mark.parametrize("version", [1, 2])
def test_grok_code_checks_actual_principal_and_workspace_at_submit_and_dispatch(isolated_env, workspace_root, version):
    policy = ActionPolicy()
    path = config_file(isolated_env, workspace_root, version, "workspace-code-v1")
    app, backend = open_app(path, policy)
    with closing(app):
        result = app.handle("task.submit", {"workspace": "demo", "backend": "worker", "text": "implement"})
        assert result.ok, result.error
        assert backend.attempted == ["implement"]
        for action in CODE_ACTIONS:
            calls = [row for row in policy.calls if row[1] == action]
            assert len(calls) >= 2
            assert all(row == (app.principal, action, "demo") for row in calls)


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("profile", [None, "text-only-v1"])
def test_legacy_text_profile_keeps_existing_action_policy_contract(isolated_env, workspace_root, version, profile):
    policy = ActionPolicy(["workspace.write", "process.execute"])
    path = config_file(isolated_env, workspace_root, version, profile)
    app, backend = open_app(path, policy)
    with closing(app):
        result = app.handle("task.submit", {"workspace": "demo", "backend": "worker", "text": "text only"})
        assert result.ok, result.error
        assert backend.attempted == ["text only"]
        assert not any(action in policy.denied for _, action, _ in policy.calls)


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("revoked_action", CODE_ACTIONS)
def test_background_queue_rechecks_revoked_code_permissions_before_dispatch(isolated_env, workspace_root, version, revoked_action):
    policy = ActionPolicy()
    path = config_file(isolated_env, workspace_root, version, "workspace-code-v1")
    app, backend = open_app(path, policy, background=True)
    try:
        first = app.handle("task.submit", {"workspace": "demo", "backend": "worker", "text": "hold"})
        assert first.ok and backend.started.wait(1)
        queued = app.handle("task.submit", {"workspace": "demo", "backend": "worker", "text": "queued"})
        assert queued.ok and queued.data["queued"]
        policy.denied.add(revoked_action)
        backend.release.set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            app.poll()
            run = app.store.get_run(RunId(queued.ids["run_id"]))
            if run.status == "failed":
                break
            time.sleep(0.01)
        assert run.status == "failed" and run.error_code == SCOPE_DENIED
        assert run.terminated is True
        assert backend.attempted == ["hold"]
        operation = app.store.find_operation_for_run(run.id)
        assert operation.status == "failed"
    finally:
        backend.release.set()
        app.close()


def test_denied_code_read_does_not_read_prompt_file(isolated_env, workspace_root, monkeypatch):
    policy = ActionPolicy(["workspace.read"])
    path = config_file(isolated_env, workspace_root, 1, "workspace-code-v1")
    app, backend = open_app(path, policy)
    prompt = workspace_root / "prompt.txt"
    prompt.write_text("fixture input")
    original_read = Path.read_text

    def read(value, *args, **kwargs):
        assert value != prompt, "被拒绝的 workspace.read 不能先读取任务文件"
        return original_read(value, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    with closing(app):
        result = app.handle("task.submit", {"workspace": "demo", "backend": "worker", "path": "prompt.txt"})
        assert not result.ok and result.error["code"] == SCOPE_DENIED
        assert backend.attempted == []


def test_external_plugin_with_builtin_brand_keeps_its_declared_policy(isolated_env, workspace_root, external_fake_installation):
    path = config_file(isolated_env, workspace_root, 2, "workspace-code-v1")
    raw = json.loads(path.read_text())
    plugin = external_fake_installation
    manifest = json.loads(Path(plugin["manifest"]).read_text())
    manifest["plugin_id"] = "xai.grok"
    manifest_path = isolated_env / "external-grok.json"
    manifest_path.write_text(json.dumps(manifest))
    raw["plugins"]["xai.grok"] = {
        "enabled": True, "manifest_path": str(manifest_path), "runner": plugin["runner"],
        "installation_path": str(plugin["wheel"]), "installation_sha256": plugin["sha256"],
    }
    connection = raw["connections"][raw["backends"]["worker"]["connection_ref"]]
    connection["auth_mode"] = "none"
    raw["billing_pools"][connection["billing_pool_refs"][0]]["billing_mode"] = "local"
    path.write_text(json.dumps(raw))
    policy = ActionPolicy(["workspace.write", "process.execute"])
    app, backend = open_app(path, policy)
    with closing(app):
        assert backend.config.kind == "xai.grok"
        assert backend.config.execution_profile is None
        assert backend.config.options["execution_profile"] == "workspace-code-v1"
        result = app.handle("task.submit", {"workspace": "demo", "backend": "worker", "text": "external"})
        assert result.ok, result.error
        assert backend.attempted == ["external"]
        assert not any(action in policy.denied for _, action, _ in policy.calls)
