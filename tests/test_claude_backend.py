from __future__ import annotations

import json

from asterun.backends.claude import (
    DEPTH_ENV,
    PARENT_ENV,
    ClaudeSession,
    apply_result,
    build_command,
    build_env,
    parse_result,
    run_claude_blocking,
)


def test_build_command_puts_allowed_tools_last() -> None:
    cmd = build_command(
        "claude",
        "review this",
        allowed_tools=("Read", "Grep", "Glob"),
        permission_mode="plan",
        append_system_prompt="read only",
    )
    assert cmd[:3] == ["claude", "-p", "review this"]
    assert cmd[-4:] == ["--allowed-tools", "Read", "Grep", "Glob"]
    assert "Write" not in cmd and "Bash" not in cmd


def test_build_command_without_tool_restriction() -> None:
    cmd = build_command("claude", "do it")
    assert "--allowed-tools" not in cmd
    assert "--permission-mode" not in cmd


def test_build_env_inherits_scrubs_and_stamps_depth() -> None:
    env = build_env(depth=1, base_env={"ANTHROPIC_API_KEY": "", "FOO": "bar", "HOME": "/h"})
    assert "ANTHROPIC_API_KEY" not in env
    assert env["FOO"] == "bar"
    assert env["HOME"] == "/h"
    assert env[DEPTH_ENV] == "1"


def test_build_env_scrubs_agent_runtime_vars_and_sets_parent() -> None:
    env = build_env(
        depth=2,
        parent_run_id="run-parent",
        base_env={
            "CODEX_HOME": "/c",
            "CLAUDE_CODE_ENTRYPOINT": "x",
            "NODE_REPL_FOO": "y",
            "CLAUDE_CODE_OAUTH_TOKEN": "tok",
            "KEEPME": "1",
            "HOME": "/h",
        },
    )
    assert "CODEX_HOME" not in env
    assert "CLAUDE_CODE_ENTRYPOINT" not in env
    assert "NODE_REPL_FOO" not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"
    assert env["KEEPME"] == "1"
    assert env[DEPTH_ENV] == "2"
    assert env[PARENT_ENV] == "run-parent"


def test_parse_result_reads_probe_shape() -> None:
    parsed = parse_result(
        json.dumps(
            {
                "type": "result",
                "is_error": False,
                "result": "LGTM, 2 nits",
                "session_id": "cae5f133",
                "total_cost_usd": 0.084,
                "num_turns": 2,
                "stop_reason": "stop_sequence",
            }
        )
    )
    assert parsed["is_error"] is False
    assert parsed["result"] == "LGTM, 2 nits"
    assert parsed["session_id"] == "cae5f133"
    assert parsed["total_cost_usd"] == 0.084
    assert parsed["num_turns"] == 2


def _session() -> ClaudeSession:
    return ClaudeSession(session_id="run-claude-x", cwd="/tmp", started_at="t0")


def test_run_claude_blocking_success_via_injected_runner() -> None:
    session = _session()
    payload = json.dumps(
        {"is_error": False, "result": "review body", "session_id": "s1", "total_cost_usd": 0.03, "num_turns": 1}
    )

    def fake_runner(cmd, cwd, env, timeout):
        assert "-p" in cmd
        assert env[DEPTH_ENV] == "1"
        return payload, "", 0

    run_claude_blocking("claude", session, "review this", "/tmp", depth=1, runner=fake_runner)
    assert session.status == "completed"
    assert session.result_text == "review body"
    assert session.provider_session_id == "s1"
    assert session._complete.is_set()


def test_run_claude_blocking_marks_failed_on_is_error() -> None:
    session = _session()
    payload = json.dumps({"is_error": True, "result": "Not logged in", "session_id": "s2"})
    run_claude_blocking(
        "claude",
        session,
        "x",
        "/tmp",
        runner=lambda *args: (payload, "", 1),
    )
    assert session.status == "failed"
    assert session.is_error is True
    assert session.result_text == "Not logged in"


def test_run_claude_blocking_empty_output_fails_closed() -> None:
    session = _session()
    run_claude_blocking(
        "claude",
        session,
        "x",
        "/tmp",
        runner=lambda *args: ("", "boom on stderr", 1),
    )
    assert session.status == "failed"
    assert "boom on stderr" in session.last_error
    assert session._complete.is_set()


def test_apply_result_keeps_completed_vs_failed() -> None:
    session = _session()
    apply_result(session, parse_result(json.dumps({"is_error": False, "result": "ok"})))
    assert session.status == "completed"


def test_claude_session_cancel_kills_process_group(monkeypatch) -> None:
    import asterun.backends.claude as cl

    killed_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(cl.os, "killpg", lambda pid, sig: killed_calls.append((pid, sig)))

    class FakeProc:
        pid = 4321

        def poll(self):
            return None

    session = _session()
    session.proc = FakeProc()
    assert session.cancel() is True
    assert session.status == "cancelled"
    assert session.stop_reason == "user_cancelled"
    assert not session._complete.is_set()
    assert killed_calls[0][0] == 4321


def test_claude_session_cancel_without_process_is_safe() -> None:
    session = _session()
    assert session.cancel() is False
    assert session.status == "cancelled"
    assert not session._complete.is_set()
