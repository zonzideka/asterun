"""quota_preference.mode / allow_paid_on_explicit 与 M1 换号开关。缺省键不进入 to_dict。"""

from __future__ import annotations

from types import SimpleNamespace

from asterun.config import load_config
from asterun.quota.settings import quota_preference_mode
import json
from copy import deepcopy
from pathlib import Path
import pytest
from asterun.config_migration import migrate_v1_to_v2
from asterun.errors import INVALID_CONFIG, AsterunError


def _account(name: str, provider: str) -> dict:
    return {
        "provider": provider,
        "credential_ref": "native-home:/tmp/" + name,
        "credential_revision": 1,
        "revoked": False,
    }


def _pool(mode: str = "subscription", overage: bool = False) -> dict:
    return {
        "billing_mode": mode,
        "paid_overage_allowed": overage,
        "remaining": None,
        "overage_verified": False,
        "allow_unknown_subscription": False,
        "risk_acceptance": None,
    }


def _connection(plugin: str, account: str, pool: str, options: dict) -> dict:
    return {
        "plugin_id": plugin,
        "enabled": True,
        "provider_account_ref": account,
        "runtime_ref": "runtime:configure",
        "auth_mode": "native_account",
        "billing_pool_refs": [pool],
        "options": options,
    }


def _backend(alias: str, plugin: str, provider: str, options: dict, account: str | None = None, pool: str | None = None):
    account_name = account or alias + "-acct"
    pool_name = pool or alias + "-pool"
    return account_name, pool_name, _connection(plugin, account_name, pool_name, options)


def base_config(workspace: Path) -> dict:
    specs = {
        "grok-a": ("xai.grok", "xai", {"home": "/tmp/asterun-pr5-grok-a", "model": "grok-code-fast-1"}),
        "grok-b": ("xai.grok", "xai", {"home": "/tmp/asterun-pr5-grok-b", "model": "grok-code-fast-1"}),
        "codex-a": ("openai.codex", "openai", {}),
        "codex-b": ("openai.codex", "openai", {}),
        "agy-opus": ("google.antigravity-cli", "google", {"home": "/tmp/asterun-pr5-agy-opus", "model": "claude-opus-4-6"}),
        "agy-gemini": ("google.antigravity-cli", "google", {"home": "/tmp/asterun-pr5-agy-gemini", "model": "gemini-2.5-pro"}),
    }
    plugins, accounts, pools, connections, backends = {}, {}, {}, {}, {}
    for alias, (plugin, provider, options) in specs.items():
        plugins[plugin] = {"enabled": True}
        account, pool, connection = _backend(alias, plugin, provider, options)
        accounts[account] = _account(account, provider)
        pools[pool] = _pool()
        connections["conn-" + alias] = connection
        backends[alias] = {"connection_ref": "conn-" + alias}
    return {
        "schema_version": 2,
        "revision": 1,
        "workspaces": {"demo": {"root": str(workspace), "allow_non_git": True}},
        "default_backend": "grok-a",
        "plugins": plugins,
        "provider_accounts": accounts,
        "billing_pools": pools,
        "connections": connections,
        "backends": backends,
        "routing": {
            "automatic_paid_fallback": False,
            "automatic_account_switch": False,
            "default_order": [],
            "rules": [],
            "candidate_groups": {
                "implementer": {"role": "implementation", "members": ["grok-a", "grok-b"], "primary": "grok-a"},
                "reviewer": {"role": "review", "members": ["codex-a", "codex-b"], "primary": "codex-a"},
                "planning": {"role": "planning", "members": ["agy-opus", "agy-gemini"], "primary": "agy-opus"},
            },
            "quota_preference": {"enabled": False},
        },
    }


def _write(path: Path, raw: dict) -> Path:
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def _invalid(path: Path, raw: dict) -> str:
    _write(path, raw)
    with pytest.raises(AsterunError) as caught:
        load_config(path)
    assert caught.value.code == INVALID_CONFIG
    return caught.value.message


def test_rejects_bad_mode_paid_flag_and_enabled_false_conflict(config_path, workspace_root):
    raw = base_config(workspace_root)
    raw["routing"]["quota_preference"] = {"mode": "auto"}
    assert "mode" in _invalid(config_path, raw)

    conflict = base_config(workspace_root)
    conflict["routing"]["quota_preference"] = {"enabled": False, "mode": "dry_run"}
    assert "enabled=false" in _invalid(config_path, conflict)

    conflict_on = base_config(workspace_root)
    conflict_on["routing"]["quota_preference"] = {"enabled": False, "mode": "on"}
    assert "enabled=false" in _invalid(config_path, conflict_on)

    paid_flag = base_config(workspace_root)
    paid_flag["routing"]["quota_preference"] = {"allow_paid_on_explicit": "yes"}
    assert "allow_paid_on_explicit" in _invalid(config_path, paid_flag)


def test_paid_fallback_stays_rejected_when_switch_would_otherwise_be_allowed(config_path, workspace_root):
    raw = base_config(workspace_root)
    raw["routing"]["automatic_paid_fallback"] = True
    raw["routing"]["automatic_account_switch"] = True
    raw["routing"]["quota_preference"] = {"mode": "on"}
    assert _invalid(config_path, raw) == "M1 不支持 automatic_paid_fallback"


def test_account_switch_requires_mode_on_and_ready_groups(config_path, workspace_root):
    off = base_config(workspace_root)
    off["routing"]["automatic_account_switch"] = True
    off["routing"]["quota_preference"] = {"enabled": True}
    assert "quota_preference.mode=on" in _invalid(config_path, off)

    explicit_off = base_config(workspace_root)
    explicit_off["routing"]["automatic_account_switch"] = True
    explicit_off["routing"]["quota_preference"] = {"mode": "off"}
    assert "quota_preference.mode=on" in _invalid(config_path, explicit_off)

    dry = base_config(workspace_root)
    dry["routing"]["automatic_account_switch"] = True
    dry["routing"]["quota_preference"] = {"mode": "dry_run"}
    assert "quota_preference.mode=on" in _invalid(config_path, dry)

    empty = base_config(workspace_root)
    empty["routing"]["candidate_groups"] = {}
    empty["routing"]["automatic_account_switch"] = True
    empty["routing"]["quota_preference"] = {"mode": "on"}
    assert "candidate_groups" in _invalid(config_path, empty)

    missing = base_config(workspace_root)
    del missing["routing"]["candidate_groups"]
    missing["routing"]["automatic_account_switch"] = True
    missing["routing"]["quota_preference"] = {"mode": "on"}
    assert "candidate_groups" in _invalid(config_path, missing)

    rules = base_config(workspace_root)
    rules["routing"]["rules"] = [{"if": "anything"}]
    rules["routing"]["automatic_account_switch"] = True
    rules["routing"]["quota_preference"] = {"mode": "on"}
    assert "rules" in _invalid(config_path, rules)

    broken = base_config(workspace_root)
    broken["routing"]["automatic_account_switch"] = True
    broken["routing"]["quota_preference"] = {"mode": "on"}
    broken["routing"]["candidate_groups"]["implementer"]["primary"] = "codex-a"
    assert "primary" in _invalid(config_path, broken)


def test_mode_on_requires_account_switch(config_path, workspace_root):
    absent = base_config(workspace_root)
    del absent["routing"]["automatic_account_switch"]
    absent["routing"]["quota_preference"] = {"mode": "on"}
    assert _invalid(config_path, absent) == "mode=on 需要 automatic_account_switch=true"

    disabled = base_config(workspace_root)
    disabled["routing"]["automatic_account_switch"] = False
    disabled["routing"]["quota_preference"] = {"enabled": True, "mode": "on"}
    assert _invalid(config_path, disabled) == "mode=on 需要 automatic_account_switch=true"


def test_accepts_on_with_switch_and_dry_run_without_it(config_path, workspace_root):
    on = base_config(workspace_root)
    on["routing"]["automatic_account_switch"] = True
    on["routing"]["quota_preference"] = {"mode": "on", "allow_paid_on_explicit": True}
    loaded = load_config(_write(config_path, on))
    assert loaded.to_dict()["routing"] == on["routing"]
    assert quota_preference_mode(loaded) == "on"

    omitted_rules = base_config(workspace_root)
    del omitted_rules["routing"]["rules"]
    omitted_rules["routing"]["automatic_account_switch"] = True
    omitted_rules["routing"]["quota_preference"] = {"enabled": True, "mode": "on"}
    loaded_rules = load_config(_write(config_path, omitted_rules))
    assert quota_preference_mode(loaded_rules) == "on"
    assert "rules" not in loaded_rules.to_dict()["routing"]

    dry = base_config(workspace_root)
    dry["routing"]["quota_preference"] = {"mode": "dry_run"}
    dry_loaded = load_config(_write(config_path, dry))
    assert dry_loaded.routing["automatic_account_switch"] is False
    assert quota_preference_mode(dry_loaded) == "dry_run"
    assert "allow_paid_on_explicit" not in dry_loaded.to_dict()["routing"]["quota_preference"]

    dry_without_groups = base_config(workspace_root)
    del dry_without_groups["routing"]["candidate_groups"]
    del dry_without_groups["routing"]["automatic_account_switch"]
    dry_without_groups["routing"]["quota_preference"] = {"mode": "dry_run"}
    assert quota_preference_mode(load_config(_write(config_path, dry_without_groups))) == "dry_run"

    explicit_off = base_config(workspace_root)
    explicit_off["routing"]["quota_preference"] = {"enabled": False, "mode": "off"}
    off_loaded = load_config(_write(config_path, explicit_off))
    assert quota_preference_mode(off_loaded) == "off"
    assert off_loaded.to_dict()["routing"]["quota_preference"] == {"enabled": False, "mode": "off"}


def test_omitted_new_keys_do_not_change_to_dict(config_path, workspace_root):
    raw = base_config(workspace_root)
    config = load_config(_write(config_path, raw))
    assert config.to_dict()["routing"] == raw["routing"]
    assert "mode" not in config.to_dict()["routing"]["quota_preference"]
    assert "allow_paid_on_explicit" not in config.to_dict()["routing"]["quota_preference"]
    assert quota_preference_mode(config) == "off"

    bare = base_config(workspace_root)
    del bare["routing"]["quota_preference"]
    bare_config = load_config(_write(config_path, bare))
    assert bare_config.to_dict()["routing"] == bare["routing"]
    assert "quota_preference" not in bare_config.to_dict()["routing"]
    assert quota_preference_mode(bare_config) == "off"


def test_quota_preference_mode_is_off_for_v1_and_unconfigured(config_path):
    assert quota_preference_mode(load_config(config_path)) == "off"
    assert quota_preference_mode(SimpleNamespace(schema_version=1, routing={"quota_preference": {"mode": "on"}})) == "off"
    assert quota_preference_mode(SimpleNamespace(schema_version=2, routing={})) == "off"
    assert quota_preference_mode(SimpleNamespace(schema_version=2, routing={"quota_preference": {"enabled": False}})) == "off"
    assert quota_preference_mode(SimpleNamespace(
        schema_version=2, routing={"quota_preference": {"enabled": False, "mode": "on"}},
    )) == "off"
    assert quota_preference_mode(SimpleNamespace(schema_version=2, routing={"quota_preference": {"mode": "dry_run"}})) == "dry_run"
