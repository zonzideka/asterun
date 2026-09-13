from pathlib import Path

import pytest

from asterun.application import Application
from asterun.backends.codex import CodexAppServerTransport, CodexPromptResult
from asterun.config import load_config
from tests.conftest import write_config


@pytest.mark.parametrize("kind", ["codex", "grok", "claude"])
def test_application_passes_workspace_to_native_adapter(isolated_env, workspace_root, monkeypatch, kind):
    import asterun.backends.codex as codex
    import asterun.backends.grok as grok

    seen = []

    class Transport:
        def __init__(self, **kwargs):
            pass

        def spawn(self, cwd):
            seen.append(Path(cwd))

        def initialize(self):
            return {}

        def run_text_prompt(self, *, text, cwd):
            seen.append(Path(cwd))
            return CodexPromptResult(status="completed", thread_id="thread-test", turn_id="turn-test", message="已执行")

        def new_session(self, cwd):
            seen.append(Path(cwd))
            return {"sessionId": "session-test"}

        def on_notification(self, callback):
            self.callback = callback

        def prompt(self, session_id, text):
            for update_kind, body in [("agent_thought_chunk", "不应采集"), ("agent_message_chunk", "已执行")]:
                self.callback({"method": "session/update", "params": {"sessionId": session_id, "update": {
                    "sessionUpdate": update_kind, "content": {"type": "text", "text": body}}}})
            return {"stopReason": "end_turn"}

        def close(self):
            pass

    monkeypatch.setattr(codex, "CodexAppServerTransport", Transport)
    monkeypatch.setattr(grok, "GrokAcpTransport", Transport)
    binary = isolated_env / ("stub-" + kind)
    binary.write_text("#!/bin/sh\nexit 90\n")
    binary.chmod(0o755)
    monkeypatch.setenv("ASTERUN_RUN_" + kind.upper(), "1")
    backend_config = {"kind": kind, "bin": str(binary)}
    if kind == "grok":
        from asterun.grok_profile import prepare_home

        grok_home = isolated_env / "dedicated-grok-home"
        prepare_home(grok_home)
        backend_config["home"] = str(grok_home)
    config_path = write_config(isolated_env / "config.json", workspace_root,
                               extra_backends={kind: backend_config})
    app = Application(load_config(config_path))

    def runner(cmd, cwd, env, timeout):
        seen.append(Path(cwd))
        return '{"is_error": false, "result": "已执行", "session_id": "session-test"}', "", 0

    if kind == "claude":
        app.backends[kind].runner = runner
    monkeypatch.chdir(isolated_env)
    result = app.handle("task.submit", {"workspace": "demo", "backend": kind, "text": "输入"})
    assert result.ok, result.error
    assert seen and all(path == workspace_root for path in seen)
    assert result.data["run"]["summary"] == "已执行"
    assert result.data["run"]["status"] == "succeeded"
    assert result.data["session"]["backend_session_id"] == ("thread-test" if kind == "codex" else "session-test")
    app.close()


@pytest.mark.parametrize("terminal", ["failed", "interrupted", None])
def test_codex_completion_notification_is_not_automatically_success(terminal):
    class Transport(CodexAppServerTransport):
        def on_message(self, callback):
            self.callback = callback

        def start_thread(self, params, timeout=60):
            return {"thread": {"id": "thread-test"}}

        def start_turn(self, params, timeout=60):
            turn = {"id": "turn-test"}
            if terminal is not None:
                turn["status"] = terminal
            self.callback({"method": "turn/completed", "params": {"turn": turn}})
            return {"turn": {"id": "turn-test"}}

    result = Transport().run_text_prompt(text="输入", cwd="/tmp", timeout=0.1)
    assert result.status == (terminal or "unknown")
    assert result.status != "completed"


def test_codex_empty_account_does_not_verify_authentication(isolated_env, workspace_root, monkeypatch):
    import asterun.backends.codex as codex

    class Transport:
        def __init__(self, **kwargs):
            pass

        def spawn(self, cwd):
            pass

        def initialize(self):
            return {}

        def read_account(self):
            return {"account": None, "requiresOpenaiAuth": True}

        def close(self):
            pass

    binary = isolated_env / "codex-stub"
    binary.write_text("#!/bin/sh\nexit 90\n")
    binary.chmod(0o755)
    monkeypatch.setattr(codex, "CodexAppServerTransport", Transport)
    monkeypatch.setenv("ASTERUN_RUN_CODEX", "1")
    config = load_config(write_config(isolated_env / "config.json", workspace_root,
                        extra_backends={"codex": {"kind": "codex", "bin": str(binary)}}))
    app = Application(config)
    result = app.handle("connection.verify", {"backend": "codex"})
    assert not result.ok and result.error["code"] == "AUTH_REQUIRED"


def test_claude_nonzero_exit_does_not_accept_success_json():
    from asterun.backends.claude import ClaudeSession, run_claude_blocking

    session = ClaudeSession(session_id="run-test", cwd="/tmp", started_at="")
    run_claude_blocking("stub", session, "输入", "/tmp",
                       runner=lambda *args: ('{"is_error": false, "result": "看似成功"}', "", 1))
    assert session.status == "failed"
