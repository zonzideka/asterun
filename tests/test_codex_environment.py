from __future__ import annotations

from pathlib import Path

from asterun.application import Application
from asterun.backends.codex import (
    DEFAULT_CODEX_BIN,
    LIVE_ENV,
    CodexBackend,
    discover_codex_bin,
    live_codex_enabled,
)
from asterun.config import BackendConfig, load_config
from asterun.policy import Principal
from tests.conftest import write_config


def test_default_bin_is_unset_and_product_has_no_author_paths() -> None:
    assert DEFAULT_CODEX_BIN is None
    source = Path(__file__).resolve().parents[1] / "src" / "asterun" / "backends" / "codex.py"
    text = source.read_text(encoding="utf-8")
    assert "/Applications/Codex.app" not in text
    assert "/Users/" not in text
    assert 'client_name: str = "asterun"' in text


def test_live_flag_and_discover_bin_order(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(LIVE_ENV, raising=False)
    assert live_codex_enabled() is False
    monkeypatch.setenv(LIVE_ENV, "1")
    assert live_codex_enabled() is True

    explicit = tmp_path / "explicit-codex"
    env_bin = tmp_path / "env-codex"
    path_dir = tmp_path / "bin"
    empty_path = tmp_path / "empty-path"
    path_dir.mkdir()
    empty_path.mkdir()
    path_bin = path_dir / "codex"
    for item in (explicit, env_bin, path_bin):
        item.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        item.chmod(0o755)

    monkeypatch.setenv("CODEX_BIN", str(env_bin))
    monkeypatch.setenv("PATH", str(path_dir))
    assert discover_codex_bin(str(explicit)) == explicit
    assert discover_codex_bin() == env_bin
    monkeypatch.delenv("CODEX_BIN")
    assert discover_codex_bin() == path_bin
    monkeypatch.setenv("PATH", str(empty_path))
    assert discover_codex_bin() is None


def test_inspect_and_default_verify_do_not_spawn(
    isolated_env: Path, workspace_root: Path, monkeypatch
) -> None:
    def boom(*_args, **_kwargs):
        raise AssertionError("inspect/verify 默认不得启动进程")

    monkeypatch.setattr("asterun.transport.subprocess.Popen", boom)
    path = write_config(
        isolated_env / "config.json",
        workspace_root,
        extra_backends={"codex": {"kind": "codex", "enabled": True}},
    )
    app = Application(load_config(path), principal=Principal("test:user", "test"))
    inspected = app.handle("backend.inspect", {"backend": "codex"})
    assert inspected.ok
    assert inspected.data["backends"][0]["is_real_connection"] is False
    verified = app.handle("connection.verify", {"backend": "codex"})
    assert verified.ok
    assert verified.data["verified"] is False
    assert verified.data["authentication"] == "not_tested"


def test_binary_without_live_flag_is_auth_required(
    isolated_env: Path, workspace_root: Path, monkeypatch
) -> None:
    dummy = isolated_env / "dummy-codex"
    dummy.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    dummy.chmod(0o755)
    monkeypatch.setenv("CODEX_BIN", str(dummy))
    path = write_config(
        isolated_env / "config.json",
        workspace_root,
        extra_backends={"codex": {"kind": "codex", "enabled": True}},
    )
    app = Application(load_config(path), principal=Principal("test:user", "test"))
    backend = app.backends["codex"]
    assert isinstance(backend, CodexBackend)
    assert backend.discover_bin() == dummy
    assert backend.can_dispatch() is False
    error = backend.unavailable_error()
    assert error.code == "AUTH_REQUIRED"
    submitted = app.handle("task.submit", {"workspace": "demo", "backend": "codex", "text": "ping"})
    assert submitted.ok is False
    assert submitted.error["code"] == "AUTH_REQUIRED"
    assert backend.dispatch_count == 0
    assert app.store.tasks == {}


def test_config_bin_is_used_for_discovery(isolated_env: Path, workspace_root: Path) -> None:
    dummy = isolated_env / "configured-codex"
    dummy.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    dummy.chmod(0o755)
    path = write_config(
        isolated_env / "config.json",
        workspace_root,
        extra_backends={"codex": {"kind": "codex", "enabled": True, "bin": str(dummy)}},
    )
    backend = CodexBackend(load_config(path).backends["codex"])
    assert backend.discover_bin() == dummy
    assert backend.config.bin == str(dummy)
    assert BackendConfig(name="codex", kind="codex", enabled=True).bin is None
