import os
from pathlib import Path

import pytest

from asterun.backends.grok import GrokAcpTransport
from asterun.grok_profile import build_environment


@pytest.mark.skipif(
    os.getenv("RUN_GROK_INTEGRATION") != "1",
    reason="set RUN_GROK_INTEGRATION=1 to run the live grok ACP smoke test",
)
def test_live_grok_acp_smoke():
    grok_bin = os.getenv("GROK_BIN")
    if not grok_bin:
        pytest.skip("live grok test needs GROK_BIN; product code has no default author path")
    grok_home = os.getenv("ASTERUN_GROK_TEST_HOME")
    if not grok_home:
        pytest.skip("live grok test needs an independently prepared ASTERUN_GROK_TEST_HOME")
    transport = GrokAcpTransport(grok_bin=grok_bin, env=build_environment(grok_home))
    chunks: list[str] = []

    def on_msg(msg):
        update = msg.get("params", {}).get("update", {})
        if update.get("sessionUpdate") == "agent_message_chunk":
            chunks.append(update.get("content", {}).get("text", ""))

    try:
        transport.spawn(Path.cwd())
        transport.on_notification(on_msg)
        init = transport.initialize(timeout=20)
        # loadSession 只是握手声明，不能当成续接已通过。
        assert "agentCapabilities" in init
        session = transport.new_session(Path.cwd(), timeout=30)
        result = transport.prompt(
            str(session["sessionId"]),
            "Reply with exactly this token and nothing else: ASTERUN_GROK_OK",
            timeout=180,
        )
        assert result["stopReason"] == "end_turn"
        assert "ASTERUN_GROK_OK" in "".join(chunks)
    finally:
        transport.close()
