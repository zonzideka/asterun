from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from asterun.application import Application
from asterun.config_migration import migrate_v1_to_v2
from asterun.contracts import RunStatus
from asterun.mcp_server import handle_rpc
from asterun.plugins.registry import installation_digest
from tests.conftest import write_config
from tests.plugin_support import build_external_fake


def external_config(root, workspace, installation):
    source = write_config(root / "original.json", workspace)
    raw = migrate_v1_to_v2(source)["config"]
    connection = next(iter(raw["connections"].values()))
    connection["plugin_id"] = "example.external-fake"
    raw["plugins"] = {"example.external-fake": {
        "enabled": True, "manifest_path": str(installation["manifest"]),
        "runner": installation["runner"], "installation_path": str(installation["wheel"]),
        "installation_sha256": installation["sha256"],
        "runtime_path": str(installation["runtime_path"]),
        "runtime_sha256": installation_digest(installation["runtime_path"]),
    }}
    raw["connections"] = {"external": connection}
    raw["backends"] = {"external": {"connection_ref": "external"}}
    raw["default_backend"] = "external"
    path = root / "config.json"
    path.write_text(json.dumps(raw))
    return path


@pytest.fixture
def plugin_config(isolated_env, workspace_root, external_fake_installation):
    return external_config(isolated_env, workspace_root, external_fake_installation)


def invoke(app, mode="success", key="one"):
    return app.handle("capability.invoke", {"workspace": "demo", "backend": "external",
        "capability": "local.echo", "input": {"text": "typed output", "mode": mode}, "idempotency_key": key})


def test_real_external_wheel_through_task_and_typed_entry(plugin_config):
    app = Application.from_paths(plugin_config, plugin_config.parent / "state")
    try:
        first = app.handle("task.submit", {"workspace": "demo", "text": "external agent"})
        assert first.ok, first.error
        assert first.data["run"]["status"] == "succeeded"
        assert first.data["run"]["output"] == {"text": "external agent"}
        assert "capability" not in first.data["task"]
        typed = invoke(app)
        assert typed.ok, typed.error
        assert typed.data["task"]["text"] == ""
        assert typed.data["task"]["capability_input"] == {"text": "typed output", "mode": "success"}
        assert typed.data["run"]["output"] == {"text": "typed output"}
        assert invoke(app).ids == typed.ids
        assert app.backends["external"].dispatch_count == 2
        pool_ref = next(iter(app.config.billing_pools))
        assert len(app.admission.store.observations(pool_ref)) == 2
        assert app.admission.store.pool_totals(pool_ref)["committed"] == 2
        assert not app.handle("workflow.repair", {"task_id": typed.ids["task_id"]}).ok
    finally:
        app.close()


def test_worker_crash_persists_unknown_and_disabled_reconcile(plugin_config):
    state = plugin_config.parent / "state"
    app = Application.from_paths(plugin_config, state)
    result = invoke(app, "crash")
    assert result.ok, result.error
    assert result.data["run"]["status"] == "pending_reconcile"
    run_id, task_id = result.ids["run_id"], result.ids["task_id"]
    assert app.admission.store.binding_for_run(run_id)["status"] == "uncertain"
    disabled = app.handle("plugin.disable", {"plugin_id": "example.external-fake"})
    assert disabled.ok, disabled.error
    app.close()
    app = Application.from_paths(plugin_config, state)
    try:
        reject = invoke(app, key="new")
        assert not reject.ok and reject.error["code"] == "BACKEND_UNAVAILABLE"
        reconcile = app.handle("task.reconcile", {"task_id": task_id})
        assert reconcile.ok, reconcile.error
        cancel = app.handle("task.cancel", {"task_id": task_id})
        assert cancel.ok and not cancel.data["terminated"]
        assert app.admission.store.binding_for_run(run_id)["status"] == "uncertain"
        assert app.backends["external"].dispatch_count == 0
        workspace = app.config.workspaces["demo"].root
        assert (workspace / "accepted.txt").read_text() == "accepted\n"
    finally:
        app.close()


def test_background_typed_execution_and_mcp_management(plugin_config):
    app = Application.from_paths(plugin_config, plugin_config.parent / "state", background=True)
    try:
        listing = handle_rpc(app, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {item["name"] for item in listing["result"]["tools"]}
        assert {"plugin_list", "plugin_disable", "capability_invoke", "usage_inspect"} <= names
        result = invoke(app)
        assert result.ok and result.data["background"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            app.poll()
            run = app.store.get_run(app.store.get_task(app.store.list_task_ids()[0]).current_run_id)
            if run.status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.PENDING_RECONCILE}:
                break
            time.sleep(.01)
        assert run.status == RunStatus.SUCCEEDED
        assert run.output == {"text": "typed output"}
        rpc = handle_rpc(app, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "plugin_inspect", "arguments": {"plugin_id": "example.external-fake"}}})
        assert not rpc["result"]["isError"]
    finally:
        app.close()


def test_tools_only_wheel_uses_no_agent_role(isolated_env, workspace_root):
    install_root = isolated_env / "tools-install"
    install_root.mkdir()
    installation = build_external_fake(install_root, tools_only=True)
    path = external_config(isolated_env, workspace_root, installation)
    app = Application.from_paths(path, isolated_env / "state")
    try:
        result = invoke(app)
        assert result.ok and result.data["run"]["status"] == "succeeded", result.to_dict()
        assert app.backends["external"].registration.manifest.to_dict()["roles"] == ["capability"]
        rejected = app.handle("task.submit", {"workspace": "demo", "text": "agent not installed"})
        assert not rejected.ok
        assert app.backends["external"].dispatch_count == 1
    finally:
        app.close()


def test_plugin_cli_offline_uses_same_management_and_typed_path(plugin_config):
    import os
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "HOME", "ASTERUN_HOME", "LANG"}}
    request = plugin_config.parent / "typed.json"
    request.write_text(json.dumps({"workspace": "demo", "backend": "external", "capability": "local.echo",
                                   "input": {"text": "cli typed"}, "idempotency_key": "cli"}))
    command = [sys.executable, "-m", "asterun", "--config", str(plugin_config), "--state-dir", str(plugin_config.parent / "cli-state")]
    listed = subprocess.run([*command, "plugin-list"], env=env, capture_output=True, text=True, timeout=10)
    assert listed.returncode == 0, listed.stderr
    result = subprocess.run([*command, "capability-invoke", "--request", str(request)], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["data"]["run"]["output"] == {"text": "cli typed"}


def test_plugin_observations_are_untrusted_deduplicated_and_restored(plugin_config):
    from asterun.backup import backup_state, restore_state
    app = Application.from_paths(plugin_config, plugin_config.parent / "state")
    result = invoke(app)
    assert result.ok, result.error
    run = app.store.get_run(app.store.get_task(app.store.list_task_ids()[0]).current_run_id)
    event_result = {"status": "succeeded", "terminated": True,
                    "plugin_events": [{"provider_event_id": "evt-1", "summary": "provider observation", "provider_sequence": 7}]}
    # 模拟重复observe回执；直接测试核心对worker净化后的结果处理边界。
    app._apply_backend_result(run, event_result, "backend.external")
    app._apply_backend_result(run, deepcopy(event_result), "backend.external")
    events = [event for event in app.store.list_events(run.id) if event.source == "plugin.observation"]
    assert len(events) == 1 and events[0].payload["trust"] == "untrusted_provider_observation"
    archive = plugin_config.parent / "archive.tar.gz"
    backup_state(app.state_dir, archive)
    app.close()
    restored_dir = plugin_config.parent / "restored"
    restore_state(archive, restored_dir)
    restored = Application.from_paths(plugin_config, restored_dir)
    try:
        restored_run = restored.store.get_run(run.id)
        restored._apply_backend_result(restored_run, deepcopy(event_result), "backend.external")
        events = [event for event in restored.store.list_events(run.id) if event.source == "plugin.observation"]
        assert len(events) == 1
        assert restored_run.output == {"text": "typed output"}
    finally:
        restored.close()
