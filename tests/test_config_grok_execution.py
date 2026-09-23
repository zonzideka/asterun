from __future__ import annotations

import json

import pytest

from asterun.config import load_config
from asterun.config_migration import migrate_v1_to_v2
from asterun.control_protocol import digest
from asterun.errors import AsterunError, INVALID_CONFIG, UNKNOWN_FIELD
from tests.conftest import write_config


FIELDS = ("execution_profile", "max_turns", "timeout_seconds", "session_sync_home")


def configured_path(isolated_env, workspace_root, version, options, kind="grok"):
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
@pytest.mark.parametrize("options", [
    {"session_sync_home": "/test-only/usage-home"},
    {}, {"execution_profile": "text-only-v1"}, {"max_turns": 1},
    {"timeout_seconds": 1}, {"timeout_seconds": 3600},
    {"execution_profile": "workspace-code-v1"},
    {"execution_profile": "workspace-code-v1", "max_turns": 1, "timeout_seconds": 1},
    {"execution_profile": "workspace-code-v1", "max_turns": 100, "timeout_seconds": 3600},
    {"execution_profile": "text-only-v1", "max_turns": 1, "timeout_seconds": 3600},
])
def test_grok_execution_options_preserve_only_explicit_values(isolated_env, workspace_root, version, options):
    config = load_config(configured_path(isolated_env, workspace_root, version, options))
    backend = config.get_backend("target")
    serialized = backend.to_dict()
    for field in FIELDS:
        assert getattr(backend, field) == options.get(field)
        assert (field in serialized) == (field in options)
        if field in options:
            assert serialized[field] == options[field]
    if version == 2:
        assert backend.options == config.connections[backend.connection_ref]["options"] == options


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("options", [
    {"execution_profile": value} for value in (None, True, 1, {}, [], "", " text-only-v1", "workspace-code-v2")
] + [
    {"execution_profile": "workspace-code-v1", "max_turns": value}
    for value in (None, True, False, 1.0, "1", 0, -1, 101, [], {})
] + [
    {"timeout_seconds": value}
    for value in (None, True, False, 1.0, "1", 0, -1, 3601, [], {})
] + [
    *[{"session_sync_home": value} for value in (None, True, "", "relative", "/foo/../bar", "/tmp/invalid\x00")],
    {"max_turns": 2}, {"execution_profile": "text-only-v1", "max_turns": 2},
])
def test_grok_execution_limits_reject_invalid_or_implicitly_relaxed_options(isolated_env, workspace_root, version, options):
    path = configured_path(isolated_env, workspace_root, version, options)
    with pytest.raises(AsterunError) as captured:
        load_config(path)
    assert captured.value.code == INVALID_CONFIG


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("kind", ["fake", "codex", "claude", "antigravity"])
@pytest.mark.parametrize("field,value", [
    ("session_sync_home", "/test-only/usage-home"),
    ("execution_profile", "workspace-code-v1"), ("max_turns", 1), ("timeout_seconds", 180),
])
def test_grok_execution_options_do_not_expand_other_builtin_backends(isolated_env, workspace_root, version, kind, field, value):
    path = configured_path(isolated_env, workspace_root, version, {field: value}, kind)
    with pytest.raises(AsterunError) as captured:
        load_config(path)
    assert captured.value.code == (INVALID_CONFIG if version == 1 else UNKNOWN_FIELD)


@pytest.mark.parametrize("options", [
    {"session_sync_home": "/test-only/usage-home"},
    {}, {"execution_profile": "text-only-v1", "max_turns": 1},
    {"execution_profile": "workspace-code-v1", "max_turns": 100, "timeout_seconds": 3600},
])
def test_v1_migration_preserves_explicit_grok_execution_options_without_enabling(isolated_env, workspace_root, options):
    old_options = {"bin": "/test-only/grok", "home": "/test-only/grok-profile", "model": "grok-4.6"}
    path = configured_path(isolated_env, workspace_root, 1, old_options | options)
    source = path.read_bytes()
    report = migrate_v1_to_v2(path)
    assert path.read_bytes() == source
    proposed = report["config"]
    connection = proposed["connections"][proposed["backends"]["target"]["connection_ref"]]
    assert connection["options"] == old_options | options
    assert connection["enabled"] is False
    assert proposed["plugins"]["xai.grok"]["enabled"] is False
    migrated_path = isolated_env / "migrated.json"
    migrated_path.write_text(json.dumps(proposed))
    backend = load_config(migrated_path).get_backend("target")
    for field in FIELDS:
        assert getattr(backend, field) == options.get(field)


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_grok_backend_projection_and_digest_do_not_gain_defaults(isolated_env, workspace_root, version):
    config = load_config(configured_path(isolated_env, workspace_root, version, {}))
    backend = config.get_backend("target")
    expected = {
        "name": "target", "kind": "grok", "enabled": version == 1,
        "bin": None, "executable": False, "is_real_backend": True,
        "home": None, "model": None,
    }
    if version == 2:
        expected.update(connection_ref=backend.connection_ref, plugin_id="xai.grok", options={})
    assert json.dumps(backend.to_dict()) == json.dumps(expected)
    assert digest(backend.to_dict()) == digest(expected)
    original_digest = digest(config.to_dict())
    backend.execution_profile = "text-only-v1"
    assert digest(config.to_dict()) != original_digest


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("field,value", [
    ("session_sync_home", "/test-only/usage-home"),
    ("execution_profile", "text-only-v1"), ("max_turns", 2), ("timeout_seconds", 181),
])
def test_explicit_grok_execution_settings_are_bound_to_config_digest(isolated_env, workspace_root, version, field, value):
    options = {"execution_profile": "workspace-code-v1", "max_turns": 1, "timeout_seconds": 180}
    path = configured_path(isolated_env, workspace_root, version, options)
    original_digest = digest(load_config(path).to_dict())
    raw = json.loads(path.read_text())
    target = (raw["connections"][raw["backends"]["target"]["connection_ref"]]["options"]
              if version == 2 else raw["backends"]["target"])
    target[field] = value
    path.write_text(json.dumps(raw))
    assert digest(load_config(path).to_dict()) != original_digest
