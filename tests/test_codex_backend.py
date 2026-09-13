from asterun.backends.codex import (
    CODEX_CLIENT_METHODS,
    CodexAppServerTransport,
    CodexSession,
    build_codex_app_server_command,
    build_image_user_input,
    build_initialize_params,
    build_local_image_user_input,
    build_text_user_input,
    build_thread_start_params,
    build_turn_steer_params,
    build_turn_start_params,
    extract_thread_id,
    extract_turn_id,
)


def test_codex_command_uses_stdio_app_server():
    assert build_codex_app_server_command("codex") == ["codex", "app-server", "--listen", "stdio://"]
    assert "thread/start" in CODEX_CLIENT_METHODS
    assert "command/exec" in CODEX_CLIENT_METHODS


def test_codex_initialize_params_shape():
    params = build_initialize_params(client_name="test-client", client_version="1.2.3", experimental_api=True)

    assert params == {
        "clientInfo": {"name": "test-client", "version": "1.2.3"},
        "capabilities": {"experimentalApi": True},
    }


def test_codex_initialize_sends_initialized_notification():
    class StubTransport:
        def __init__(self):
            self.requests = []
            self.notifications = []

        def request(self, method, params, timeout):
            self.requests.append((method, params, timeout))
            return {"userAgent": "Codex Desktop/test"}

        def notify(self, method, params):
            self.notifications.append((method, params))

    stub = StubTransport()
    transport = CodexAppServerTransport(client_name="test-client", client_version="1.2.3")
    transport._transport = stub

    result = transport.initialize(timeout=7)

    assert result == {"userAgent": "Codex Desktop/test"}
    assert stub.requests == [
        (
            "initialize",
            {
                "clientInfo": {"name": "test-client", "version": "1.2.3"},
                "capabilities": {"experimentalApi": True},
            },
            7,
        )
    ]
    assert stub.notifications == [("initialized", {})]


def test_codex_thread_and_turn_param_builders():
    assert build_thread_start_params(cwd="/tmp/project", model="gpt-5.5") == {
        "experimentalRawEvents": False,
        "cwd": "/tmp/project",
        "model": "gpt-5.5",
    }
    assert build_text_user_input("hello") == {"type": "text", "text": "hello"}
    assert build_image_user_input("https://example.com/a.png", detail="high") == {
        "type": "image",
        "url": "https://example.com/a.png",
        "detail": "high",
    }
    assert build_local_image_user_input("/tmp/a.png") == {"type": "localImage", "path": "/tmp/a.png"}
    assert build_turn_start_params(thread_id="thread-1", text="hello", cwd="/tmp/project") == {
        "threadId": "thread-1",
        "input": [{"type": "text", "text": "hello"}],
        "cwd": "/tmp/project",
    }
    assert build_turn_start_params(thread_id="thread-1", input_items=[{"type": "localImage", "path": "/tmp/a.png"}]) == {
        "threadId": "thread-1",
        "input": [{"type": "localImage", "path": "/tmp/a.png"}],
    }
    assert build_turn_steer_params(thread_id="thread-1", expected_turn_id="turn-1", text="steer") == {
        "threadId": "thread-1",
        "expectedTurnId": "turn-1",
        "input": [{"type": "text", "text": "steer"}],
    }


def test_extract_thread_and_turn_ids_from_app_server_shapes():
    assert extract_thread_id({"thread": {"id": "thread-1"}}) == "thread-1"
    assert extract_thread_id({"threadId": "thread-2"}) == "thread-2"
    assert extract_turn_id({"turn": {"id": "turn-1"}}) == "turn-1"
    assert extract_turn_id({"turnId": "turn-2"}) == "turn-2"


def test_codex_run_text_prompt_collects_agent_deltas():
    class FakePromptTransport(CodexAppServerTransport):
        def on_message(self, cb):
            self.cb = cb

        def start_thread(self, params, timeout=60.0):
            self.thread_params = params
            return {"thread": {"id": "thread-1"}}

        def start_turn(self, params, timeout=900.0):
            self.turn_params = params
            self.cb({"method": "item/agentMessage/delta", "params": {"delta": "HELLO"}})
            self.cb({"method": "item/agentMessage/delta", "params": {"delta": "_CODEX"}})
            self.cb({"method": "item/completed", "params": {"itemId": "item-1"}})
            self.cb({"method": "thread/tokenUsage/updated", "params": {"totalTokens": 10}})
            self.cb({"method": "turn/completed", "params": {"turn": {"id": "turn-1", "status": "completed"}}})
            return {"turn": {"id": "turn-1"}}

    transport = FakePromptTransport()
    result = transport.run_text_prompt(text="hello", cwd="/tmp/project", model="gpt-5.5", timeout=1)

    assert result.status == "completed"
    assert result.thread_id == "thread-1"
    assert result.turn_id == "turn-1"
    assert result.message == "HELLO_CODEX"
    assert result.item_completed_count == 1
    assert result.token_usage == {"totalTokens": 10}
    assert result.method_counts["item/agentMessage/delta"] == 2
    assert result.recent_events[-1]["method"] == "turn/completed"


def test_codex_session_tracks_turn_messages_and_pending_requests():
    transport = CodexAppServerTransport()
    codex_session = CodexSession(
        session_id="thread-1",
        transport=transport,
        cwd="/tmp/project",
        model="gpt-5.5",
        started_at="2026-05-28T00:00:00+00:00",
    )

    codex_session.handle_message({"method": "turn/started", "params": {"turnId": "turn-1"}})
    codex_session.handle_message({"method": "item/agentMessage/delta", "params": {"delta": "Hello"}})
    codex_session.handle_message(
        {
            "id": "req-1",
            "method": "item/tool/requestUserInput",
            "params": {"questions": [{"id": "q1"}]},
        }
    )

    status = codex_session.to_status_dict()
    assert status["status"] == "waiting_input"
    assert status["current_turn_id"] == "turn-1"
    assert status["message"] == "Hello"
    assert status["pending_requests"][0]["id"] == "req-1"
    assert status["recent_events"][-1]["method"] == "item/tool/requestUserInput"
