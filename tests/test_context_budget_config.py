"""v2 context_budget：未写不进 to_dict / digest；只接受 off 与 observe。"""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from asterun.config import load_config
from asterun.config_migration import migrate_v1_to_v2
from asterun.config_v2 import context_budget_settings, default_context_budget
from asterun.control_protocol import digest
from asterun.errors import INVALID_CONFIG, UNKNOWN_FIELD, AsterunError


V2_KEYS = {
    "approval_policy",
    "backends",
    "billing_pools",
    "connections",
    "default_backend",
    "entries",
    "plugins",
    "provider_accounts",
    "revision",
    "routing",
    "scheduler",
    "schema_version",
    "source_path",
    "workflow",
    "workspaces",
}


def _write(path, raw):
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def _proposed(config_path):
    proposed = migrate_v1_to_v2(config_path)["config"]
    assert "context_budget" not in proposed
    assert "quota" not in proposed
    return proposed


def _load(config_path, isolated_env, name, budget=None, mutate=None):
    raw = _proposed(config_path)
    if mutate is not None:
        mutate(raw)
    if budget is not None:
        raw["context_budget"] = budget
    return raw, load_config(_write(isolated_env / name, raw))


def test_omitted_key_keeps_to_dict_and_digest(config_path, isolated_env):
    raw = _proposed(config_path)
    config = load_config(_write(isolated_env / "cb-omit.json", raw))
    projected = config.to_dict()
    assert config.context_budget_explicit is False
    assert "context_budget" not in projected
    assert set(projected) == V2_KEYS
    assert digest(projected) == digest({key: value for key, value in projected.items() if key != "context_budget"})
    assert "context_budget" not in json.dumps(projected, sort_keys=True)
    assert context_budget_settings(config) == default_context_budget()
    assert context_budget_settings(config)["mode"] == "off"
    leaked = context_budget_settings(config)
    leaked["mode"] = "observe"
    leaked["capsule"]["notes_max_bytes"] = 1
    assert context_budget_settings(config) == default_context_budget()
    assert config.to_dict() == projected


def test_explicit_empty_serializes_defaults_and_changes_digest(config_path, isolated_env):
    omitted_raw, omitted = _load(config_path, isolated_env, "cb-omit-digest.json")
    explicit_raw = deepcopy(omitted_raw)
    explicit_raw["context_budget"] = {}
    explicit = load_config(_write(isolated_env / "cb-empty.json", explicit_raw))
    assert explicit.context_budget_explicit is True
    assert explicit.to_dict()["context_budget"] == default_context_budget()
    assert context_budget_settings(explicit) == default_context_budget()
    assert digest(explicit.to_dict()) != digest(omitted.to_dict())
    assert "quota" not in explicit.to_dict()
    assert explicit.to_dict()["routing"] == omitted_raw["routing"]


def test_observe_accepts_bounds_and_omitted_profile_fields(config_path, isolated_env):
    ten = 10_000_000_000
    budget = {
        "mode": "observe",
        "capsule": {
            "nonmandatory_soft_bytes": 65536,
            "nonmandatory_hard_bytes": 131072,
            "notes_max_bytes": 16384,
            "progress_markers": False,
        },
        "segmentation": {
            "backends": ["fake"],
            "roles": ["repair", "implementation"],
            "min_long_task_processed": 1_000_000_000,
            "resume_max_residency": 100_000_000,
            "observe_capsule_min_processed": 0,
        },
        "ab_assignment": {"enabled": True, "enforce_ratio_bp": 10000},
        "profiles": {
            "fake/implementation": {
                "segment_turns": 1000,
                "segment_soft": ten,
                "residency_soft": 1,
                "task_soft": ten,
                "task_hard": ten,
                "max_segments": 100,
                "segment": True,
            },
            "fake/repair": {"segment_turns": 1, "max_segments": 1, "segment": False},
            "fake/review": {"segment": False},
            "fake/planning": {"task_soft": 5},
        },
    }
    raw, loaded = _load(config_path, isolated_env, "cb-observe.json", budget)
    assert loaded.context_budget_explicit is True
    settings = context_budget_settings(loaded)
    assert settings["mode"] == "observe"
    assert settings["token_basis"] == "processed"
    assert settings["capsule"]["progress_markers"] is False
    assert settings["segmentation"]["backends"] == ["fake"]
    assert settings["segmentation"]["roles"] == ["repair", "implementation"]
    assert settings["ab_assignment"] == {"enabled": True, "enforce_ratio_bp": 10000}
    assert set(settings["profiles"]["fake/planning"]) == {"task_soft"}
    assert settings["profiles"]["fake/review"] == {"segment": False}
    assert "task_hard" not in settings["profiles"]["fake/repair"]
    assert settings["profiles"]["fake/implementation"]["segment"] is True
    assert loaded.to_dict()["context_budget"] == settings
    assert loaded.to_dict()["routing"] == raw["routing"]
    assert "quota" not in loaded.to_dict()
    copied = context_budget_settings(loaded)
    copied["profiles"]["fake/planning"]["task_hard"] = 9
    assert "task_hard" not in context_budget_settings(loaded)["profiles"]["fake/planning"]

    partial = {"mode": "observe", "segmentation": {"roles": []}, "profiles": {"fake/review": {}}}
    _, sparse = _load(config_path, isolated_env, "cb-observe-sparse.json", partial)
    sparse_settings = context_budget_settings(sparse)
    assert sparse_settings["mode"] == "observe"
    assert sparse_settings["segmentation"]["roles"] == []
    assert sparse_settings["segmentation"]["backends"] == []
    assert sparse_settings["segmentation"]["min_long_task_processed"] == 600_000
    assert sparse_settings["capsule"]["nonmandatory_soft_bytes"] == 16384
    assert sparse_settings["capsule"]["nonmandatory_hard_bytes"] == 24576
    assert sparse_settings["profiles"] == {"fake/review": {}}
    assert sparse_settings["ab_assignment"]["enabled"] is False

    floor = {
        "mode": "off",
        "capsule": {
            "nonmandatory_soft_bytes": 1024,
            "nonmandatory_hard_bytes": 1024,
            "notes_max_bytes": 0,
        },
        "ab_assignment": {"enforce_ratio_bp": 0},
        "profiles": {
            "fake/implementation": {"segment_soft": 1, "task_soft": 1, "task_hard": 1, "segment_turns": 1},
        },
    }
    _, low = _load(config_path, isolated_env, "cb-floor.json", floor)
    low_settings = context_budget_settings(low)
    assert low_settings["mode"] == "off"
    assert low_settings["capsule"]["nonmandatory_hard_bytes"] == 1024
    assert low_settings["capsule"]["progress_markers"] is True
    assert low_settings["profiles"]["fake/implementation"]["segment_turns"] == 1
    assert "segment" not in low_settings["profiles"]["fake/implementation"]
    assert "context_budget" in low.to_dict()


def test_v1_settings_are_off(config_path, isolated_env):
    config = load_config(config_path)
    assert config.schema_version == 1
    projected = config.to_dict()
    assert "context_budget" not in projected
    assert digest(projected) == digest({key: value for key, value in projected.items() if key != "context_budget"})
    assert context_budget_settings(config) == default_context_budget()
    config.context_budget = {"mode": "observe"}
    assert context_budget_settings(config) == default_context_budget()
    assert context_budget_settings(SimpleNamespace(schema_version=1, context_budget={"mode": "observe"}))["mode"] == "off"
    assert context_budget_settings(SimpleNamespace(schema_version=2))["mode"] == "off"
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["context_budget"] = {"mode": "off"}
    with pytest.raises(AsterunError) as caught:
        load_config(_write(isolated_env / "v1-cb.json", raw))
    assert caught.value.code == UNKNOWN_FIELD


@pytest.mark.parametrize("budget, code, fragment", [
    ({"mode": "enforce"}, INVALID_CONFIG, "enforce 尚未实现"),
    ({"mode": "on"}, INVALID_CONFIG, "只能是 off 或 observe"),
    ({"mode": "hard"}, INVALID_CONFIG, "不允许值 hard"),
    ({"token_basis": "raw"}, INVALID_CONFIG, "token_basis"),
    ({"token_basis": "hard"}, INVALID_CONFIG, "不允许值 hard"),
    ({"extra": 1}, UNKNOWN_FIELD, "未知字段"),
    ({"enforcement": "soft"}, UNKNOWN_FIELD, "enforcement"),
    ({"enforcement": "hard"}, UNKNOWN_FIELD, "enforcement"),
    ({"capsule": {"notes_max_bytes": True}}, INVALID_CONFIG, "整数"),
    ({"capsule": {"nonmandatory_soft_bytes": False}}, INVALID_CONFIG, "整数"),
    ({"segmentation": {"min_long_task_processed": True}}, INVALID_CONFIG, "整数"),
    ({"ab_assignment": {"enforce_ratio_bp": True}}, INVALID_CONFIG, "整数"),
    ({"profiles": {"fake/implementation": {"segment_turns": True}}}, INVALID_CONFIG, "整数"),
    ({"profiles": {"fake/implementation": {"task_hard": True}}}, INVALID_CONFIG, "整数"),
    ({"profiles": {"fake/implementation": {"max_segments": False}}}, INVALID_CONFIG, "整数"),
    ({"capsule": {"nonmandatory_soft_bytes": 1023}}, INVALID_CONFIG, "nonmandatory_soft_bytes"),
    ({"capsule": {"nonmandatory_soft_bytes": 65537}}, INVALID_CONFIG, "nonmandatory_soft_bytes"),
    ({"capsule": {"nonmandatory_hard_bytes": 1023}}, INVALID_CONFIG, "nonmandatory_hard_bytes"),
    ({"capsule": {"nonmandatory_hard_bytes": 131073}}, INVALID_CONFIG, "nonmandatory_hard_bytes"),
    ({"capsule": {"notes_max_bytes": -1}}, INVALID_CONFIG, "notes_max_bytes"),
    ({"capsule": {"notes_max_bytes": 16385}}, INVALID_CONFIG, "notes_max_bytes"),
    ({"capsule": {"nonmandatory_soft_bytes": 30000}}, INVALID_CONFIG, "大于等于"),
    ({"capsule": {"nonmandatory_soft_bytes": 20000, "nonmandatory_hard_bytes": 19999}}, INVALID_CONFIG, "大于等于"),
    ({"segmentation": {"backends": ["missing"]}}, INVALID_CONFIG, "未知后端"),
    ({"segmentation": {"roles": ["review"]}}, INVALID_CONFIG, "roles"),
    ({"segmentation": {"roles": ["hard"]}}, INVALID_CONFIG, "不允许值 hard"),
    ({"segmentation": {"min_long_task_processed": -1}}, INVALID_CONFIG, "min_long_task_processed"),
    ({"segmentation": {"min_long_task_processed": 1_000_000_001}}, INVALID_CONFIG, "min_long_task_processed"),
    ({"segmentation": {"resume_max_residency": 100_000_001}}, INVALID_CONFIG, "resume_max_residency"),
    ({"segmentation": {"observe_capsule_min_processed": 1_000_000_001}}, INVALID_CONFIG, "observe_capsule_min_processed"),
    ({"ab_assignment": {"enforce_ratio_bp": 10001}}, INVALID_CONFIG, "enforce_ratio_bp"),
    ({"ab_assignment": {"enforce_ratio_bp": -1}}, INVALID_CONFIG, "enforce_ratio_bp"),
    ({"ab_assignment": {"enabled": "false"}}, INVALID_CONFIG, "布尔"),
    ({"profiles": {"missing/implementation": {"segment_turns": 1}}}, INVALID_CONFIG, "未知后端"),
    ({"profiles": {"fake/nope": {}}}, INVALID_CONFIG, "角色"),
    ({"profiles": "fake/implementation"}, INVALID_CONFIG, "必须为对象"),
    ({"profiles": {"fake/implementation": {"task_soft": 10, "task_hard": 9}}}, INVALID_CONFIG, "task_hard"),
    ({"profiles": {"fake/implementation": {"segment_soft": 8, "task_soft": 7, "task_hard": 9}}}, INVALID_CONFIG, "task_hard"),
    ({"profiles": {"fake/implementation": {"segment_soft": 8, "task_hard": 7}}}, INVALID_CONFIG, "task_hard"),
    ({"profiles": {"fake/review": {"segment": True}}}, INVALID_CONFIG, "segment=true"),
    ({"profiles": {"fake/review": {"segment": "hard"}}}, INVALID_CONFIG, "不允许值 hard"),
    ({"profiles": {"fake/implementation": {"segment_turns": 0}}}, INVALID_CONFIG, "segment_turns"),
    ({"profiles": {"fake/implementation": {"segment_turns": 1001}}}, INVALID_CONFIG, "segment_turns"),
    ({"profiles": {"fake/implementation": {"max_segments": 101}}}, INVALID_CONFIG, "max_segments"),
    ({"profiles": {"fake/implementation": {"task_soft": 10**10 + 1}}}, INVALID_CONFIG, "task_soft"),
    ({"profiles": {"fake/implementation": {"residency_soft": 0}}}, INVALID_CONFIG, "residency_soft"),
    ({"profiles": {"fake/implementation": {"enforcement": "soft"}}}, UNKNOWN_FIELD, "enforcement"),
    ({"profiles": {"fake/implementation": {"enforcement": "hard"}}}, INVALID_CONFIG, "不允许值 hard"),
    ({"profiles": {"fake/implementation": {"nope": 1}}}, UNKNOWN_FIELD, "未知字段"),
    ({"capsule": {"balance": 1}}, UNKNOWN_FIELD, "未知字段"),
])
def test_context_budget_rejects_invalid_values(config_path, isolated_env, budget, code, fragment):
    raw = _proposed(config_path)
    raw["context_budget"] = budget
    with pytest.raises(AsterunError) as caught:
        load_config(_write(isolated_env / "cb-bad.json", raw))
    assert caught.value.code == code
    assert fragment in caught.value.message
