from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import stat

import pytest

from asterun.application import Application
from asterun.backup import backup_state, restore_state
from asterun.config import load_config
from asterun.config_migration import migrate_v1_to_v2
from asterun.control_protocol import digest
from asterun.errors import AsterunError
from asterun.plugins.builtins import build_registry
from asterun.plugins.management import LATCH_PREFIX, handle, plugin_enabled, registration_identity
from asterun.plugins.registry import installation_digest
from asterun.policy import DenyAll, Grant


@pytest.fixture
def managed(config_path):
    raw = migrate_v1_to_v2(config_path)["config"]
    config_path.write_text(json.dumps(raw))
    app = Application.from_paths(config_path, config_path.parent / "managed-state")
    try:
        yield app
    finally:
        app.close()


def error(code, callable, *args, **kwargs):
    with pytest.raises(AsterunError) as exc:
        callable(*args, **kwargs)
    assert exc.value.code == code


def registration_payload(app, tmp_path, external):
    output_dir = tmp_path / "candidate-output"
    output_dir.mkdir(exist_ok=True)
    return {"plugin_id": "example.external-fake", "registration": {
        "enabled": False, "manifest_path": str(external["manifest"]), "runner": external["runner"],
        "installation_path": str(external["wheel"]), "installation_sha256": external["sha256"],
        "runtime_path": str(external["manifest"].parent),
        "runtime_sha256": installation_digest(external["manifest"].parent),
    }, "output": str(output_dir / "candidate.json"), "backup": str(output_dir / "source.backup.json"),
        "expected_source_sha256": hashlib.sha256(app.config.source_path.read_bytes()).hexdigest()}


def assert_no_worker(monkeypatch):
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: pytest.fail("静态管理不得启动进程"))
    monkeypatch.setattr("asterun.plugins.registry.importlib.import_module", lambda *args, **kwargs: pytest.fail("静态管理不得加载插件factory"))


def test_list_inspect_and_setup_are_static_and_do_not_claim_authentication(managed, monkeypatch):
    assert_no_worker(monkeypatch)
    source = managed.config.source_path.read_bytes()
    listed = handle(managed, "plugin.list", {})
    fake = next(item for item in listed["plugins"] if item["plugin_id"] == "asterun.fake")
    assert fake["enabled"] and fake["configured"] and fake["installation_verified"]
    assert fake["authenticated"] == fake["callable"] == "not_tested"
    assert not fake["worker_started"] and not fake["native_visible_verified"]
    assert handle(managed, "plugin.inspect", {"plugin_id": "asterun.fake"})["plugin"] == fake
    ref = managed.config.backends["fake"].connection_ref
    setup = handle(managed, "connection.setup", {"connection_ref": ref})
    assert setup["auth_mode"] == "none" and setup["auth_mode_supported"]
    assert not setup["credential_reference_resolved"] and not setup["worker_started"]
    assert setup["setup_mode"] == "static_guidance"
    assert managed.config.source_path.read_bytes() == source


def test_setup_never_resolves_credential_reference_or_reveals_environment_value(managed, monkeypatch):
    ref = managed.config.backends["fake"].connection_ref
    account = managed.config.connections[ref]["provider_account_ref"]
    managed.config.provider_accounts[account]["credential_ref"] = "env:MANAGEMENT_TEST_SECRET"
    monkeypatch.setenv("MANAGEMENT_TEST_SECRET", "must-stay-out-of-management-result")
    assert_no_worker(monkeypatch)
    report = handle(managed, "connection.setup", {"connection_ref": ref})
    assert report["credential_reference_configured"]
    assert "must-stay-out-of-management-result" not in json.dumps(report)


def test_disable_persists_without_mutating_configuration_or_registry(managed):
    original = managed.config.source_path.read_bytes()
    config_sha = digest(managed.config.to_dict())
    registry = build_registry(managed.config)
    registration = registry.get("asterun.fake")
    disabled = handle(managed, "plugin.disable", {"plugin_id": "asterun.fake"})
    assert not disabled["new_execution_allowed"]
    assert disabled["inflight_observation_allowed"] and not disabled["remote_termination_verified"]
    assert not plugin_enabled(managed, registration)
    value = json.loads(managed.store._conn.execute("SELECT value FROM meta WHERE key=?", (LATCH_PREFIX + "asterun.fake",)).fetchone()[0])
    assert value["disabled"] is True and value["identity_sha256"] == digest(registration_identity(registration))
    assert value["principal_id"] == managed.principal.subject_id
    assert digest(managed.config.to_dict()) == config_sha
    assert managed.config.source_path.read_bytes() == original
    assert registration.enabled and registry.get("asterun.fake").enabled
    enabled = handle(managed, "plugin.enable", {"plugin_id": "asterun.fake"})
    assert enabled["new_execution_allowed"] and plugin_enabled(managed, registration)
    assert not managed.store._conn.execute("SELECT value FROM meta WHERE key=?", (LATCH_PREFIX + "asterun.fake",)).fetchone()


def test_reopen_preserves_disable_latch(managed):
    path, state = managed.config.source_path, managed.state_dir
    handle(managed, "plugin.disable", {"plugin_id": "asterun.fake"})
    managed.close()
    reopened = Application.from_paths(path, state)
    try:
        report = handle(reopened, "plugin.inspect", {"plugin_id": "asterun.fake"})["plugin"]
        assert not report["enabled"] and report["runtime_latch"]["reason"] == "operator_disabled"
    finally:
        reopened.close()


def test_backup_restore_keeps_runtime_disable_latch(managed, tmp_path):
    handle(managed, "plugin.disable", {"plugin_id": "asterun.fake"})
    archive = tmp_path / "plugin-state.tar.gz"
    backup_state(managed.state_dir, archive)
    destination = tmp_path / "restored-state"
    restore_state(archive, destination)
    restored = Application.from_paths(managed.config.source_path, destination)
    try:
        assert not plugin_enabled(restored, build_registry(restored.config).get("asterun.fake"))
    finally:
        restored.close()


@pytest.mark.parametrize("mutate", [
    lambda value: "malformed-json",
    lambda value: json.dumps({**value, "disabled": False}),
    lambda value: json.dumps({**value, "identity_sha256": "0" * 64}),
    lambda value: json.dumps({**value, "plugin_id": "unrelated"}),
    lambda value: json.dumps({**value, "unknown": True}),
    lambda value: json.dumps({**value, "updated_at": "not-a-time"}),
    lambda value: '{"disabled":false,"disabled":true}',
])
def test_tampered_latch_cannot_reenable_and_validation_failure_is_visible(managed, mutate):
    handle(managed, "plugin.disable", {"plugin_id": "asterun.fake"})
    key = LATCH_PREFIX + "asterun.fake"
    value = json.loads(managed.store._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()[0])
    managed.store._conn.execute("UPDATE meta SET value=? WHERE key=?", (mutate(value), key))
    report = handle(managed, "plugin.inspect", {"plugin_id": "asterun.fake"})["plugin"]
    assert not report["enabled"] and not report["runtime_latch"]["identity_matches"]
    assert report["runtime_latch"]["reason"] in {"invalid_latch", "installation_changed_while_disabled"}
    error("BINDING_MISMATCH", handle, managed, "plugin.enable", {"plugin_id": "asterun.fake"})


def test_changed_installation_identity_remains_disabled(managed):
    handle(managed, "plugin.disable", {"plugin_id": "asterun.fake"})
    registration = build_registry(managed.config).get("asterun.fake")
    changed = replace(registration, factory="asterun.backends.fake:DifferentFactory")
    assert not plugin_enabled(managed, changed)


def test_enable_cannot_override_config_disabled(managed):
    managed.config.plugins["asterun.fake"]["enabled"] = False
    handle(managed, "plugin.disable", {"plugin_id": "asterun.fake"})
    error("SCOPE_DENIED", handle, managed, "plugin.enable", {"plugin_id": "asterun.fake"})
    assert not plugin_enabled(managed, build_registry(managed.config).get("asterun.fake"))


@pytest.mark.parametrize("method,payload", [
    ("plugin.list", {}), ("plugin.inspect", {"plugin_id": "asterun.fake"}),
    ("plugin.enable", {"plugin_id": "asterun.fake"}), ("plugin.disable", {"plugin_id": "asterun.fake"}),
    ("connection.setup", {"connection_ref": "unused"}), ("usage.inspect", {}),
])
def test_management_requires_policy_and_full_resource_grant(managed, method, payload):
    managed.grant = Grant(resources=("demo",))
    error("SCOPE_DENIED", handle, managed, method, payload)
    managed.grant = Grant(expires_at="2000-01-01T00:00:00Z")
    error("AUTH_REQUIRED", handle, managed, method, payload)
    managed.grant = None
    managed.policy = DenyAll()
    error("SCOPE_DENIED", handle, managed, method, payload)


def test_metadata_and_latches_require_v2_sqlite(app, managed):
    error("CONFIG_REQUIRED", handle, app, "plugin.list", {})
    memory = Application(managed.config)
    try:
        error("CONFIG_REQUIRED", handle, memory, "plugin.list", {})
        assert not plugin_enabled(memory, build_registry(memory.config).get("asterun.fake"))
    finally:
        memory.close()


@pytest.mark.parametrize("method,payload", [
    ("plugin.list", {"unexpected": True}),
    ("plugin.inspect", {}),
    ("plugin.enable", {"plugin_id": "asterun.fake", "enabled": True}),
    ("connection.setup", {"connection_ref": "unused", "login": True}),
    ("usage.inspect", {"pool_ref": "unused", "refresh": True}),
    ("plugin.register", {"plugin_id": "new"}),
])
def test_management_payload_schema_rejects_implicit_execution_or_unknown_fields(managed, method, payload):
    error("INVALID_REQUEST", handle, managed, method, payload)


def test_register_writes_new_private_candidate_and_backup_without_hot_loading(managed, tmp_path, external_fake_installation, monkeypatch):
    payload = registration_payload(managed, tmp_path, external_fake_installation)
    source = managed.config.source_path.read_bytes()
    config_sha = digest(managed.config.to_dict())
    assert_no_worker(monkeypatch)
    report = handle(managed, "plugin.register", payload)
    assert report["restart_required"] and report["reload_required"] and not report["configuration_applied"]
    assert report["candidate_written"] and not report["worker_started"]
    assert managed.config.source_path.read_bytes() == Path(payload["backup"]).read_bytes() == source
    assert digest(managed.config.to_dict()) == config_sha
    candidate = load_config(Path(payload["output"]))
    assert candidate.revision == managed.config.revision + 1
    assert candidate.plugins["example.external-fake"]["enabled"] is False
    assert "example.external-fake" not in managed.config.plugins
    assert candidate.workspaces["demo"].root == managed.config.workspaces["demo"].root
    for key in ("output", "backup"):
        assert stat.S_IMODE(Path(payload[key]).stat().st_mode) == 0o600


@pytest.mark.parametrize("change", ["source", "same-output", "existing-output", "existing-backup", "stanza", "installation-pin", "wrong-id"])
def test_register_refuses_stale_or_invalid_candidate_before_writing(managed, tmp_path, external_fake_installation, change):
    payload = registration_payload(managed, tmp_path, external_fake_installation)
    if change == "source": payload["expected_source_sha256"] = "0" * 64
    elif change == "same-output": payload["output"] = str(managed.config.source_path)
    elif change == "existing-output": Path(payload["output"]).write_text("must keep output")
    elif change == "existing-backup": Path(payload["backup"]).write_text("must keep backup")
    elif change == "stanza": payload["registration"]["unknown"] = True
    elif change == "installation-pin": payload["registration"]["installation_sha256"] = "0" * 64
    else: payload["plugin_id"] = "wrong.plugin-id"
    source = managed.config.source_path.read_bytes()
    with pytest.raises(AsterunError):
        handle(managed, "plugin.register", payload)
    assert managed.config.source_path.read_bytes() == source
    if change not in {"same-output", "existing-output"}:
        assert not Path(payload["output"]).exists()
    if change != "existing-backup":
        assert not Path(payload["backup"]).exists()


def test_source_changes_after_backup_retains_only_exact_backup(managed, tmp_path, external_fake_installation, monkeypatch):
    from asterun import config_migration
    payload = registration_payload(managed, tmp_path, external_fake_installation)
    original = managed.config.source_path.read_bytes()
    create = config_migration._create_private
    def write_then_change(path, data):
        create(path, data)
        managed.config.source_path.write_bytes(original + b"\n")
    monkeypatch.setattr(config_migration, "_create_private", write_then_change)
    error("REVISION_CONFLICT", handle, managed, "plugin.register", payload)
    assert Path(payload["backup"]).read_bytes() == original
    assert not Path(payload["output"]).exists()


def test_usage_inspection_is_read_only_and_unknown_balance_stays_null(managed, monkeypatch):
    assert_no_worker(monkeypatch)
    before = managed.store._conn.total_changes
    report = handle(managed, "usage.inspect", {})
    assert report["pools"] and report["pools"][0]["remaining"] is None
    assert report["pools"][0]["ledger"]["used"] == 0
    assert not report["upstream_queried"] and not report["account_bill_hard_cap"]
    assert managed.store._conn.total_changes == before
