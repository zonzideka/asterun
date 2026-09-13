from asterun.backends.codex_protocol import (
    build_codex_decision_answer,
    build_codex_dynamic_tool_answer,
    build_codex_legacy_decision_answer,
    build_codex_mcp_elicitation_answer,
    build_codex_permissions_answer,
    build_codex_user_input_answer,
    is_codex_approval_request,
    parse_codex_user_input_request,
)


def test_parse_codex_user_input_request():
    request = {
        "id": "req-1",
        "method": "item/tool/requestUserInput",
        "params": {
            "itemId": "item-1",
            "threadId": "thread-1",
            "turnId": "turn-1",
            "questions": [{"id": "choice", "options": [{"label": "A"}]}],
        },
    }

    parsed = parse_codex_user_input_request(request)

    assert parsed is not None
    assert parsed.request_id == "req-1"
    assert parsed.tool_call_id == "item-1"
    assert parsed.questions == [{"id": "choice", "options": [{"label": "A"}]}]


def test_codex_user_input_and_approval_response_shapes():
    assert build_codex_user_input_answer({"choice": "A", "multi": ["B", "C"]}) == {
        "answers": {
            "choice": {"answers": ["A"]},
            "multi": {"answers": ["B", "C"]},
        }
    }
    assert build_codex_decision_answer("accept") == {"decision": "accept"}
    assert build_codex_permissions_answer(permissions="workspace-write", scope="thread") == {
        "permissions": "workspace-write",
        "scope": "thread",
    }
    assert build_codex_legacy_decision_answer("accept") == {"decision": "approved"}
    assert build_codex_mcp_elicitation_answer(action="accept", content={"name": "A"}) == {
        "action": "accept",
        "content": {"name": "A"},
    }
    assert build_codex_dynamic_tool_answer(text="done") == {
        "success": True,
        "contentItems": [{"type": "inputText", "text": "done"}],
    }


def test_identifies_codex_approval_requests():
    assert is_codex_approval_request({"method": "item/commandExecution/requestApproval"})
    assert is_codex_approval_request({"method": "item/fileChange/requestApproval"})
    assert is_codex_approval_request({"method": "item/permissions/requestApproval"})
    assert is_codex_approval_request({"method": "applyPatchApproval"})
    assert is_codex_approval_request({"method": "execCommandApproval"})
    assert not is_codex_approval_request({"method": "item/tool/requestUserInput"})
