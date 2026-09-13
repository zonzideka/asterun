"""v2 生命周期边界：公开核心入口、临时状态与固定离线协议替身。"""
from copy import deepcopy
import json

import pytest

from asterun.application import Application
from asterun.config_migration import migrate_v1_to_v2
from asterun.contracts import RunStatus
from asterun.grok_profile import prepare_home
from asterun.ids import RunId, TaskId
from asterun.policy import Grant
from tests.conftest import write_config
from tests.test_control_native import action_for, submit as control_submit
from tests.test_grok_environment import write_failure_grok_stub
from tests.test_plugin_admission import open_app, v2_config


def connection(app):
    return app.config.connections[app.config.backends["fake"].connection_ref]


def test_repair_cannot_replace_credentials_of_the_existing_conversation(v2_config):
    app = open_app(v2_config)
    try:
        first = app.handle("task.submit", {"workspace": "demo", "text": "first"})
        assert first.ok, first.error
        before = app.store.get_task(TaskId(first.ids["task_id"])).to_dict()
        app.config.provider_accounts[connection(app)["provider_account_ref"]]["credential_revision"] = 2
        result = app.handle("workflow.repair", {
            "task_id": first.ids["task_id"], "idempotency_key": "repair-after-credential-change",
        })
        assert not result.ok and result.error["code"] == "BINDING_MISMATCH"
        assert app.backends["fake"].dispatch_count == 1
        assert app.store.get_task(TaskId(first.ids["task_id"])).to_dict() == before
        assert app.store._conn.execute("SELECT COUNT(*) FROM plugin_execution_bindings").fetchone()[0] == 1
        assert not app.store._conn.in_transaction
    finally:
        app.close()


@pytest.mark.parametrize("method", ["task.cancel", "task.reconcile"])
@pytest.mark.parametrize("change", ["credential_revision", "provider_account"])
def test_existing_action_rejects_changed_identity_before_backend_call(v2_config, method, change):
    app = open_app(v2_config)
    try:
        first = app.handle("task.submit", {
            "workspace": "demo", "text": "pending", "script": "lost_reply",
        })
        assert first.ok, first.error
        run = app.store.get_run(RunId(first.ids["run_id"]))
        run.native = {"provider_job_ref": "offline-job"}
        app.store.save_run(run)
        observed = []
        app.backends["fake"].reconcile_native = lambda *args: observed.append(args)
        conn = connection(app)
        if change == "credential_revision":
            app.config.provider_accounts[conn["provider_account_ref"]]["credential_revision"] = 2
        else:
            app.config.provider_accounts["different-account"] = deepcopy(app.config.provider_accounts[conn["provider_account_ref"]])
            conn["provider_account_ref"] = "different-account"
        result = app.handle(method, {"task_id": first.ids["task_id"]})
        assert not result.ok and result.error["code"] == "BINDING_MISMATCH"
        assert observed == []
        assert not any(call.method == "request_cancel" for call in app.backends["fake"].calls)
        saved = app.store.get_run(run.id)
        assert saved.status == RunStatus.PENDING_RECONCILE and not saved.cancel_requested
        assert app.admission.store.binding_for_run(run.id.value)["status"] == "uncertain"
    finally:
        app.close()


@pytest.mark.parametrize("method", ["task.cancel", "task.reconcile"])
def test_disable_keeps_authorized_existing_actions_and_uncertain_reservation(v2_config, method):
    app = open_app(v2_config)
    try:
        first = app.handle("task.submit", {
            "workspace": "demo", "text": "pending", "script": "lost_reply",
        })
        assert first.ok, first.error
        conn = connection(app)
        conn["enabled"] = False
        app.config.plugins[conn["plugin_id"]]["enabled"] = False
        result = app.handle(method, {"task_id": first.ids["task_id"]})
        assert result.ok, result.error
        assert app.admission.store.binding_for_run(first.ids["run_id"])["status"] == "uncertain"
        refused = app.handle("task.submit", {"workspace": "demo", "text": "new"})
        assert not refused.ok and refused.error["code"] == "BACKEND_UNAVAILABLE"
        assert app.backends["fake"].dispatch_count == 1
    finally:
        app.close()


@pytest.mark.parametrize("method", ["task.submit", "workflow.repair"])
def test_expired_grant_is_checked_before_idempotent_result(v2_config, method):
    app = open_app(v2_config)
    try:
        payload = {"workspace": "demo", "text": "first", "idempotency_key": "submit"}
        first = app.handle("task.submit", payload)
        assert first.ok, first.error
        if method == "workflow.repair":
            payload = {"task_id": first.ids["task_id"], "idempotency_key": "repair"}
            assert app.handle(method, payload).ok
        before = app.backends["fake"].dispatch_count
        app.grant = Grant(expires_at="2000-01-01T00:00:00Z")
        rejected = app.handle(method, payload)
        assert not rejected.ok and rejected.error["code"] == "AUTH_REQUIRED"
        assert app.backends["fake"].dispatch_count == before
    finally:
        app.close()


@pytest.mark.parametrize("failure", ["initialize", "missing-session-id"])
@pytest.mark.parametrize("entry", ["legacy", "control"])
def test_trusted_failure_before_prompt_releases_v2_reservation(isolated_env, workspace_root, monkeypatch, failure, entry):
    native_home = isolated_env / "grok-home"
    prepare_home(native_home)
    capture = isolated_env / "methods.log"
    binary = write_failure_grok_stub(isolated_env / "grok-stub", capture, failure)
    # 此开关仅用于显式 fixture binary；独立 HOME/PATH 中无真实账户或 CLI。
    monkeypatch.setenv("ASTERUN_RUN_GROK", "1")
    original = write_config(isolated_env / "v1.json", workspace_root,
        extra_backends={"grok": {"kind": "grok", "bin": str(binary), "home": str(native_home)}},
        extra={"default_backend": "grok"})
    raw = migrate_v1_to_v2(original)["config"]
    raw["plugins"]["xai.grok"]["enabled"] = True
    raw["default_backend"] = "grok"
    conn = raw["connections"][raw["backends"]["grok"]["connection_ref"]]
    conn["enabled"] = True
    raw["billing_pools"][conn["billing_pool_refs"][0]].update(
        billing_mode="subscription", meter="requests", local_limit=1, max_concurrency=1,
        window_id="offline-window", estimate_per_action=1,
        overage_verified=True, allow_unknown_subscription=True,
    )
    path = isolated_env / "v2.json"
    path.write_text(json.dumps(raw))
    app = Application.from_paths(path, isolated_env / "state")
    try:
        if entry == "control":
            _, run_id = control_submit(app, "never send a prompt")
            app.poll()
            action = action_for(app, run_id)
            assert app.control.store.get("Run", run_id, app.control.namespace)["status"] == "FAILED"
            binding = app.admission.store.binding_for_control(action["id"])
            reservation = app.control.store.get("Reservation", action["extensions"]["asterun.reservation_id"], app.control.namespace)
            assert reservation["actual"] == [{"meter": "turns", "amount": 0, "billing_pool_ref": None}]
        else:
            result = app.handle("task.submit", {"workspace": "demo", "text": "never send a prompt"})
            assert result.ok, result.error
            assert result.data["run"]["status"] == "failed"
            binding = app.admission.store.binding_for_run(result.ids["run_id"])
        assert capture.read_text().splitlines().count("spawn") == 1
        assert "session/prompt" not in capture.read_text().splitlines()
        assert binding["status"] == "released"
        assert all(item["actual"] == 0 for item in binding["reservations"])
        assert not app.store._conn.in_transaction
    finally:
        app.close()
