from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from asterun.application import Application
from asterun.backends.codex import CodexAppServerTransport, CodexBackend, CodexPromptResult
from asterun.backends.codex_policy import normal_thread_params
from asterun.config import BackendConfig, load_config
from asterun.config_migration import migrate_v1_to_v2
from asterun.connections.admission import input_digest
from asterun.control_protocol import digest
from asterun.errors import AsterunError, INVALID_CONFIG, UNKNOWN_FIELD
from asterun.ids import RunId, TaskId
from tests.conftest import write_config


def configured_path(isolated_env, workspace_root, version, options, kind="codex"):
    path = write_config(
        isolated_env / "config.json", workspace_root,
        extra_backends={"target": {"kind": kind}},
    )
    raw = (migrate_v1_to_v2(path)["config"] if version == 2
           else json.loads(path.read_text()))
    target = (raw["connections"][raw["backends"]["target"]["connection_ref"]]["options"]
              if version == 2 else raw["backends"]["target"])
    target.update(options)
    path.write_text(json.dumps(raw))
    return path


@pytest.mark.parametrize("version", [1, 2])
def test_omitted_codex_approval_policy_stays_out_of_the_config_projection(isolated_env, workspace_root, version):
    config = load_config(configured_path(isolated_env, workspace_root, version, {}))
    backend = config.get_backend("target")
    assert backend.approval_policy is None
    assert "approval_policy" not in backend.to_dict()
    assert normal_thread_params(backend) == {"approvalPolicy": "untrusted", "sandbox": "workspace-write"}


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("policy", ["untrusted", "on-failure", "on-request"])
def test_explicit_codex_approval_policy_is_preserved(isolated_env, workspace_root, version, policy):
    config = load_config(configured_path(isolated_env, workspace_root, version, {"approval_policy": policy}))
    backend = config.get_backend("target")
    assert backend.approval_policy == policy
    assert backend.to_dict()["approval_policy"] == policy
    if version == 2:
        assert backend.options["approval_policy"] == policy
    assert normal_thread_params(backend) == {"approvalPolicy": policy, "sandbox": "workspace-write"}


@pytest.mark.parametrize("version", [1, 2])
def test_pinning_untrusted_changes_the_revision_digest(isolated_env, workspace_root, version):
    digests = []
    for options in ({}, {"approval_policy": "untrusted"}, {"approval_policy": "on-request"}):
        digests.append(digest(load_config(configured_path(isolated_env, workspace_root, version, options)).to_dict()))
    assert len(set(digests)) == 3


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("value", [
    "never", "danger-full-access", "granular", "yolo", "unlessTrusted", "on_request",
    "On-Request", "on-request ", " untrusted", "", None, True, False, 1,
    ["on-request"],
    {"sandbox_approval": False, "rules": False, "mcp_elicitations": False},
])
def test_codex_approval_policy_rejects_unsafe_or_unknown_values(isolated_env, workspace_root, version, value):
    path = configured_path(isolated_env, workspace_root, version, {"approval_policy": value})
    with pytest.raises(AsterunError) as captured:
        load_config(path)
    assert captured.value.code == INVALID_CONFIG
    assert "workspace-write" in captured.value.message
    assert "never" in captured.value.message
    assert "danger-full-access" in captured.value.message


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("field", ["sandbox", "sandbox_mode"])
def test_codex_sandbox_mode_cannot_be_configured(isolated_env, workspace_root, version, field):
    path = configured_path(isolated_env, workspace_root, version, {field: "danger-full-access"})
    with pytest.raises(AsterunError) as captured:
        load_config(path)
    assert captured.value.code == UNKNOWN_FIELD


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("kind", ["fake", "grok", "claude", "antigravity"])
def test_approval_policy_does_not_expand_other_backends(isolated_env, workspace_root, version, kind):
    path = configured_path(isolated_env, workspace_root, version, {"approval_policy": "on-request"}, kind)
    with pytest.raises(AsterunError) as captured:
        load_config(path)
    assert captured.value.code == (INVALID_CONFIG if version == 1 else UNKNOWN_FIELD)


def test_migration_preserves_approval_policy_without_enabling(isolated_env, workspace_root):
    path = configured_path(
        isolated_env, workspace_root, 1,
        {"bin": "/test-only/codex", "desktop_projects": True, "approval_policy": "on-request"},
    )
    original = path.read_bytes()
    report = migrate_v1_to_v2(path)
    assert path.read_bytes() == original
    proposed = report["config"]
    connection = proposed["connections"][proposed["backends"]["target"]["connection_ref"]]
    assert connection["enabled"] is False
    assert connection["options"]["approval_policy"] == "on-request"
    assert connection["options"]["desktop_projects"] is True
    migrated_path = isolated_env / "migrated.json"
    migrated_path.write_text(json.dumps(proposed))
    migrated = load_config(migrated_path).get_backend("target")
    assert migrated.approval_policy == "on-request"
    assert normal_thread_params(migrated)["approvalPolicy"] == "on-request"
    assert normal_thread_params(migrated)["sandbox"] == "workspace-write"


def test_v2_plan_binds_approval_policy_and_rejects_a_changed_config(isolated_env, workspace_root, monkeypatch):
    path = configured_path(isolated_env, workspace_root, 2, {"approval_policy": "on-request"})
    raw = json.loads(path.read_text())
    raw["plugins"]["openai.codex"]["enabled"] = True
    connection = raw["connections"][raw["backends"]["target"]["connection_ref"]]
    connection["enabled"] = True
    raw["billing_pools"][connection["billing_pool_refs"][0]].update(
        billing_mode="subscription", meter="requests", local_limit=1, max_concurrency=1,
        window_id="offline-window", estimate_per_action=1,
        overage_verified=True, allow_unknown_subscription=True,
    )
    path.write_text(json.dumps(raw))
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("配置与计划验证不得启动后端"))
    app = Application.from_paths(path, isolated_env / "state")
    try:
        backend = app.config.get_backend("target")
        assert app.backends["target"].config.approval_policy == "on-request"
        plan = app.admission.prepare("demo", "target", input_digest("offline"))
        assert plan["binding"]["connection"]["options"]["approval_policy"] == "on-request"
        assert plan["binding"]["configuration_sha256"] == digest(app.config.to_dict())
        app.config.connections[backend.connection_ref]["options"]["approval_policy"] = "never"
        with pytest.raises(AsterunError):
            app.admission.assert_plan(plan)
        assert app.backends["target"].dispatch_count == 0
    finally:
        app.close()


def test_mutated_policy_is_not_sent(isolated_env):
    config = BackendConfig(name="codex", kind="codex", enabled=True, approval_policy="never")
    with pytest.raises(AsterunError) as captured:
        normal_thread_params(config)
    assert captured.value.code == INVALID_CONFIG
    assert "workspace-write" in captured.value.message


def _native_config(isolated_env, workspace_root, monkeypatch, policy):
    binary = isolated_env / "codex-stub"
    binary.write_text(f"#!{sys.executable}\n" + (Path(__file__).parent / "fixtures/codex_server.py").read_text())
    binary.chmod(0o700)
    monkeypatch.setenv("ASTERUN_RUN_CODEX", "1")
    backend = {"kind": "codex", "bin": str(binary)}
    if policy is not None:
        backend["approval_policy"] = policy
    return write_config(isolated_env / "native-config.json", workspace_root, extra_backends={"codex": backend})


def _wait(app, task_id):
    deadline = time.monotonic() + 5
    last = None
    while time.monotonic() < deadline:
        app.poll()
        last = app.handle("task.get", {"task_id": task_id})
        assert last.ok, last.error
        if last.data["run"]["status"] == "succeeded":
            return last.data
        time.sleep(0.02)
    pytest.fail(f"未等到成功：{last}")


def _calls(workspace_root):
    return [json.loads(line) for line in (workspace_root / "native-calls.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("policy,expected", [
    (None, "untrusted"),
    ("untrusted", "untrusted"),
    ("on-request", "on-request"),
    ("on-failure", "on-failure"),
])
def test_normal_codex_runs_send_the_configured_policy_and_keep_the_sandbox(
        isolated_env, workspace_root, monkeypatch, policy, expected):
    path = _native_config(isolated_env, workspace_root, monkeypatch, policy)
    app = Application.from_paths(path, isolated_env / "state", background=True)
    try:
        submitted = app.handle("task.submit", {"workspace": "demo", "backend": "codex", "text": "finish"})
        assert submitted.ok, submitted.error
        _wait(app, submitted.ids["task_id"])
    finally:
        app.close()
    calls = _calls(workspace_root)
    start = next(call for call in calls if call.get("method") == "thread/start")
    turn = next(call for call in calls if call.get("method") == "turn/start")
    assert start["params"]["approvalPolicy"] == expected
    assert start["params"]["sandbox"] == "workspace-write"
    assert "approvalPolicy" not in turn["params"]
    assert "sandbox" not in turn["params"]
    assert "danger-full-access" not in json.dumps(calls)


def test_on_request_is_sent_again_when_the_native_thread_resumes(isolated_env, workspace_root, monkeypatch):
    path = _native_config(isolated_env, workspace_root, monkeypatch, "on-request")
    app = Application.from_paths(path, isolated_env / "state", background=True)
    try:
        first = app.handle("task.submit", {"workspace": "demo", "backend": "codex", "text": "finish"})
        assert first.ok, first.error
        _wait(app, first.ids["task_id"])
        second = app.handle("task.submit", {
            "workspace": "demo", "backend": "codex", "text": "finish",
            "conversation_id": first.ids["conversation_id"],
        })
        assert second.ok, second.error
        done = _wait(app, second.ids["task_id"])
        assert done["run"]["native"]["thread_id"] == "native-thread-1"
    finally:
        app.close()
    calls = _calls(workspace_root)
    threaded = [call for call in calls if call.get("method") in {"thread/start", "thread/resume"}]
    assert [call["method"] for call in threaded] == ["thread/start", "thread/resume"]
    assert all(call["params"]["approvalPolicy"] == "on-request" for call in threaded)
    assert all(call["params"]["sandbox"] == "workspace-write" for call in threaded)
    assert all("approvalPolicy" not in call["params"] and "sandbox" not in call["params"]
               for call in calls if call.get("method") == "turn/start")


@pytest.mark.parametrize("policy,expected", [(None, "untrusted"), ("on-request", "on-request")])
def test_sync_dispatch_uses_the_same_thread_permissions(tmp_path, monkeypatch, policy, expected):
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("ASTERUN_RUN_CODEX", "1")
    seen = {}

    def run_text_prompt(self, **kwargs):
        seen.update(kwargs)
        return CodexPromptResult(status="completed", thread_id="thread-1", turn_id="turn-1", message="ok")

    monkeypatch.setattr(CodexAppServerTransport, "spawn", lambda self, cwd: None)
    monkeypatch.setattr(CodexAppServerTransport, "initialize", lambda self: {})
    monkeypatch.setattr(CodexAppServerTransport, "close", lambda self: None)
    monkeypatch.setattr(CodexAppServerTransport, "run_text_prompt", run_text_prompt)
    backend = CodexBackend(BackendConfig(
        name="codex", kind="codex", enabled=True, bin=str(binary), approval_policy=policy))
    result = backend.dispatch(TaskId("task-1"), RunId("run-1"), "", "hello", cwd=workspace)
    assert result["status"] == "succeeded"
    assert seen["thread_params"] == {"approvalPolicy": expected, "sandbox": "workspace-write"}
