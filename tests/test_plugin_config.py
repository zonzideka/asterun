from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat

import pytest

from asterun.config import load_config
from asterun.config_migration import migrate_v1_to_v2, write_v2_migration
from asterun.errors import AsterunError


def write(path, raw):
    path.write_text(json.dumps(raw, ensure_ascii=False))
    return path


@pytest.fixture
def v2_raw(config_path):
    return migrate_v1_to_v2(config_path)["config"]


def first_connection(raw):
    return next(iter(raw["connections"].values()))


def test_v1_projection_remains_byte_semantically_unchanged(config_path):
    config = load_config(config_path)
    assert config.to_dict() == {
        "schema_version": 1, "revision": 1, "source_path": str(config_path),
        "workspaces": {"demo": {"alias": "demo", "root": str(config.workspaces["demo"].root), "allow_non_git": True, "is_git": False}},
        "backends": {"fake": {"name": "fake", "kind": "fake", "enabled": True, "bin": None, "executable": True, "is_real_backend": False}},
        "default_backend": "fake", "workflow": {"preset": "none", "require_review": False, "review_backend": None, "approved_substitute": None, "max_repairs": 2},
        "scheduler": {"max_queue": 32, "per_backend_concurrency": 1, "max_runs_per_task": 4},
        "entries": {"cli": {"enabled": True}, "mcp": {"enabled": True}},
        "approval_policy": {"mode": "manual", "rules": []},
    }
    assert config.plugins == config.connections == config.provider_accounts == config.billing_pools == {}


def test_v2_alias_resolves_through_connection_and_plugin(config_path, v2_raw):
    config = load_config(write(config_path, v2_raw))
    backend = config.get_backend("fake")
    assert backend.kind == "fake"
    assert backend.plugin_id == "asterun.fake"
    assert backend.connection_ref in config.connections
    assert backend.enabled is True
    assert config.to_dict()["plugins"] == {"asterun.fake": {"enabled": True}}
    assert config.to_dict()["backends"]["fake"]["connection_ref"] == backend.connection_ref


@pytest.mark.parametrize("disabled", ["plugin", "connection"])
def test_either_disabled_layer_blocks_backend(config_path, v2_raw, disabled):
    target = v2_raw["plugins"]["asterun.fake"] if disabled == "plugin" else first_connection(v2_raw)
    target["enabled"] = False
    assert load_config(write(config_path, v2_raw)).get_backend("fake").enabled is False


def test_tool_only_configuration_needs_no_agent_or_workspace(config_path, v2_raw):
    v2_raw.update(backends={}, workspaces={}, default_backend=None)
    config = load_config(write(config_path, v2_raw))
    assert config.backends == config.workspaces == {}
    assert config.connections


@pytest.mark.parametrize("mutate", [
    lambda raw: raw.update(unrecognized=True),
    lambda raw: raw.update(plugins=[]),
    lambda raw: raw["plugins"]["asterun.fake"].update(unknown=True),
    lambda raw: raw["plugins"]["asterun.fake"].update(enabled=1),
    lambda raw: first_connection(raw).update(unknown=True),
    lambda raw: first_connection(raw).update(plugin_id="not.registered"),
    lambda raw: first_connection(raw).update(enabled="yes"),
    lambda raw: first_connection(raw).update(provider_account_ref="unknown"),
    lambda raw: first_connection(raw).update(auth_mode="api_key"),
    lambda raw: first_connection(raw).update(runtime_ref=""),
    lambda raw: first_connection(raw).update(billing_pool_refs=["missing"]),
    lambda raw: first_connection(raw).update(billing_pool_refs=first_connection(raw)["billing_pool_refs"] * 2),
    lambda raw: first_connection(raw).update(options={"home": "/unused"}),
    lambda raw: first_connection(raw).update(options={"bin": ["bad"]}),
    lambda raw: first_connection(raw).update(options={"api_key": "must-not-be-an-option"}),
    lambda raw: next(iter(raw["provider_accounts"].values())).update(unknown=True),
    lambda raw: next(iter(raw["provider_accounts"].values())).update(credential_ref=123),
    lambda raw: next(iter(raw["billing_pools"].values())).update(unknown=True),
    lambda raw: next(iter(raw["billing_pools"].values())).update(paid_overage_allowed=True),
    lambda raw: next(iter(raw["billing_pools"].values())).update(billing_mode="metered"),
    lambda raw: next(iter(raw["billing_pools"].values())).update(remaining=-1),
    lambda raw: next(iter(raw["billing_pools"].values())).update(remaining=0.5),
    lambda raw: raw["backends"]["fake"].update(kind="fake"),
    lambda raw: raw["backends"]["fake"].update(connection_ref="missing"),
    lambda raw: raw["routing"].update(automatic_paid_fallback=True),
    lambda raw: raw["routing"].update(automatic_account_switch=True),
    lambda raw: raw["routing"].update(default_order=["missing"]),
    lambda raw: raw["routing"].update(rules=[{"if": "anything"}]),
])
def test_v2_rejects_unknown_fields_invalid_types_and_references(config_path, v2_raw, mutate):
    mutate(v2_raw)
    with pytest.raises(AsterunError):
        load_config(write(config_path, v2_raw))


def test_v2_external_unknown_id_registers_without_loading(config_path, v2_raw, external_fake_installation, monkeypatch):
    plugin = external_fake_installation
    connection = first_connection(v2_raw)
    v2_raw["plugins"] = {"example.external-fake": {
        "enabled": False, "manifest_path": str(plugin["manifest"]), "runner": plugin["runner"],
        "installation_path": str(plugin["wheel"]), "installation_sha256": plugin["sha256"],
    }}
    connection.update(plugin_id="example.external-fake", options={"custom": "plugin-specific"})
    monkeypatch.setattr("asterun.plugins.registry.importlib.import_module", lambda *a, **k: pytest.fail("配置校验不得加载外部模块"))
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("配置校验不得启动 worker"))
    config = load_config(write(config_path, v2_raw))
    assert config.backends["fake"].kind == "example.external-fake"
    assert config.backends["fake"].options == {"custom": "plugin-specific"}
    assert not config.backends["fake"].enabled


def test_v2_explicit_external_manifest_can_replace_builtin_id(config_path, v2_raw, external_fake_installation, tmp_path):
    plugin = external_fake_installation
    manifest = json.loads(Path(plugin["manifest"]).read_text())
    manifest["plugin_id"] = "google.antigravity-cli"
    manifest_path = write(tmp_path / "manifest.json", manifest)
    v2_raw["plugins"] = {"google.antigravity-cli": {
        "enabled": False, "manifest_path": str(manifest_path), "runner": plugin["runner"],
        "installation_path": str(plugin["wheel"]), "installation_sha256": plugin["sha256"],
    }}
    first_connection(v2_raw).update(plugin_id="google.antigravity-cli", options={"external_version_pin": "fake-test"})
    config = load_config(write(config_path, v2_raw))
    assert config.backends["fake"].kind == "google.antigravity-cli"


def test_migration_dry_run_preserves_original_and_disables_unverified_accounts(config_path, monkeypatch):
    raw = json.loads(config_path.read_text())
    raw["backends"]["my-agy"] = {"kind": "antigravity", "enabled": True, "home": "/test-only/agy-home", "model": "test-only-model", "bin": "/test-only/agy"}
    raw["backends"]["my-codex"] = {"kind": "codex", "enabled": False, "bin": "/test-only/codex"}
    raw["default_backend"] = "my-agy"
    write(config_path, raw)
    original = config_path.read_bytes()
    monkeypatch.setenv("ASTERUN_ACCOUNT_REF", "core-user-must-not-be-provider")
    monkeypatch.setenv("ASTERUN_RUNTIME_REF", "live-host-must-not-be-copied")
    report = migrate_v1_to_v2(config_path)
    assert report["dry_run"] and report["source_unchanged"]
    assert config_path.read_bytes() == original
    assert report["source_sha256"] == hashlib.sha256(original).hexdigest()
    proposed = report["config"]
    assert proposed["revision"] == raw["revision"] + 1
    assert proposed["default_backend"] == "my-agy"
    assert set(proposed["backends"]) == set(raw["backends"])
    agy = proposed["connections"][proposed["backends"]["my-agy"]["connection_ref"]]
    assert not agy["enabled"]
    assert agy["options"] == {"home": "/test-only/agy-home", "model": "test-only-model", "bin": "/test-only/agy"}
    agy_pool = proposed["billing_pools"][agy["billing_pool_refs"][0]]
    assert agy_pool["billing_mode"] == "unknown"
    assert "estimate_per_action" not in agy_pool
    assert "observed_at" not in agy_pool
    assert "local_limit" not in agy_pool
    assert report["original_backend_enabled"]["my-agy"] is True
    assert all(account["credential_ref"] is None for account in proposed["provider_accounts"].values())
    assert "core-user-must-not-be-provider" not in json.dumps(proposed)
    assert "live-host-must-not-be-copied" not in json.dumps(proposed)


def test_explicit_migration_writes_new_private_config_and_exact_backup(config_path, tmp_path):
    original = config_path.read_bytes()
    report = migrate_v1_to_v2(config_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    destination, backup = elsewhere / "v2.json", elsewhere / "v1.backup.json"
    result = write_v2_migration(config_path, destination, backup, expected_source_sha256=report["source_sha256"])
    assert result["dry_run"] is False
    assert config_path.read_bytes() == backup.read_bytes() == original
    assert stat.S_IMODE(destination.stat().st_mode) == stat.S_IMODE(backup.stat().st_mode) == 0o600
    migrated = load_config(destination)
    assert migrated.schema_version == 2
    assert migrated.workspaces["demo"].root == load_config(config_path).workspaces["demo"].root
    assert migrated.get_backend("fake").enabled


@pytest.mark.parametrize("which", ["destination", "backup", "source", "sha"])
def test_migration_refuses_overwrite_or_stale_source(config_path, tmp_path, which):
    report = migrate_v1_to_v2(config_path)
    destination, backup = tmp_path / "v2.json", tmp_path / "backup.json"
    digest = report["source_sha256"]
    if which == "destination":
        destination.write_text("concurrent destination")
    elif which == "backup":
        backup.write_text("concurrent backup")
    elif which == "source":
        destination = config_path
    else:
        digest = "0" * 64
    original = config_path.read_bytes()
    with pytest.raises(AsterunError):
        write_v2_migration(config_path, destination, backup, expected_source_sha256=digest)
    assert config_path.read_bytes() == original
    if which == "destination":
        assert destination.read_text() == "concurrent destination"
    if which == "backup":
        assert backup.read_text() == "concurrent backup"


def test_source_changes_after_backup_retains_backup_without_v2_write(config_path, tmp_path, monkeypatch):
    import asterun.config_migration as migration
    report = migrate_v1_to_v2(config_path)
    original = config_path.read_bytes()
    destination, backup = tmp_path / "v2.json", tmp_path / "backup.json"
    original_create = migration._create_private
    def change_after_backup(path, data):
        original_create(path, data)
        config_path.write_bytes(original + b"\n")
    monkeypatch.setattr(migration, "_create_private", change_after_backup)
    with pytest.raises(AsterunError, match="备份后"):
        write_v2_migration(config_path, destination, backup, expected_source_sha256=report["source_sha256"])
    assert backup.read_bytes() == original
    assert not destination.exists()
    assert config_path.read_bytes() == original + b"\n"


def test_migration_rejects_second_migration(config_path, v2_raw):
    write(config_path, v2_raw)
    with pytest.raises(AsterunError, match="只接受 v1"):
        migrate_v1_to_v2(config_path)


def first_pool(raw):
    return next(iter(raw["billing_pools"].values()))


@pytest.mark.parametrize("field,value", [
    ("meter", "requests"), ("meter", "micro_usd"), ("local_limit", 100), ("max_concurrency", 2),
    ("window_id", "2026-09-local"), ("estimate_per_action", 0), ("remaining", None),
    ("allow_unknown_subscription", True), ("overage_verified", False),
    ("risk_acceptance", "local_estimate"), ("risk_acceptance", None),
    ("observed_at", "2026-09-11T00:00:00Z"), ("valid_until", "2026-09-12T00:00:00+00:00"),
])
def test_pool_fields_keep_units_uncertainty_and_explicit_policy(config_path, v2_raw, field, value):
    first_pool(v2_raw)[field] = value
    config = load_config(write(config_path, v2_raw))
    assert next(iter(config.billing_pools.values()))[field] == value


@pytest.mark.parametrize("field,value", [
    ("meter", "usd"), ("meter", ""), ("meter", 1),
    ("local_limit", 0), ("local_limit", -1), ("local_limit", 1.5), ("local_limit", True),
    ("max_concurrency", 0), ("max_concurrency", False), ("max_concurrency", "2"),
    ("window_id", ""), ("window_id", None),
    ("estimate_per_action", -1), ("estimate_per_action", 0.01), ("estimate_per_action", True),
    ("allow_unknown_subscription", "true"), ("overage_verified", 1),
    ("risk_acceptance", "hard_account_cap"), ("risk_acceptance", True),
    ("observed_at", "2026-09-11T00:00:00+08:00"), ("valid_until", "2026-09-11"),
    ("observed_at", "2026-02-30T00:00:00Z"), ("valid_until", "2026-09-11T00:00:00"),
])
def test_pool_fields_reject_floats_unbounded_values_and_invalid_utc(config_path, v2_raw, field, value):
    first_pool(v2_raw)[field] = value
    with pytest.raises(AsterunError):
        load_config(write(config_path, v2_raw))


def test_usage_window_requires_expiry_after_observation(config_path, v2_raw):
    first_pool(v2_raw).update(observed_at="2026-09-11T00:00:00Z", valid_until="2026-09-11T00:00:00Z")
    with pytest.raises(AsterunError, match="晚于"):
        load_config(write(config_path, v2_raw))


def test_static_config_does_not_invent_missing_budget_or_freshness(config_path, v2_raw):
    pool = first_pool(v2_raw)
    pool.clear()
    pool.update(billing_mode="local", remaining=None, paid_overage_allowed=False)
    config = load_config(write(config_path, v2_raw))
    assert next(iter(config.billing_pools.values())) == pool
    assert "estimate_per_action" not in pool
    assert "observed_at" not in pool
    assert pool["remaining"] is None


def test_subscription_unknown_policy_is_data_not_automatic_authorization(config_path, v2_raw):
    v2_raw["plugins"] = {"openai.codex": {"enabled": False}}
    first_connection(v2_raw).update(plugin_id="openai.codex", auth_mode="native_account", enabled=False)
    first_pool(v2_raw).update(billing_mode="subscription", remaining=None,
                              allow_unknown_subscription=True, overage_verified=False)
    config = load_config(write(config_path, v2_raw))
    assert not config.backends["fake"].enabled
    assert next(iter(config.billing_pools.values()))["remaining"] is None
    assert next(iter(config.billing_pools.values()))["overage_verified"] is False


@pytest.mark.parametrize("ref", [None, "credential-ref:configure-later", "env:ASTERUN_PLUGIN_KEY", "file:/not-read/private-key", "native-home:/not-read/agy-home"])
def test_credential_references_are_static_and_never_read(config_path, v2_raw, ref, monkeypatch):
    account = next(iter(v2_raw["provider_accounts"].values()))
    account.update(credential_ref=ref, credential_revision=2, revoked=True)
    first_connection(v2_raw).update(credential_ref=ref, upstream_version="1.2.0")
    original_open = Path.open
    def guarded_open(path, *args, **kwargs):
        if str(path).startswith("/not-read/"):
            pytest.fail("静态配置不得解析凭据内容或原生账户")
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", guarded_open)
    config = load_config(write(config_path, v2_raw))
    assert next(iter(config.provider_accounts.values()))["credential_ref"] == ref
    assert next(iter(config.provider_accounts.values()))["revoked"] is True
    assert next(iter(config.connections.values()))["upstream_version"] == "1.2.0"


@pytest.mark.parametrize("field,value", [
    ("credential_revision", 0), ("credential_revision", True), ("credential_revision", 1.1),
    ("revoked", "false"), ("revoked", 0),
    ("credential_ref", "literal-credential-value"), ("credential_ref", "env:KEY=literal"),
    ("credential_ref", "file:relative/path"), ("credential_ref", "native-home:relative"),
    ("credential_ref", "credential-ref:"),
])
def test_provider_credentials_reject_values_and_invalid_revisions(config_path, v2_raw, field, value):
    next(iter(v2_raw["provider_accounts"].values()))[field] = value
    with pytest.raises(AsterunError):
        load_config(write(config_path, v2_raw))


@pytest.mark.parametrize("upstream", ["", 123, {}, True])
def test_upstream_version_rejects_invalid_pin(config_path, v2_raw, upstream):
    first_connection(v2_raw)["upstream_version"] = upstream
    with pytest.raises(AsterunError):
        load_config(write(config_path, v2_raw))


@pytest.mark.parametrize("options", [
    {"env": {"GEMINI_API_KEY": "value"}}, {"nested": {"api_key": "value"}},
    {"authorization": "value"}, {"APIKEY": "value"}, {"GEMINI_API_KEY": "value"},
    {"nested": [{"SERVICE_TOKEN": "value"}]},
])
def test_external_plugin_options_cannot_smuggle_env_or_credentials(config_path, v2_raw, external_fake_installation, options):
    plugin = external_fake_installation
    v2_raw["plugins"] = {"example.external-fake": {
        "enabled": False, "manifest_path": str(plugin["manifest"]), "runner": plugin["runner"],
        "installation_path": str(plugin["wheel"]), "installation_sha256": plugin["sha256"],
    }}
    first_connection(v2_raw).update(plugin_id="example.external-fake", options=options)
    with pytest.raises(AsterunError, match="明文凭据字段"):
        load_config(write(config_path, v2_raw))


def test_fake_migration_has_bounded_local_budget(config_path):
    result = migrate_v1_to_v2(config_path)
    pool = first_pool(result["config"])
    assert pool["meter"] == "requests"
    assert pool["local_limit"] == 100
    assert pool["max_concurrency"] == 1
    assert pool["window_id"] == "local"
    assert pool["estimate_per_action"] == 1
    assert pool["remaining"] is None
    assert pool["allow_unknown_subscription"] is False
    assert pool["risk_acceptance"] is None


def test_distributed_fake_example_is_complete_without_user_state(tmp_path):
    source = Path(__file__).parents[1] / "examples/plugins/fake-v2.json"
    target = tmp_path / "fake-v2.json"
    target.write_bytes(source.read_bytes())
    config = load_config(target)
    assert config.get_backend("fake").enabled
    assert config.workspaces["demo"].root == tmp_path.resolve()
    assert config.billing_pools["fake_pool"]["local_limit"] == 100
    assert config.provider_accounts["fake_account"]["credential_ref"] is None
