from __future__ import annotations

from pathlib import Path

from asterun.application import Application
from asterun.backends.claude import (
    DEFAULT_CLAUDE_BIN,
    LIVE_ENV,
    ClaudeBackend,
    discover_claude_bin,
    live_claude_enabled,
)
from asterun.config import load_config
from asterun.ids import RunId, TaskId
from asterun.policy import Principal
from tests.conftest import write_config


def test_default_bin_is_unset_and_product_has_no_author_paths() -> None:
    assert DEFAULT_CLAUDE_BIN is None
    source = Path(__file__).resolve().parents[1] / "src" / "asterun" / "backends" / "claude.py"
    text = source.read_text(encoding="utf-8")
    assert "/Users/" not in text
    assert "/opt/homebrew" not in text
    assert "resolve_profile" not in text
    assert "world_reader" not in text
    assert "canon_proposer" not in text


def test_live_flag_and_discover_bin_order(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(LIVE_ENV, raising=False)
    assert live_claude_enabled() is False
    monkeypatch.setenv(LIVE_ENV, "1")
    assert live_claude_enabled() is True
    explicit = tmp_path / "explicit-claude"
    env_bin = tmp_path / "env-claude"
    path_dir = tmp_path / "bin"
    empty_path = tmp_path / "empty-path"
    path_dir.mkdir()
    empty_path.mkdir()
    path_bin = path_dir / "claude"
    for item in (explicit, env_bin, path_bin):
        item.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        item.chmod(0o755)
    monkeypatch.setenv("CLAUDE_BIN", str(env_bin))
    monkeypatch.setenv("PATH", str(path_dir))
    assert discover_claude_bin(str(explicit)) == explicit
    assert discover_claude_bin() == env_bin
    monkeypatch.delenv("CLAUDE_BIN")
    assert discover_claude_bin() == path_bin
    monkeypatch.setenv("PATH", str(empty_path))
    assert discover_claude_bin() is None


def test_inspect_and_default_verify_do_not_spawn(
    isolated_env: Path, workspace_root: Path, monkeypatch
) -> None:
    def boom(*_args, **_kwargs):
        raise AssertionError("inspect/verify 默认不得启动进程")

    monkeypatch.setattr("asterun.backends.claude.subprocess.Popen", boom)
    path = write_config(
        isolated_env / "config.json",
        workspace_root,
        extra_backends={"claude": {"kind": "claude", "enabled": True}},
    )
    app = Application(load_config(path), principal=Principal("test:user", "test"))
    inspected = app.handle("backend.inspect", {"backend": "claude"})
    row = inspected.data["backends"][0]
    assert row["one_shot"] is True
    assert row["resume_supported"] is False
    capabilities = {item["name"]: item for item in row["capabilities"]}
    assert capabilities["resume_session"]["adapter_support"] == "unsupported"
    verified = app.handle("connection.verify", {"backend": "claude"})
    assert verified.ok
    assert verified.data["verified"] is False
    assert verified.data["resume_supported"] is False


def test_binary_without_live_flag_is_auth_required(
    isolated_env: Path, workspace_root: Path, monkeypatch
) -> None:
    dummy = isolated_env / "dummy-claude"
    dummy.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    dummy.chmod(0o755)
    monkeypatch.setenv("CLAUDE_BIN", str(dummy))
    path = write_config(
        isolated_env / "config.json",
        workspace_root,
        extra_backends={"claude": {"kind": "claude", "enabled": True}},
    )
    app = Application(load_config(path), principal=Principal("test:user", "test"))
    backend = app.backends["claude"]
    assert isinstance(backend, ClaudeBackend)
    assert backend.can_dispatch() is False
    assert backend.unavailable_error().code == "AUTH_REQUIRED"
    submitted = app.handle("task.submit", {"workspace": "demo", "backend": "claude", "text": "ping"})
    assert submitted.error["code"] == "AUTH_REQUIRED"
    assert backend.dispatch_count == 0


def test_injected_runner_can_dispatch_when_live(
    isolated_env: Path, workspace_root: Path, monkeypatch
) -> None:
    dummy = isolated_env / "dummy-claude"
    dummy.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    dummy.chmod(0o755)
    monkeypatch.setenv("CLAUDE_BIN", str(dummy))
    monkeypatch.setenv(LIVE_ENV, "1")
    backend = ClaudeBackend(load_config(write_config(
        isolated_env / "config.json",
        workspace_root,
        extra_backends={"claude": {"kind": "claude", "enabled": True}},
    )).backends["claude"])
    backend.runner = lambda *args: (
        '{"is_error": false, "result": "ASTERUN_CLAUDE_OK", "session_id": "s1"}',
        "",
        0,
    )
    result = backend.dispatch(TaskId("tsk_1"), RunId("run_1"), "success", "ping", cwd=workspace_root)
    assert result["status"] == "succeeded"
    assert result["native"]["resume_supported"] is False
    assert result["summary"] == "ASTERUN_CLAUDE_OK"
