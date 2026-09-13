"""M1 收口验收：真实离线 worker 观察、计费拒绝与重启后的权限绑定。"""
from copy import deepcopy
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from asterun.application import Application
from asterun.backup import backup_state, restore_state
from asterun.contracts import ApprovalState, RunStatus
from asterun.errors import AsterunError
from asterun.ids import ApprovalId, RunId
from asterun.policy import Grant, Principal
from tests.test_plugin_admission import v2_config
from tests.test_plugin_execution import invoke, plugin_config


@pytest.mark.parametrize("method", ["task.get", "task.events", "session.resume"])
def test_v2_public_result_is_private_to_the_authenticated_principal(plugin_config, method):
    app = Application.from_paths(plugin_config, plugin_config.parent / "state", principal=Principal("owner", "test"))
    try:
        result = invoke(app)
        assert result.ok, result.error
        app.principal = Principal("another-authorized-workspace-user", "test")
        payload = ({"conversation_id": result.ids["conversation_id"]} if method == "session.resume"
                   else {"task_id": result.ids["task_id"]})
        private = app.handle(method, payload)
        assert not private.ok
        assert private.error["code"] in {"NOT_FOUND", "SCOPE_DENIED"}
        assert app.backends["external"].dispatch_count == 1
    finally:
        app.close()


@pytest.mark.parametrize("method", ["approval.assess", "approval.respond"])
def test_v2_approval_cannot_be_read_or_forwarded_by_another_principal(v2_config, method):
    from tests.test_approval_batch import pending
    app = Application.from_paths(v2_config, v2_config.parent / "state", principal=Principal("owner", "test"))
    try:
        payload = pending(app)
        if method == "approval.respond":
            payload["decision"] = "approve"
        app.principal = Principal("another-authorized-workspace-user", "test")
        result = app.handle(method, payload)
        assert not result.ok
        assert result.error["code"] in {"NOT_FOUND", "SCOPE_DENIED"}
        assert app.store.get_approval(ApprovalId(payload["approval_id"])).state == ApprovalState.PENDING
        assert not any(call.method == "respond_approval" for call in app.backends["fake"].calls)
    finally:
        app.close()


def test_provider_event_gaps_and_reordering_keep_core_sequences_across_restore(plugin_config):
    app = Application.from_paths(plugin_config, plugin_config.parent / "state")
    try:
        result = invoke(app)
        assert result.ok, result.error
        run = app.store.get_run(RunId(result.ids["run_id"]))
        before = app.store.list_events(run.id)
        rows = [{"provider_event_id": f"provider-{sequence}", "provider_sequence": sequence,
                 "summary": f"observation {sequence}"} for sequence in (10, 12, 11)]
        reply = {"status": "succeeded", "terminated": True, "plugin_events": rows}
        app._apply_backend_result(run, reply, "backend.external")
        app._apply_backend_result(run, deepcopy(reply), "backend.external")
        events = app.store.list_events(run.id)
        added = events[len(before):]
        assert [event.payload["provider_sequence_note"] for event in added] == [
            "first_observation", "gap_observed", "out_of_order"]
        assert [event.seq for event in events] == list(range(1, len(events) + 1))
        saved = [event.to_dict() for event in events]
        archive = plugin_config.parent / "event-backup.tar.gz"
        backup_state(app.state_dir, archive)
    finally:
        app.close()
    target = plugin_config.parent / "restored-events"
    restore_state(archive, target)
    reopened = Application.from_paths(plugin_config, target)
    try:
        run = reopened.store.get_run(run.id)
        reopened._apply_backend_result(run, deepcopy(reply), "backend.external")
        assert [event.to_dict() for event in reopened.store.list_events(run.id)] == saved
        reopened._apply_backend_result(run, {"status": "succeeded", "terminated": True,
            "plugin_events": [{"provider_event_id": "provider-13", "provider_sequence": 13,
                               "summary": "next observation"}]}, "backend.external")
        events = reopened.store.list_events(run.id)
        assert events[-1].seq == saved[-1]["seq"] + 1
        assert events[-1].payload["provider_sequence_note"] == "contiguous"
        assert all(event.payload["trust"] == "untrusted_provider_observation"
                   for event in events if event.source == "plugin.observation")
        assert reopened.backends["external"].dispatch_count == 0
    finally:
        reopened.close()


@pytest.mark.parametrize("missing", ["stale_quota", "unknown_billing", "unknown_metered_estimate"])
def test_public_submit_rejects_uncertain_billing_without_any_intent(plugin_config, missing):
    raw = json.loads(plugin_config.read_text())
    registration = raw["plugins"]["example.external-fake"]
    # 同一无网络 fake worker 声明三种计费类型；此测试不会派发它。
    manifest = json.loads(Path(registration["manifest_path"]).read_text())
    manifest["billing_modes"] = ["local", "subscription", "metered"]
    metadata = plugin_config.parent / "billing-manifest.json"
    metadata.write_text(json.dumps(manifest))
    registration["manifest_path"] = str(metadata)
    pool = next(iter(raw["billing_pools"].values()))
    if missing == "stale_quota":
        pool.update(billing_mode="subscription", remaining=10,
                    observed_at="2000-01-01T00:00:00Z", valid_until="2000-01-02T00:00:00Z")
    elif missing == "unknown_billing":
        pool.update(billing_mode="unknown")
    else:
        pool.update(billing_mode="metered", meter="micro_usd", risk_acceptance="local_estimate")
        pool.pop("estimate_per_action", None)
    plugin_config.write_text(json.dumps(raw))
    app = Application.from_paths(plugin_config, plugin_config.parent / "state")
    try:
        result = app.handle("task.submit", {"workspace": "demo", "backend": "external",
            "text": "must not be delivered", "idempotency_key": "billing-rejected"})
        assert not result.ok
        assert result.error["code"] == ("BINDING_MISMATCH" if missing == "unknown_billing" else "BUDGET_EXHAUSTED")
        assert result.error["message"]
        assert app.backends["external"].dispatch_count == 0
        assert not (app.config.workspaces["demo"].root / "accepted.txt").exists()
        for table in ("tasks", "runs", "operations", "sessions", "plugin_plans",
                      "plugin_execution_bindings", "plugin_pool_reservations"):
            assert app.store._conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] == 0
        assert not app.store._conn.in_transaction
    finally:
        app.close()


@pytest.mark.parametrize("change", ["revoked", "credential_revision", "provider_account", "grant_expired"])
def test_restart_cannot_continue_unknown_run_under_changed_authority(v2_config, change):
    state = v2_config.parent / "state"
    app = Application.from_paths(v2_config, state)
    try:
        result = app.handle("task.submit", {"workspace": "demo", "text": "accepted once",
            "script": "lost_reply", "idempotency_key": "original"})
        assert result.ok and app.backends["fake"].dispatch_count == 1, result.error
        binding = app.admission.store.binding_for_run(result.ids["run_id"])
        old_plan = app.admission.store.get_plan(binding["plan_id"], app.admission.principal, app.admission.namespace)
        assert binding["status"] == "uncertain"
    finally:
        app.close()
    raw = json.loads(v2_config.read_text())
    connection = raw["connections"][raw["backends"]["fake"]["connection_ref"]]
    provider = raw["provider_accounts"][connection["provider_account_ref"]]
    if change == "revoked":
        provider["revoked"] = True
    elif change == "credential_revision":
        provider["credential_revision"] = provider.get("credential_revision", 1) + 1
    elif change == "provider_account":
        raw["provider_accounts"]["replacement"] = deepcopy(provider)
        connection["provider_account_ref"] = "replacement"
    v2_config.write_text(json.dumps(raw))
    grant = Grant(expires_at="2000-01-01T00:00:00Z") if change == "grant_expired" else None
    reopened = Application.from_paths(v2_config, state, grant=grant)
    try:
        assert reopened.backends["fake"].dispatch_count == 0
        with pytest.raises(AsterunError):
            reopened.admission.assert_plan(old_plan)
        continued = reopened.handle("task.submit", {"workspace": "demo", "text": "must not continue",
            "conversation_id": result.ids["conversation_id"], "idempotency_key": "changed-authority"})
        assert not continued.ok
        reconciled = reopened.handle("task.reconcile", {"task_id": result.ids["task_id"]})
        assert not reconciled.ok
        assert reopened.backends["fake"].dispatch_count == 0
        assert len(reopened.store.list_task_ids()) == 1
        assert reopened.store.get_run(RunId(result.ids["run_id"])).status == RunStatus.PENDING_RECONCILE
        after = reopened.admission.store.binding_for_run(result.ids["run_id"])
        assert after["status"] == "uncertain" and after["reservations"] == binding["reservations"]
        assert not reopened.store._conn.in_transaction
    finally:
        reopened.close()


@pytest.mark.parametrize("change", ["model", "plugin_version"])
def test_restart_cannot_rebind_unknown_worker_to_new_model_or_version(plugin_config, change):
    state = plugin_config.parent / "state"
    app = Application.from_paths(plugin_config, state)
    try:
        result = invoke(app, "crash")
        assert result.ok and result.data["run"]["status"] == "pending_reconcile"
        binding = app.admission.store.binding_for_run(result.ids["run_id"])
        old_plan = app.admission.store.get_plan(binding["plan_id"], app.admission.principal, app.admission.namespace)
        workspace = app.config.workspaces["demo"].root
        assert (workspace / "accepted.txt").read_text() == "accepted\n"
    finally:
        app.close()
    raw = json.loads(plugin_config.read_text())
    if change == "model":
        raw["connections"]["external"]["options"]["model"] = "replacement-model"
    else:
        registration = raw["plugins"]["example.external-fake"]
        manifest = json.loads(Path(registration["manifest_path"]).read_text())
        manifest["plugin_version"] = "2.0.0"
        metadata = plugin_config.parent / "replacement-manifest.json"
        metadata.write_text(json.dumps(manifest))
        registration["manifest_path"] = str(metadata)
    plugin_config.write_text(json.dumps(raw))
    reopened = Application.from_paths(plugin_config, state)
    try:
        with pytest.raises(AsterunError) as changed:
            reopened.admission.assert_plan(old_plan)
        assert changed.value.code == "BINDING_MISMATCH"
        reconciled = reopened.handle("task.reconcile", {"task_id": result.ids["task_id"]})
        assert not reconciled.ok and reconciled.error["code"] == "BINDING_MISMATCH"
        assert reopened.backends["external"].dispatch_count == 0
        assert (workspace / "accepted.txt").read_text() == "accepted\n"
        assert reopened.admission.store.binding_for_run(result.ids["run_id"])["reservations"] == binding["reservations"]
    finally:
        reopened.close()


def test_restart_rejects_changed_plugin_installation_without_releasing_unknown(plugin_config):
    raw = json.loads(plugin_config.read_text())
    registration = raw["plugins"]["example.external-fake"]
    # 独立复制发行物，避免改变其它测试共用的已安装 fake wheel。
    artifact = plugin_config.parent / "pinned-worker.whl"
    shutil.copyfile(registration["installation_path"], artifact)
    registration["installation_path"] = str(artifact)
    plugin_config.write_text(json.dumps(raw))
    state = plugin_config.parent / "state"
    app = Application.from_paths(plugin_config, state)
    try:
        result = invoke(app, "crash")
        assert result.ok and result.data["run"]["status"] == "pending_reconcile"
        binding = app.admission.store.binding_for_run(result.ids["run_id"])
        workspace = app.config.workspaces["demo"].root
        assert (workspace / "accepted.txt").read_text() == "accepted\n"
    finally:
        app.close()
    artifact.write_bytes(artifact.read_bytes() + b"changed-after-admission")
    with pytest.raises(AsterunError):
        Application.from_paths(plugin_config, state)
    assert (workspace / "accepted.txt").read_text() == "accepted\n"
    with closing(sqlite3.connect(state / "asterun.sqlite")) as conn, conn:
        saved = json.loads(conn.execute("SELECT payload FROM plugin_execution_bindings WHERE id=?", (binding["id"],)).fetchone()[0])
        assert saved["plan_id"] == binding["plan_id"] and saved["legacy_run_id"] == result.ids["run_id"]
        assert conn.execute("SELECT COUNT(*) FROM plugin_execution_bindings").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM plugin_pool_reservations WHERE status='uncertain'").fetchone()[0] == 1
