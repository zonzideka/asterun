"""内置 Antigravity 超时：v1 字段与 v2 options，默认仍为 5 分钟。"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time

import pytest

from asterun.antigravity_profile import prepare_home
from asterun.application import Application
from asterun.backends.antigravity import AntigravityBackend, build_command
from asterun.config import BackendConfig, load_config
from asterun.config_migration import migrate_v1_to_v2
from asterun.control_protocol import digest
from asterun.errors import AsterunError, INVALID_CONFIG
from tests.conftest import write_config
from tests.test_config_grok_execution import configured_path

FIXTURE = Path(__file__).parent / "fixtures" / "antigravity_cli.py"


def test_plugin_print_timeout_follows_its_own_timeout_seconds():
    import sys
    source = Path(__file__).resolve().parents[1] / "packages/asterun-plugin-antigravity/src"
    sys.path.insert(0, str(source))
    from asterun_plugin_antigravity.worker import apply_plugin_print_timeout, cli_timeout_seconds

    assert cli_timeout_seconds(20) == 20
    assert cli_timeout_seconds(2) == 2
    assert cli_timeout_seconds(1.2) == 2
    command = apply_plugin_print_timeout(build_command(Path("/usr/bin/agy"), "fixture-model"), 2)
    assert command[command.index("--print-timeout") + 1] == "2s"
    longer = apply_plugin_print_timeout(build_command(Path("/usr/bin/agy"), "fixture-model"), 20)
    assert longer[longer.index("--print-timeout") + 1] == "20s"


def test_default_print_timeout_stays_five_minutes():
    command = build_command(Path("/usr/bin/agy"), "fixture-model")
    assert command[command.index("--print-timeout") + 1] == "5m"


@pytest.mark.parametrize("seconds,flag", [(30, "30s"), (90, "90s"), (300, "5m"), (600, "10m"), (3600, "60m")])
def test_print_timeout_uses_minutes_only_when_divisible_by_sixty(seconds, flag):
    command = build_command(Path("/usr/bin/agy"), "fixture-model", seconds)
    assert command[command.index("--print-timeout") + 1] == flag


@pytest.mark.parametrize("value", [True, False, 29, 3601, 0, -1, 1.5, "300", None, 300.0])
def test_build_command_rejects_timeout_outside_bounds(value):
    with pytest.raises(AsterunError) as captured:
        build_command(Path("/usr/bin/agy"), "fixture-model", value)
    assert captured.value.code == INVALID_CONFIG
    assert "30..3600" in captured.value.message


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("seconds", [30, 90, 300, 3600])
def test_antigravity_timeout_is_explicit_on_v1_and_v2(isolated_env, workspace_root, version, seconds):
    path = configured_path(isolated_env, workspace_root, version, {"timeout_seconds": seconds}, "antigravity")
    config = load_config(path)
    backend = config.get_backend("target")
    assert backend.timeout_seconds == seconds
    assert backend.to_dict()["timeout_seconds"] == seconds
    if version == 2:
        assert backend.options["timeout_seconds"] == seconds


@pytest.mark.parametrize("version", [1, 2])
def test_omitted_antigravity_timeout_does_not_change_projection(isolated_env, workspace_root, version):
    path = configured_path(isolated_env, workspace_root, version, {}, "antigravity")
    config = load_config(path)
    backend = config.get_backend("target")
    assert backend.timeout_seconds is None
    assert "timeout_seconds" not in backend.to_dict()
    original = digest(config.to_dict())
    raw = json.loads(path.read_text())
    target = (raw["connections"][raw["backends"]["target"]["connection_ref"]]["options"]
              if version == 2 else raw["backends"]["target"])
    target["timeout_seconds"] = 600
    path.write_text(json.dumps(raw))
    assert digest(load_config(path).to_dict()) != original


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("value", [True, False, 29, 3601, 0, -1, 1.5, "300", None, 300.0])
def test_antigravity_config_rejects_invalid_timeout(isolated_env, workspace_root, version, value):
    path = configured_path(isolated_env, workspace_root, version, {"timeout_seconds": value}, "antigravity")
    with pytest.raises(AsterunError) as captured:
        load_config(path)
    assert captured.value.code == INVALID_CONFIG
    assert "30..3600" in captured.value.message


def test_v1_migration_preserves_explicit_antigravity_timeout(isolated_env, workspace_root):
    path = configured_path(
        isolated_env, workspace_root, 1,
        {"bin": "/test-only/agy", "home": "/test-only/agy-home", "model": "fixture-model", "timeout_seconds": 900},
        "antigravity",
    )
    source = path.read_bytes()
    report = migrate_v1_to_v2(path)
    assert path.read_bytes() == source
    proposed = report["config"]
    connection = proposed["connections"][proposed["backends"]["target"]["connection_ref"]]
    assert connection["options"]["timeout_seconds"] == 900
    assert connection["enabled"] is False
    migrated = isolated_env / "migrated.json"
    migrated.write_text(json.dumps(proposed))
    backend = load_config(migrated).get_backend("target")
    assert backend.timeout_seconds == 900
    assert backend.to_dict()["timeout_seconds"] == 900


def test_backend_applies_same_timeout_to_runtime_and_cli():
    backend = AntigravityBackend(BackendConfig(name="agy", kind="antigravity", model="fixture-model", timeout_seconds=90))
    assert backend.runtime.timeout == 90
    command = build_command(Path("/usr/bin/agy"), "fixture-model", backend.runtime.timeout)
    assert command[command.index("--print-timeout") + 1] == "90s"
    default = AntigravityBackend(BackendConfig(name="agy", kind="antigravity", model="fixture-model"))
    assert default.runtime.timeout == 300


def test_dispatch_sends_configured_print_timeout(isolated_env, workspace_root, monkeypatch):
    home = isolated_env / "agy-home"
    prepare_home(home)
    binary = isolated_env / "agy-stub"
    prelude = (
        f"#!{sys.executable}\nimport sys,os\n"
        "assert sys.argv[1:]==['--input-format','stream-json','--output-format','stream-json',"
        "'--model','fixture-model','--disable-slash-commands','--print-timeout','90s',"
        "'--add-dir',os.getcwd()]\n"
        "sys.argv=sys.argv[:1]\n"
    )
    binary.write_text(prelude + FIXTURE.read_text())
    binary.chmod(0o700)
    monkeypatch.setenv("ASTERUN_RUN_ANTIGRAVITY", "1")
    path = write_config(
        isolated_env / "agy-config.json", workspace_root,
        extra_backends={"agy": {
            "kind": "antigravity", "bin": str(binary), "home": str(home),
            "model": "fixture-model", "timeout_seconds": 90,
        }},
        extra={"default_backend": "agy"},
    )
    app = Application.from_paths(path, isolated_env / "state", background=True)
    try:
        assert app.backends["agy"].runtime.timeout == 90
        result = app.handle("task.submit", {"workspace": "demo", "backend": "agy", "text": "input", "idempotency_key": "timeout"})
        assert result.ok, result.error
        deadline = time.monotonic() + 5
        final = None
        while time.monotonic() < deadline:
            app.poll()
            final = app.handle("task.get", {"task_id": result.ids["task_id"]}).data
            if final["run"]["status"] == "succeeded":
                break
            time.sleep(0.01)
        assert final["run"]["status"] == "succeeded"
        assert final["run"]["summary"] == "fixture response"
    finally:
        app.close()
