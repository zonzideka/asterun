from __future__ import annotations

import json
from pathlib import Path

import pytest

from asterun.backends.grok import (
    ASTERUN_RECURSION_DENY,
    DEFAULT_GROK_MODEL,
    build_grok_agent_command,
    build_initialize_params,
    build_session_new_params,
    build_session_prompt_params,
    known_grok_models,
    resolve_grok_model,
    resolve_reasoning_effort,
)
from asterun.errors import AsterunError, CAPABILITY_UNSUPPORTED, INVALID_CONFIG


def test_grok_command_preserves_deny_rules_and_stdio_mode() -> None:
    cmd = build_grok_agent_command(
        "grok",
        bash_deny=["rm -rf*", "sudo*"],
        extra_mcp_deny=["MCPTool(example__tool)"],
    )
    assert cmd[:5] == ["grok", "--deny", "Bash(rm -rf*)", "--deny", "Bash(sudo*)"]
    assert "MCPTool(example__tool)" in cmd
    assert ASTERUN_RECURSION_DENY in cmd
    assert cmd[-3:] == ["agent", "--no-leader", "stdio"]
    assert "--always-approve" not in cmd
    assert "*" in [cmd[index + 1] for index, value in enumerate(cmd) if value == "--deny"]
    assert cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert all(flag in cmd for flag in ("--disable-web-search", "--no-subagents", "--no-plan"))
    assert "novel-toolkit" not in " ".join(cmd)
    assert "keyworld" not in " ".join(cmd)
    assert "agent-runner" not in " ".join(cmd)


def test_grok_command_injects_model_default() -> None:
    cmd = build_grok_agent_command("grok")
    assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "grok-4.6"
    assert cmd.index("--model") < cmd.index("agent")
    assert DEFAULT_GROK_MODEL == "grok-4.6"
    assert resolve_grok_model("gpt-5.5") == "gpt-5.5"
    assert resolve_grok_model("grok-composer-2.5-fast") == "grok-composer-2.5-fast"
    assert resolve_grok_model("grok-build") == "grok-4.6"


def test_grok_model_ignores_ambient_override(monkeypatch) -> None:
    monkeypatch.setenv("GROK_MODEL", "grok-composer-2.5-fast")
    assert resolve_grok_model(None) == "grok-4.6"
    assert resolve_grok_model("unknown-explicit-model") == "unknown-explicit-model"


@pytest.mark.parametrize("model", ["", " ", "grok-4.6 ", 4, "grok\x00model"])
def test_grok_model_rejects_invalid_input(model) -> None:
    with pytest.raises(AsterunError) as captured:
        resolve_grok_model(model)
    assert captured.value.code == INVALID_CONFIG


def test_grok_models_cache_only_when_explicit(tmp_path: Path, monkeypatch) -> None:
    cache = tmp_path / "models_cache.json"
    cache.write_text(
        json.dumps({"models": {"grok-5-hypothetical": {"info": {"supports_reasoning_effort": True}}}}),
        encoding="utf-8",
    )
    assert "grok-4.5" in known_grok_models()
    monkeypatch.setenv("GROK_MODELS_CACHE", str(cache))
    assert known_grok_models() == {"grok-5-hypothetical"}
    assert resolve_grok_model("grok-5-hypothetical") == "grok-5-hypothetical"
    cache.write_text("{not json", encoding="utf-8")
    assert "grok-4.5" in known_grok_models()


def test_grok_reasoning_effort_flag() -> None:
    cmd = build_grok_agent_command("grok", model="grok-4.5", reasoning_effort="low")
    index = cmd.index("--reasoning-effort")
    assert cmd[index + 1] == "low" and index < cmd.index("agent")
    assert "--reasoning-effort" not in build_grok_agent_command("grok", model="grok-4.5")
    assert "--reasoning-effort" not in build_grok_agent_command(
        "grok", model="grok-4.5", reasoning_effort="ultra"
    )
    assert "--reasoning-effort" not in build_grok_agent_command(
        "grok", model="grok-composer-2.5-fast", reasoning_effort="low"
    )


def test_grok_reasoning_effort_env_and_cache(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GROK_REASONING_EFFORT", "MEDIUM")
    assert resolve_reasoning_effort(None, "grok-4.5") is None
    assert resolve_reasoning_effort("low", "grok-4.5") == "low"
    monkeypatch.delenv("GROK_REASONING_EFFORT")
    cache = tmp_path / "models_cache.json"
    cache.write_text(
        json.dumps(
            {
                "models": {
                    "m-yes": {"info": {"supports_reasoning_effort": True}},
                    "m-no": {"info": {"supports_reasoning_effort": False}},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GROK_MODELS_CACHE", str(cache))
    assert resolve_reasoning_effort("high", "m-yes") == "high"
    assert resolve_reasoning_effort("high", "m-no") is None


def test_grok_command_flags_before_agent() -> None:
    cmd = build_grok_agent_command("grok", max_turns=1, sandbox="read-only")
    agent_at = cmd.index("agent")
    for flag, value in (("--max-turns", "1"), ("--sandbox", "read-only")):
        index = cmd.index(flag)
        assert cmd[index + 1] == value and index < agent_at


@pytest.mark.parametrize("options", [
    {"max_turns": 80}, {"sandbox": "net"}, {"allowed_tools": ("Read",)}, {"worktree": "run-x"},
])
def test_grok_text_profile_rejects_relaxed_options(options) -> None:
    with pytest.raises(AsterunError) as captured:
        build_grok_agent_command("grok", **options)
    assert captured.value.code == CAPABILITY_UNSUPPORTED


def test_grok_acp_param_builders() -> None:
    assert build_initialize_params() == {"protocolVersion": 1}
    assert build_session_new_params(cwd="/tmp/ws") == {"cwd": "/tmp/ws", "mcpServers": []}
    assert build_session_prompt_params(session_id="s1", text="hi") == {
        "sessionId": "s1",
        "prompt": [{"type": "text", "text": "hi"}],
    }
