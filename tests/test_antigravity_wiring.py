from __future__ import annotations

import json
import pytest

from asterun.application import Application, build_backends
from asterun.backends import AntigravityBackend
from asterun.backends.base import DisabledBackend, capability_rows
from asterun.cli import main
from asterun.config import load_config
from asterun.control_protocol import digest
from asterun.errors import INVALID_CONFIG, AsterunError
from asterun.mcp_server import handle_rpc
from tests.conftest import write_config


@pytest.fixture(autouse=True)
def isolated_antigravity_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ASTERUN_RUN_ANTIGRAVITY", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_BIN", raising=False)


def test_antigravity_profile_fields_are_in_configuration_digest(isolated_env, workspace_root):
    home = str(isolated_env / "antigravity-home")
    path = write_config(
        isolated_env / "config.json", workspace_root,
        extra_backends={"agy": {"kind": "antigravity", "home": home, "model": "explicit-model"}},
    )
    config = load_config(path)
    serialized = config.to_dict()
    assert serialized["backends"]["agy"]["home"] == home
    assert serialized["backends"]["agy"]["model"] == "explicit-model"
    initial_digest = digest(serialized)
    config.backends["agy"].home = str(isolated_env / "other-home")
    assert digest(config.to_dict()) != initial_digest
    config.backends["agy"].home = home
    config.backends["agy"].model = "other-explicit-model"
    assert digest(config.to_dict()) != initial_digest


@pytest.mark.parametrize("field,value", [
    ("home", "relative"), ("home", "~/antigravity-home"), ("home", ""),
    ("home", None), ("home", 123), ("home", "/invalid\x00path"),
    ("model", ""), ("model", " "), ("model", "model "),
    ("model", None), ("model", 123), ("model", "model\x00suffix"),
])
def test_invalid_antigravity_profile_fields_are_rejected(isolated_env, workspace_root, field, value):
    path = write_config(
        isolated_env / "config.json", workspace_root,
        extra_backends={"agy": {"kind": "antigravity", field: value}},
    )
    with pytest.raises(AsterunError) as captured:
        load_config(path)
    assert captured.value.code == INVALID_CONFIG


def test_antigravity_factory_and_disabled_backend(isolated_env, workspace_root):
    path = write_config(
        isolated_env / "config.json", workspace_root,
        extra_backends={
            "agy": {"kind": "antigravity"},
            "agy-off": {"kind": "antigravity", "enabled": False},
        },
    )
    backends = build_backends(load_config(path))
    assert isinstance(backends["agy"], AntigravityBackend)
    assert isinstance(backends["agy-off"], DisabledBackend)
    assert not backends["agy-off"].can_dispatch()
    assert not backends["agy-off"].inspect()["execution_enabled"]


def test_antigravity_capability_boundary_is_explicit():
    rows = {row.name: row.to_dict() for row in capability_rows("antigravity")}
    supported = {"discover", "create_session", "submit", "observe", "interrupt", "native_reference", "usage"}
    assert set(rows) == supported | {"resume_session", "snapshot_review", "approval"}
    for name in supported:
        assert rows[name]["declared_support"] == "supported"
        assert rows[name]["adapter_support"] == "supported"
    assert rows["resume_session"]["declared_support"] == "supported"
    for name in ("resume_session", "snapshot_review", "approval"):
        assert rows[name]["adapter_support"] == "unsupported"
    for row in rows.values():
        assert row["verification_status"] == "not_tested"
        assert row["verified_at"] is None


def test_cli_and_mcp_inspect_antigravity_without_spawning(isolated_env, workspace_root, monkeypatch, capsys):
    path = write_config(
        isolated_env / "config.json", workspace_root,
        extra_backends={"agy": {"kind": "antigravity", "bin": str(isolated_env / "missing-agy")}},
    )

    def forbidden_spawn(*args, **kwargs):
        raise AssertionError("只读能力发现不能启动后端进程")

    monkeypatch.setattr("subprocess.Popen", forbidden_spawn)
    code = main([
        "--config", str(path), "--state-dir", str(isolated_env / "cli-state"),
        "backend-inspect", "--backend", "agy",
    ])
    assert code == 0
    cli_result = json.loads(capsys.readouterr().out)
    assert cli_result["ok"] is True
    cli_backend = cli_result["data"]["backends"][0]
    assert cli_backend["kind"] == "antigravity"
    assert cli_backend["is_real_connection"] is False
    app = Application(load_config(path))
    try:
        rpc_result = handle_rpc(app, {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "backend_inspect", "arguments": {"backend": "agy"}},
        })
    finally:
        app.close()
    assert rpc_result["result"]["isError"] is False
    mcp_result = json.loads(rpc_result["result"]["content"][0]["text"])
    mcp_backend = mcp_result["data"]["backends"][0]
    assert mcp_backend["kind"] == "antigravity"
    assert mcp_backend["is_real_connection"] is False
    assert mcp_backend["capabilities"] == cli_backend["capabilities"]
