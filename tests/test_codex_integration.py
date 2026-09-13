import os
from pathlib import Path

import pytest

from asterun.backends.codex import CodexAppServerTransport


@pytest.mark.skipif(
    os.getenv("RUN_CODEX_APP_SERVER_INTEGRATION") != "1",
    reason="set RUN_CODEX_APP_SERVER_INTEGRATION=1 to run the live Codex app-server smoke test",
)
def test_live_codex_app_server_initialize_and_model_list():
    transport = CodexAppServerTransport(
        codex_bin=os.getenv("CODEX_BIN", "/Applications/Codex.app/Contents/Resources/codex"),
        client_name="asterun-test",
        client_version="0.1.0",
    )
    notifications: list[dict] = []
    transport.on_notification(notifications.append)

    try:
        transport.spawn(Path.cwd())
        init = transport.initialize(timeout=20)
        models = transport.list_models(timeout=30)

        assert init["platformFamily"] == "unix"
        assert init["platformOs"] == "macos"
        assert init["codexHome"].endswith(".codex")
        assert any(model["id"].startswith("gpt-") for model in models["data"])
        assert isinstance(notifications, list)
    finally:
        transport.close()


@pytest.mark.skipif(
    os.getenv("RUN_CODEX_APP_SERVER_INTEGRATION") != "1",
    reason="set RUN_CODEX_APP_SERVER_INTEGRATION=1 to run the live Codex app-server prompt smoke test",
)
def test_live_codex_app_server_text_prompt():
    transport = CodexAppServerTransport(
        codex_bin=os.getenv("CODEX_BIN", "/Applications/Codex.app/Contents/Resources/codex"),
        client_name="asterun-test",
        client_version="0.1.0",
    )
    try:
        transport.spawn(Path.cwd())
        transport.initialize(timeout=20)
        result = transport.run_text_prompt(
            text="Reply with exactly this token and nothing else: ASTERUN_CODEX_OK. Do not inspect files or run tools.",
            cwd=Path.cwd(),
            model=os.getenv("CODEX_TEST_MODEL", "gpt-5.5"),
            timeout=120,
        )

        assert result.status == "completed"
        assert result.message == "ASTERUN_CODEX_OK"
        assert result.thread_id
        assert result.turn_id
    finally:
        transport.close()


@pytest.mark.skipif(
    os.getenv("RUN_CODEX_APP_SERVER_INTEGRATION") != "1",
    reason="set RUN_CODEX_APP_SERVER_INTEGRATION=1 to run the live Codex persistent session smoke test",
)
def test_live_codex_persistent_session_turn():
    transport = CodexAppServerTransport(
        codex_bin=os.getenv("CODEX_BIN", "/Applications/Codex.app/Contents/Resources/codex"),
        client_name="asterun-test",
        client_version="0.1.0",
    )
    try:
        transport.spawn(Path.cwd())
        transport.initialize(timeout=20)
        session = transport.create_session(
            cwd=Path.cwd(),
            model=os.getenv("CODEX_TEST_MODEL", "gpt-5.5"),
        )
        transport.start_session_turn(
            session,
            text="Reply with exactly this token and nothing else: ASTERUN_CODEX_SESSION_OK. Do not inspect files or run tools.",
            timeout=60,
        )
        assert session._turn_complete.wait(120)
        status = session.to_status_dict()

        assert status["status"] == "completed"
        assert status["message"] == "ASTERUN_CODEX_SESSION_OK"
        assert status["thread_id"]
        assert status["current_turn_id"]
        assert status["pending_requests"] == []
    finally:
        transport.close()
