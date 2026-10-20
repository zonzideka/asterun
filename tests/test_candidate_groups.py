"""v2 候选组与额度偏好的配置校验。未写这些键时不改 to_dict。"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from asterun.config import load_config
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


def test_planning_group_accepts_different_models_and_does_not_fill_defaults(config_path: Path, workspace_root: Path):
    raw = base_config(workspace_root)
    config = load_config(_write(config_path, raw))
    assert config.get_backend("agy-opus").model == "claude-opus-4-6"
    assert config.get_backend("agy-gemini").model == "gemini-2.5-pro"
    assert config.get_backend("agy-opus").kind == config.get_backend("agy-gemini").kind == "antigravity"
    stored = config.to_dict()["routing"]
    assert stored["candidate_groups"]["planning"]["members"] == ["agy-opus", "agy-gemini"]
    assert "models_allowed" not in stored["candidate_groups"]["planning"]
    assert stored["quota_preference"] == {"enabled": False}
    assert stored["automatic_account_switch"] is False


def test_absent_routing_keys_do_not_change_to_dict(config_path: Path):
    proposed = migrate_v1_to_v2(config_path)["config"]
    assert "candidate_groups" not in proposed["routing"]
    assert "quota_preference" not in proposed["routing"]
    config = load_config(_write(config_path, proposed))
    assert config.to_dict()["routing"] == proposed["routing"]


def test_role_kind_must_match_the_allowlist(config_path: Path, workspace_root: Path):
    raw = base_config(workspace_root)
    raw["routing"]["candidate_groups"]["implementer"] = {
        "role": "implementation", "members": ["codex-a"], "primary": "codex-a",
    }
    raw["routing"]["candidate_groups"]["reviewer"]["members"] = ["codex-b"]
    raw["routing"]["candidate_groups"]["reviewer"]["primary"] = "codex-b"
    message = _invalid(config_path, raw)
    assert "不能担任" in message


def test_members_of_one_group_must_share_a_kind(config_path: Path, workspace_root: Path):
    raw = base_config(workspace_root)
    raw["plugins"]["anthropic.claude"] = {"enabled": True}
    raw["provider_accounts"]["claude-a-acct"] = _account("claude-a-acct", "anthropic")
    raw["billing_pools"]["claude-a-pool"] = _pool()
    raw["connections"]["conn-claude-a"] = _connection(
        "anthropic.claude", "claude-a-acct", "claude-a-pool", {},
    )
    raw["backends"]["claude-a"] = {"connection_ref": "conn-claude-a"}
    raw["routing"]["candidate_groups"]["planning"]["members"] = ["agy-opus", "claude-a"]
    assert "成员种类必须相同" in _invalid(config_path, raw)


def test_execution_options_must_match(config_path: Path, workspace_root: Path):
    raw = base_config(workspace_root)
    raw["connections"]["conn-grok-a"]["options"]["timeout_seconds"] = 10
    raw["connections"]["conn-grok-b"]["options"]["timeout_seconds"] = 20
    assert "执行选项必须相同" in _invalid(config_path, raw)


def test_roles_cannot_share_members_or_review_the_implementer(config_path: Path, workspace_root: Path):
    overlap = base_config(workspace_root)
    overlap["routing"]["candidate_groups"]["planning"]["members"] = ["grok-a", "agy-opus"]
    assert "不能相交" in _invalid(config_path, overlap)

    review = base_config(workspace_root)
    review["routing"]["candidate_groups"]["reviewer"]["members"] = ["grok-a", "codex-a"]
    assert "审查组不能包含实现组的后端" in _invalid(config_path, review)

    account = base_config(workspace_root)
    account["connections"]["conn-codex-a"]["provider_account_ref"] = "grok-a-acct"
    assert "供应商账户" in _invalid(config_path, account)

    vendor = base_config(workspace_root)
    vendor["connections"]["conn-grok-a"]["options"]["model"] = "gpt-5.3"
    assert "同一家" in _invalid(config_path, vendor)


def test_one_group_per_role_and_primary_must_be_a_member(config_path: Path, workspace_root: Path):
    duplicated = base_config(workspace_root)
    duplicated["routing"]["candidate_groups"]["implementer_b"] = {
        "role": "implementation", "members": ["grok-b"], "primary": "grok-b",
    }
    duplicated["routing"]["candidate_groups"]["implementer"]["members"] = ["grok-a"]
    assert "同一角色只能有一个候选组" in _invalid(config_path, duplicated)

    primary = base_config(workspace_root)
    primary["routing"]["candidate_groups"]["implementer"]["primary"] = "codex-a"
    assert "primary 必须是成员" in _invalid(config_path, primary)


def test_subscription_pool_overage_grants_and_floor_limits(config_path: Path, workspace_root: Path):
    unknown_pool = base_config(workspace_root)
    unknown_pool["billing_pools"]["grok-a-pool"]["billing_mode"] = "unknown"
    assert "subscription 或 local" in _invalid(config_path, unknown_pool)

    overage = base_config(workspace_root)
    overage["billing_pools"]["grok-a-pool"]["paid_overage_allowed"] = True
    assert "overage" in _invalid(config_path, overage).lower() or "超额" in _invalid(config_path, deepcopy(overage))

    grants = base_config(workspace_root)
    grants["routing"]["quota_preference"] = {"count_reset_grants": True}
    assert "count_reset_grants" in _invalid(config_path, grants)

    short_floor = base_config(workspace_root)
    short_floor["routing"]["quota_preference"] = {"floor_short_bp": 499}
    assert "floor_short_bp" in _invalid(config_path, short_floor)

    long_floor = base_config(workspace_root)
    long_floor["routing"]["quota_preference"] = {"floor_long_bp": 999}
    assert "floor_long_bp" in _invalid(config_path, long_floor)


def test_models_allowed_is_optional_and_checked_only_when_written(config_path: Path, workspace_root: Path):
    raw = base_config(workspace_root)
    raw["routing"]["candidate_groups"]["planning"]["models_allowed"] = ["claude-opus-4-6", "gemini-2.5-pro"]
    config = load_config(_write(config_path, raw))
    assert config.routing["candidate_groups"]["planning"]["models_allowed"] == ["claude-opus-4-6", "gemini-2.5-pro"]

    raw["routing"]["candidate_groups"]["planning"]["models_allowed"] = ["claude-opus-4-6"]
    assert "models_allowed" in _invalid(config_path, raw)


def test_automatic_account_switch_stays_disabled(config_path: Path, workspace_root: Path):
    raw = base_config(workspace_root)
    raw["routing"]["automatic_account_switch"] = True
    raw["routing"]["quota_preference"] = {"enabled": True}
    assert "automatic_account_switch" in _invalid(config_path, raw)
