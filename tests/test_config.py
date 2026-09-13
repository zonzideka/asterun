from __future__ import annotations

import json
from pathlib import Path

import pytest

from asterun.config import load_config
from asterun.errors import (
    CONFIG_REQUIRED,
    INVALID_CONFIG,
    PATH_INVALID,
    UNKNOWN_FIELD,
    UNKNOWN_PROJECT,
    AsterunError,
)
from tests.conftest import write_config


def test_unknown_field_is_independent_error(isolated_env: Path, workspace_root: Path) -> None:
    path = write_config(isolated_env / "config.json", workspace_root)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["author_home"] = "/home/example/work"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(AsterunError) as captured:
        load_config(path)
    assert captured.value.code == UNKNOWN_FIELD
    assert "author_home" in captured.value.message


def test_unknown_workspace_is_independent_error(config_path: Path) -> None:
    config = load_config(config_path)
    with pytest.raises(AsterunError) as captured:
        config.get_workspace("author-lab")
    assert captured.value.code == UNKNOWN_PROJECT


def test_missing_config_is_config_required(tmp_path: Path) -> None:
    with pytest.raises(AsterunError) as captured:
        load_config(tmp_path / "missing.json")
    assert captured.value.code == CONFIG_REQUIRED


def test_nonexistent_workspace_root(isolated_env: Path) -> None:
    path = isolated_env / "config.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "revision": 1,
                "workspaces": {"demo": {"root": str(isolated_env / "nope")}},
                "backends": {"fake": {"kind": "fake", "enabled": True}},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(AsterunError) as captured:
        load_config(path)
    assert captured.value.code == PATH_INVALID


def test_temp_home_and_non_author_path(isolated_env: Path, workspace_root: Path) -> None:
    path = write_config(isolated_env / "config.json", workspace_root)
    config = load_config(path)
    assert config.workspaces["demo"].root == workspace_root.resolve()
    assert config.workspaces["demo"].root.is_relative_to(isolated_env.resolve())
    assert config.workspaces["demo"].root == workspace_root.resolve()
    assert not (workspace_root / ".git").exists()


def test_optional_backend_bin_is_accepted(isolated_env: Path, workspace_root: Path) -> None:
    path = write_config(
        isolated_env / "config.json",
        workspace_root,
        extra_backends={"codex": {"kind": "codex", "enabled": True, "bin": "/tmp/codex"}},
    )
    config = load_config(path)
    assert config.backends["codex"].bin == "/tmp/codex"
    assert config.backends["codex"].executable is False


def test_backend_unknown_field_is_independent_error(isolated_env: Path, workspace_root: Path) -> None:
    path = write_config(
        isolated_env / "config.json",
        workspace_root,
        extra_backends={"codex": {"kind": "codex", "enabled": True, "author_home": "/home/example"}},
    )
    with pytest.raises(AsterunError) as captured:
        load_config(path)
    assert captured.value.code == UNKNOWN_FIELD
    assert "author_home" in captured.value.message


def test_unknown_workflow_and_scheduler_fields(isolated_env: Path, workspace_root: Path) -> None:
    path = write_config(
        isolated_env / "config.json",
        workspace_root,
        extra={"workflow": {"preset": "none", "author_home": "/home/example"}},
    )
    with pytest.raises(AsterunError) as captured:
        load_config(path)
    assert captured.value.code == UNKNOWN_FIELD
    path = write_config(
        isolated_env / "sched.json",
        workspace_root,
        extra={"scheduler": {"max_queue": 32, "slo_ms": 250}},
    )
    with pytest.raises(AsterunError) as captured:
        load_config(path)
    assert captured.value.code == UNKNOWN_FIELD


def test_default_workflow_and_scheduler_are_candidates(config_path: Path) -> None:
    config = load_config(config_path)
    assert config.workflow.preset == "none"
    assert config.workflow.max_repairs == 2
    assert config.scheduler.max_queue == 32
    assert config.scheduler.per_backend_concurrency == 1
    assert config.entries.cli is True
    assert config.entries.mcp is True


def test_unknown_entries_field(isolated_env: Path, workspace_root: Path) -> None:
    path = write_config(
        isolated_env / "config.json",
        workspace_root,
        extra={"entries": {"cli": {"enabled": True}, "grokbot": True}},
    )
    with pytest.raises(AsterunError) as captured:
        load_config(path)
    assert captured.value.code == UNKNOWN_FIELD


def test_invalid_revision(isolated_env: Path, workspace_root: Path) -> None:
    path = write_config(isolated_env / "config.json", workspace_root)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["revision"] = True
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(AsterunError) as captured:
        load_config(path)
    assert captured.value.code == INVALID_CONFIG


def test_grok_home_model_are_preserved_in_configuration_digest(isolated_env, workspace_root):
    from asterun import control_protocol

    path = write_config(
        isolated_env / "config.json", workspace_root,
        extra_backends={"grok": {"kind": "grok", "home": str(isolated_env / "grok-home"), "model": "grok-4.6"}},
    )
    config = load_config(path)
    serialized = config.to_dict()
    assert serialized["backends"]["grok"]["home"] == str(isolated_env / "grok-home")
    assert serialized["backends"]["grok"]["model"] == "grok-4.6"
    digest = control_protocol.digest(serialized)
    config.backends["grok"].home = str(isolated_env / "other-home")
    assert control_protocol.digest(config.to_dict()) != digest
    config.backends["grok"].home = str(isolated_env / "grok-home")
    config.backends["grok"].model = "grok-custom-explicit"
    assert control_protocol.digest(config.to_dict()) != digest


@pytest.mark.parametrize("field,value", [
    ("home", "relative"), ("home", "~/grok-home"), ("home", ""), ("home", None),
    ("home", 123), ("home", "/invalid\x00path"), ("model", ""), ("model", " "),
    ("model", "grok-4.6 "), ("model", None), ("model", 123),
])
def test_invalid_grok_home_or_model_is_rejected(isolated_env, workspace_root, field, value):
    path = write_config(
        isolated_env / "config.json", workspace_root,
        extra_backends={"grok": {"kind": "grok", field: value}},
    )
    with pytest.raises(AsterunError) as captured:
        load_config(path)
    assert captured.value.code == INVALID_CONFIG


@pytest.mark.parametrize("kind", ["fake", "codex", "claude"])
@pytest.mark.parametrize("field,value", [("home", "/tmp/dedicated-home"), ("model", "grok-4.6")])
def test_grok_profile_fields_cannot_be_used_for_other_backends(isolated_env, workspace_root, kind, field, value):
    path = write_config(
        isolated_env / "config.json", workspace_root,
        extra_backends={kind: {"kind": kind, field: value}},
    )
    with pytest.raises(AsterunError) as captured:
        load_config(path)
    assert captured.value.code == INVALID_CONFIG
