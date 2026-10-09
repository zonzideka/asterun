"""子进程环境只保留白名单；Claude 普通任务默认不加载 hooks 与 MCP。"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from asterun.backends.claude import build_command, build_env
from asterun.backends.codex import CodexAppServerTransport
from asterun.config import load_config
from asterun.errors import AsterunError
from tests.conftest import write_config


HOST = {
    "PATH": "/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "HOME": "/home/operator",
    "USER": "operator",
    "CODEX_HOME": "/home/operator/.codex",
    "CLAUDE_CODE_OAUTH_TOKEN": "oauth-token",
    "CLAUDE_CONFIG_DIR": "/home/operator/.claude",
    "ANTHROPIC_API_KEY": "anthropic-secret",
    "OPENAI_API_KEY": "openai-secret",
    "AWS_SECRET_ACCESS_KEY": "aws-secret",
    "GITHUB_TOKEN": "gh-secret",
    "GH_TOKEN": "gh-secret",
    "CUSTOM_CA_BUNDLE": "/certs/ca.pem",
}


def test_claude_env_keeps_login_paths_and_drops_unlisted_secrets() -> None:
    env = build_env(depth=2, parent_run_id="run-parent", base_env=dict(HOST), extra=("CUSTOM_CA_BUNDLE",))
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["LANG"] == "C.UTF-8"
    assert env["HOME"] == "/home/operator"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-token"
    assert env["CLAUDE_CONFIG_DIR"] == "/home/operator/.claude"
    assert env["CUSTOM_CA_BUNDLE"] == "/certs/ca.pem"
    assert env["ASTERUN_DEPTH"] == "2"
    assert env["ASTERUN_PARENT_RUN_ID"] == "run-parent"
    for secret in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "GH_TOKEN", "CODEX_HOME"):
        assert secret not in env


def test_non_snapshot_claude_does_not_load_hooks_or_mcp() -> None:
    cmd = build_command("claude", "do the task", allowed_tools=("Read",))
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    assert cmd[cmd.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert "--strict-mcp-config" in cmd
    assert json.loads(cmd[cmd.index("--settings") + 1])["disableAllHooks"] is True
    assert cmd[-2:] == ["--allowed-tools", "Read"]
    assert "--tools" not in cmd


def test_snapshot_claude_isolates_once_and_explicit_user_settings_skip_only_normal_tasks() -> None:
    snapshot = build_command("claude", "review", snapshot_only=True)
    assert snapshot.count("--setting-sources") == 1
    assert snapshot.count("--strict-mcp-config") == 1
    assert "--tools" in snapshot
    opted_in = build_command("claude", "do it", load_user_settings=True)
    assert "--setting-sources" not in opted_in
    assert "--strict-mcp-config" not in opted_in
    forced = build_command("claude", "review", snapshot_only=True, load_user_settings=True)
    assert forced.count("--setting-sources") == 1
    assert "--tools" in forced


def test_codex_spawn_keeps_login_home_and_drops_host_secrets(monkeypatch) -> None:
    for key, value in HOST.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(time, "sleep", lambda *_args, **_kwargs: None)
    transport = CodexAppServerTransport(codex_bin="/usr/bin/codex", extra_env=("CUSTOM_CA_BUNDLE",))
    captured = {}

    def fake_spawn(command, cwd, env):
        captured["command"] = command
        captured["env"] = env

    transport._transport.spawn = fake_spawn
    transport._transport.proc = None
    transport.spawn(Path("/tmp"))
    env = captured["env"]
    assert env["HOME"] == "/home/operator"
    assert env["CODEX_HOME"] == "/home/operator/.codex"
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["LANG"] == "C.UTF-8"
    assert env["CUSTOM_CA_BUNDLE"] == "/certs/ca.pem"
    for secret in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "GH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        assert secret not in env
    assert transport.native_home == Path("/home/operator/.codex")


def test_backend_config_appends_env_names_and_rejects_loader_overrides(isolated_env, workspace_root) -> None:
    path = write_config(isolated_env / "config.json", workspace_root, extra_backends={
        "codex": {"kind": "codex", "enabled": True, "extra_env": ["CUSTOM_CA_BUNDLE"]},
        "claude": {"kind": "claude", "enabled": True, "extra_env": ["NODE_EXTRA_CA_CERTS"], "load_user_settings": False},
    })
    config = load_config(path)
    assert config.backends["codex"].extra_env == ("CUSTOM_CA_BUNDLE",)
    assert config.backends["claude"].extra_env == ("NODE_EXTRA_CA_CERTS",)
    assert config.backends["claude"].load_user_settings is False
    raw = json.loads(path.read_text())
    raw["backends"]["codex"]["extra_env"] = ["LD_PRELOAD"]
    path.write_text(json.dumps(raw))
    with pytest.raises(AsterunError, match="LD_PRELOAD"):
        load_config(path)
    raw["backends"]["codex"]["extra_env"] = ["GITHUB_TOKEN=inline"]
    path.write_text(json.dumps(raw))
    with pytest.raises(AsterunError, match="环境变量名"):
        load_config(path)
