from __future__ import annotations

import json

import pytest

from asterun.application import Application
from asterun.config import load_config
from asterun.config_migration import migrate_v1_to_v2
from asterun.connections.admission import input_digest
from asterun.control_protocol import digest
from asterun.errors import AsterunError, INVALID_CONFIG, UNKNOWN_FIELD
from tests.conftest import write_config


def configured_path(isolated_env, workspace_root, version, options, kind="codex"):
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
@pytest.mark.parametrize("options", [{}, {"desktop_projects": False}, {"desktop_projects": True}])
def test_codex_desktop_projects_preserves_only_explicit_values(isolated_env, workspace_root, version, options):
    config = load_config(configured_path(isolated_env, workspace_root, version, options))
    backend = config.get_backend("target")
    assert backend.desktop_projects is options.get("desktop_projects")
    assert ("desktop_projects" in backend.to_dict()) == ("desktop_projects" in options)
    if options:
        assert backend.to_dict()["desktop_projects"] is options["desktop_projects"]
    if version == 2:
        assert backend.options == config.connections[backend.connection_ref]["options"] == options


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("value", [None, 0, 1, -1, 0.0, 1.0, "true", "false", "", [], {}])
def test_codex_desktop_projects_requires_strict_boolean(isolated_env, workspace_root, version, value):
    path = configured_path(isolated_env, workspace_root, version, {"desktop_projects": value})
    with pytest.raises(AsterunError) as captured:
        load_config(path)
    assert captured.value.code == INVALID_CONFIG


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("kind", ["fake", "grok", "claude", "antigravity"])
@pytest.mark.parametrize("value", [False, True])
def test_desktop_projects_does_not_expand_other_backends(isolated_env, workspace_root, version, kind, value):
    path = configured_path(isolated_env, workspace_root, version, {"desktop_projects": value}, kind)
    with pytest.raises(AsterunError) as captured:
        load_config(path)
    assert captured.value.code == (INVALID_CONFIG if version == 1 else UNKNOWN_FIELD)


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_codex_projection_does_not_gain_defaults(isolated_env, workspace_root, version):
    config = load_config(configured_path(isolated_env, workspace_root, version, {}))
    backend = config.get_backend("target")
    expected = {
        "name": "target", "kind": "codex", "enabled": version == 1,
        "bin": None, "executable": False, "is_real_backend": True,
    }
    if version == 2:
        expected.update(connection_ref=backend.connection_ref, plugin_id="openai.codex", options={})
    assert json.dumps(backend.to_dict()) == json.dumps(expected)
    assert digest(backend.to_dict()) == digest(expected)
    projection = config.to_dict()
    expected_projection = projection | {"backends": projection["backends"] | {"target": expected}}
    assert json.dumps(projection) == json.dumps(expected_projection)
    assert digest(projection) == digest(expected_projection)


@pytest.mark.parametrize("version", [1, 2])
def test_codex_desktop_projects_binds_omitted_disabled_and_enabled_differently(isolated_env, workspace_root, version):
    digests = []
    for options in ({}, {"desktop_projects": False}, {"desktop_projects": True}):
        path = configured_path(isolated_env, workspace_root, version, options)
        digests.append(digest(load_config(path).to_dict()))
    assert len(set(digests)) == 3


@pytest.mark.parametrize("options", [{}, {"desktop_projects": False}, {"desktop_projects": True}])
def test_codex_project_migration_preserves_value_without_enabling(isolated_env, workspace_root, options):
    old_options = {"bin": "/test-only/codex"}
    path = configured_path(isolated_env, workspace_root, 1, old_options | options)
    original = path.read_bytes()
    report = migrate_v1_to_v2(path)
    assert path.read_bytes() == original
    proposed = report["config"]
    connection = proposed["connections"][proposed["backends"]["target"]["connection_ref"]]
    assert connection["options"] == old_options | options
    assert connection["enabled"] is False
    assert proposed["plugins"]["openai.codex"]["enabled"] is False
    migrated_path = isolated_env / "migrated.json"
    migrated_path.write_text(json.dumps(proposed))
    migrated = load_config(migrated_path).get_backend("target")
    assert migrated.desktop_projects is options.get("desktop_projects")


@pytest.mark.parametrize("value", [False, True])
def test_codex_project_option_is_in_fixed_plan_and_rejects_changed_config(isolated_env, workspace_root, monkeypatch, value):
    path = configured_path(isolated_env, workspace_root, 2, {"desktop_projects": value})
    raw = json.loads(path.read_text())
    raw["plugins"]["openai.codex"]["enabled"] = True
    connection = raw["connections"][raw["backends"]["target"]["connection_ref"]]
    connection["enabled"] = True
    raw["billing_pools"][connection["billing_pool_refs"][0]].update(
        billing_mode="subscription", meter="requests", local_limit=1, max_concurrency=1,
        window_id="offline-window", estimate_per_action=1,
        overage_verified=True, allow_unknown_subscription=True,
    )
    path.write_text(json.dumps(raw))
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("配置与计划验证不得启动后端"))
    app = Application.from_paths(path, isolated_env / "state")
    try:
        backend = app.config.get_backend("target")
        assert app.backends["target"].config.desktop_projects is value
        plan = app.admission.prepare("demo", "target", input_digest("offline"))
        assert plan["binding"]["connection"]["options"]["desktop_projects"] is value
        assert plan["binding"]["configuration_sha256"] == digest(app.config.to_dict())
        app.config.connections[backend.connection_ref]["options"]["desktop_projects"] = not value
        with pytest.raises(AsterunError):
            app.admission.assert_plan(plan)
        assert app.backends["target"].dispatch_count == 0
    finally:
        app.close()
