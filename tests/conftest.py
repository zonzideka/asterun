from __future__ import annotations

import json
from pathlib import Path

import pytest

from asterun.application import Application
from asterun.config import load_config
from asterun.policy import AllowConfiguredWorkspaces, Principal
from asterun.store import MemoryStore


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    asterun_home = tmp_path / "asterun-home"
    home.mkdir()
    asterun_home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ASTERUN_HOME", str(asterun_home))
    # 默认离线用例不因开发机装有 Codex/Grok/Claude 而改变发现结果。
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.delenv("ASTERUN_CONFIG", raising=False)
    monkeypatch.delenv("ASTERUN_RUN_CODEX", raising=False)
    monkeypatch.delenv("ASTERUN_RUN_GROK", raising=False)
    monkeypatch.delenv("ASTERUN_RUN_CLAUDE", raising=False)
    monkeypatch.delenv("ASTERUN_RUN_ANTIGRAVITY", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_BIN", raising=False)
    monkeypatch.delenv("RUN_ANTIGRAVITY_INTEGRATION", raising=False)
    monkeypatch.delenv("CODEX_BIN", raising=False)
    monkeypatch.delenv("GROK_BIN", raising=False)
    monkeypatch.delenv("CLAUDE_BIN", raising=False)
    monkeypatch.delenv("GROK_MODELS_CACHE", raising=False)
    monkeypatch.delenv("RUN_CODEX_APP_SERVER_INTEGRATION", raising=False)
    monkeypatch.delenv("RUN_GROK_INTEGRATION", raising=False)
    monkeypatch.setenv("ASTERUN_ACCOUNT_REF", "account:test")
    monkeypatch.setenv("ASTERUN_RUNTIME_REF", "host:test")
    return tmp_path


@pytest.fixture
def workspace_root(isolated_env: Path) -> Path:
    root = isolated_env / "workspace"
    root.mkdir()
    (root / "note.txt").write_text("非 Git 输入\n", encoding="utf-8")
    return root


def write_config(
    path: Path,
    workspace_root: Path,
    extra_backends: dict | None = None,
    extra: dict | None = None,
) -> Path:
    backends = {"fake": {"kind": "fake", "enabled": True}}
    if extra_backends:
        backends.update(extra_backends)
    payload = {
        "schema_version": 1,
        "revision": 1,
        "workspaces": {"demo": {"root": str(workspace_root), "allow_non_git": True}},
        "backends": backends,
        "default_backend": "fake",
    }
    if extra:
        payload.update(extra)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


@pytest.fixture
def config_path(isolated_env: Path, workspace_root: Path) -> Path:
    return write_config(isolated_env / "config.json", workspace_root)


@pytest.fixture
def app(config_path: Path) -> Application:
    return Application(
        load_config(config_path),
        store=MemoryStore(),
        policy=AllowConfiguredWorkspaces({"demo"}),
        principal=Principal("test:user", "test"),
    )


@pytest.fixture(scope="session")
def external_fake_installation(tmp_path_factory):
    from tests.plugin_support import build_external_fake

    return build_external_fake(tmp_path_factory.mktemp("external-plugin"))
