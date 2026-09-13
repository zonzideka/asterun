from copy import deepcopy
from contextlib import closing
import json
from pathlib import Path

import pytest

from asterun.application import Application
from asterun.config import load_config
from asterun.config_migration import migrate_v1_to_v2
from asterun.contracts import RunStatus
from asterun.errors import AsterunError
from asterun.policy import DenyAll
from tests.conftest import write_config


@pytest.fixture
def v2_config(isolated_env, workspace_root):
    original = write_config(isolated_env / "v1.json", workspace_root)
    raw = migrate_v1_to_v2(original)["config"]
    path = isolated_env / "v2.json"
    path.write_text(json.dumps(raw))
    return path


def open_app(path, suffix="state"):
    return Application.from_paths(path, path.parent / suffix)


def test_v2_intent_plan_reservation_and_idempotency(v2_config):
    with_app = open_app(v2_config)
    try:
        request = {"workspace": "demo", "text": "offline", "idempotency_key": "same"}
        first = with_app.handle("task.submit", request)
        assert first.ok, first.error
        binding = with_app.admission.store.binding_for_run(first.ids["run_id"])
        assert binding is not None
        assert binding["status"] == "committed"
        assert all(item["actual"] is None for item in binding["reservations"])
        second = with_app.handle("task.submit", request)
        assert second.ok and second.ids == first.ids
        assert with_app.backends["fake"].dispatch_count == 1
        assert with_app.store._conn.execute("SELECT COUNT(*) FROM plugin_execution_bindings").fetchone()[0] == 1
        with_app.policy = DenyAll()
        denied = with_app.handle("task.submit", request)
        assert not denied.ok
    finally:
        with_app.close()


def test_shared_pool_rejection_rolls_back_entire_intent(v2_config):
    raw = json.loads(v2_config.read_text())
    pool = next(iter(raw["billing_pools"].values()))
    pool["local_limit"] = 1
    connection_ref = raw["backends"]["fake"]["connection_ref"]
    raw["connections"]["other"] = deepcopy(raw["connections"][connection_ref])
    raw["backends"]["other"] = {"connection_ref": "other"}
    v2_config.write_text(json.dumps(raw))
    app = open_app(v2_config)
    try:
        first = app.handle("task.submit", {"workspace": "demo", "text": "first"})
        assert first.ok, first.error
        denied = app.handle("task.submit", {"workspace": "demo", "backend": "other", "text": "second"})
        assert not denied.ok
        assert denied.error["code"] == "BUDGET_EXHAUSTED"
        assert len(app.store.list_task_ids()) == 1
        assert not app.store._conn.in_transaction
        assert app.backends["other"].dispatch_count == 0
    finally:
        app.close()


@pytest.mark.parametrize("change", ["revision", "workspace", "provider", "model", "pool", "credential", "runtime", "plugin"])
def test_prepared_binding_changes_fail_closed(v2_config, change, tmp_path):
    from asterun.connections.admission import input_digest
    app = open_app(v2_config)
    try:
        plan = app.admission.prepare("demo", "fake", input_digest("one"))
        conf = app.config
        conn = conf.connections[conf.backends["fake"].connection_ref]
        if change == "revision": conf.revision += 1
        elif change == "workspace":
            other = tmp_path / "other"; other.mkdir(); conf.workspaces["demo"].root = other
        elif change == "provider": conf.provider_accounts[conn["provider_account_ref"]]["provider"] = "different"
        elif change == "model": conn["options"]["model"] = "different"
        elif change == "pool": conf.billing_pools[conn["billing_pool_refs"][0]]["local_limit"] += 1
        elif change == "credential": conf.provider_accounts[conn["provider_account_ref"]]["credential_revision"] = 2
        elif change == "runtime": conn["runtime_ref"] = "different"
        elif change == "plugin": conf.plugins[conn["plugin_id"]]["enabled"] = False
        with pytest.raises(AsterunError): app.admission.assert_plan(plan)
        assert app.backends["fake"].dispatch_count == 0
    finally:
        app.close()


def test_intent_callback_exception_rolls_back_and_connection_remains_usable(v2_config):
    app = open_app(v2_config)
    original = app.store.intent_hook
    def fail_after_admission(*args):
        original(*args)
        raise AsterunError("BUDGET_EXHAUSTED", "fault after reservation")
    app.store.intent_hook = fail_after_admission
    try:
        failed = app.handle("task.submit", {"workspace": "demo", "text": "fault"})
        assert not failed.ok
        assert app.store.list_task_ids() == []
        assert not app.store._conn.in_transaction
        assert app.store._conn.execute("SELECT COUNT(*) FROM plugin_execution_bindings").fetchone()[0] == 0
        assert app.backends["fake"].dispatch_count == 0
        app.store.intent_hook = original
        assert app.handle("task.submit", {"workspace": "demo", "text": "after rollback"}).ok
    finally:
        app.close()


def test_unknown_keeps_reservation_across_restart_without_redispatch(v2_config):
    app = open_app(v2_config)
    def lost(*a, **k):
        raise RuntimeError("accepted then lost")
    app.backends["fake"].dispatch = lost
    result = app.handle("task.submit", {"workspace": "demo", "text": "once", "idempotency_key": "once"})
    assert not result.ok and result.error["code"] == "REMOTE_STATE_UNKNOWN"
    task_id = app.store.list_task_ids()[0]
    run_id = app.store.get_task(task_id).current_run_id
    assert app.admission.store.binding_for_run(run_id.value)["status"] == "uncertain"
    app.close()
    app = open_app(v2_config)
    try:
        assert app.admission.store.binding_for_run(run_id.value)["status"] == "uncertain"
        app.handle("task.reconcile", {"task_id": task_id.value})
        assert app.backends["fake"].dispatch_count == 0
    finally:
        app.close()


def test_credential_change_cannot_reuse_existing_conversation(v2_config):
    app = open_app(v2_config)
    try:
        result = app.handle("task.submit", {"workspace": "demo", "text": "first"})
        assert result.ok, result.error
        connection = app.config.connections[app.config.backends["fake"].connection_ref]
        app.config.provider_accounts[connection["provider_account_ref"]]["credential_revision"] = 2
        rejected = app.handle("task.submit", {"workspace": "demo", "text": "second",
                                               "conversation_id": result.ids["conversation_id"]})
        assert not rejected.ok and rejected.error["code"] == "BINDING_MISMATCH"
        assert app.backends["fake"].dispatch_count == 1
    finally:
        app.close()


def test_control_admission_and_legacy_intent_share_one_pool_reservation(v2_config):
    from tests.test_control_runtime import create_task, start_command
    app = open_app(v2_config)
    try:
        runtime = app.control
        task = create_task(runtime)
        request = start_command(runtime, task)
        result = runtime.command(request)
        actions = runtime.store.list("Action", runtime.namespace)
        assert len(actions) == 1
        binding = app.admission.store.binding_for_control(actions[0]["id"])
        assert binding and binding["legacy_run_id"] is None
        runtime.advance()
        binding = app.admission.store.binding_for_control(actions[0]["id"])
        assert binding["legacy_run_id"] is not None
        assert app.backends["fake"].dispatch_count == 1
        assert runtime.command(request)["operation_id"] == result["operation_id"]
        runtime.advance()
        assert app.backends["fake"].dispatch_count == 1
        assert app.store._conn.execute("SELECT COUNT(*) FROM plugin_execution_bindings").fetchone()[0] == 1
    finally:
        app.close()


def test_schema_five_upgrade_snapshot_preserves_legacy_rows(config_path, isolated_env):
    import sqlite3
    from asterun.sqlite_store import SqliteStore
    from asterun.plugin_store import TABLE_COLUMNS
    app = open_app(config_path)
    result = app.handle("task.submit", {"workspace": "demo", "text": "before migration"})
    original = app.store.get_task(app.store.list_task_ids()[0]).to_dict()
    database = app.store.path
    app.close()
    # 重建历史schema5布局；使用原有core/control表，不放任何新插件行。
    with closing(sqlite3.connect(database)) as conn, conn:
        for table in TABLE_COLUMNS:
            conn.execute("DROP TABLE " + table)
        conn.execute("UPDATE meta SET value='5' WHERE key='schema_version'")
    upgraded = SqliteStore(database)
    try:
        assert upgraded.migration_backup is not None
        with closing(sqlite3.connect(upgraded.migration_backup)) as backup:
            assert backup.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "5"
        assert upgraded.get_task(upgraded.list_task_ids()[0]).to_dict() == original
        assert upgraded.get_applied_revision() == 1
    finally:
        upgraded.close()


def test_v2_backup_restore_keeps_execution_and_session_lock(v2_config, isolated_env):
    from asterun.backup import backup_state, restore_state
    app = open_app(v2_config)
    result = app.handle("task.submit", {"workspace": "demo", "text": "backup"})
    assert result.ok, result.error
    before = app.admission.store.binding_for_run(result.ids["run_id"])
    archive = isolated_env / "backup.tar.gz"
    backup_state(app.state_dir, archive)
    app.close()
    target = isolated_env / "restored"
    restore_state(archive, target)
    restored = Application.from_paths(v2_config, target)
    try:
        assert restored.admission.store.binding_for_run(result.ids["run_id"]) == before
        assert restored.backends["fake"].dispatch_count == 0
        assert restored.admission.store.get_session_lock(result.ids["conversation_id"]) is not None
    finally:
        restored.close()
