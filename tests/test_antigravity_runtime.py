import json
import os
from pathlib import Path
import sys
import threading
import time

import pytest

from asterun.backends import antigravity_runtime as module
from asterun.backends.antigravity_runtime import AntigravityRuntime, DISPATCH_RECEIPT_KEY
from asterun.ids import RunId

FIXTURE = Path(__file__).parent / "fixtures" / "antigravity_cli.py"


def execute(tmp_path, mode="happy", *, runtime=None, stopping=None, publish=None,
            text="private user prompt", expected_model=None):
    runtime = runtime or AntigravityRuntime(timeout=3)
    updates = []
    result = runtime.run(RunId("run_fixture"), text, command=[sys.executable, str(FIXTURE)],
                         env={"AGY_FIXTURE_MODE": mode, "AGY_FIXTURE_READY": str(tmp_path / "ready")},
                         cwd=tmp_path, publish=publish or updates.append,
                         stopping=stopping or threading.Event(), expected_model=expected_model)
    return result, updates


@pytest.mark.parametrize("mode", ["happy", "before_input", "benign_stderr"])
def test_native_success_after_input_and_stdin_eof(tmp_path, mode):
    result, updates = execute(tmp_path, mode)
    assert result["status"] == "succeeded" and result["terminated"] is True
    native = result["native"]
    assert native["backend_session_id"] == "fixture-conversation-123"
    assert "turn_id" not in native
    assert native["result_status"] == "SUCCESS"
    assert native["usage_scope"] == "session"
    assert native["usage"]["total_tokens"] == 10
    assert native["init_evidence"] == {"cwd": str(tmp_path.resolve()), "model": "fixture-model", "permission_mode": "request-review"}
    assert native["num_turns"] == 1
    assert native[DISPATCH_RECEIPT_KEY]["process_exit_code"] == 0
    assert native["policy_denied_count"] == 0
    assert native["review_denied_count"] == 0
    assert native["policy_denied_tools"] == []
    assert native["soft_denied_tools"] == []
    assert native[DISPATCH_RECEIPT_KEY]["process_started"] is True
    assert any(update["native"][DISPATCH_RECEIPT_KEY]["prompt_attempted"] for update in updates)
    assert len(updates) <= 3
    public = json.dumps([result, updates])
    for secret in ("private user prompt", "private reasoning", "private content", "private diagnostic"):
        assert secret not in public


@pytest.mark.parametrize("mode", ["error", "INVALID", "tool_error", "soft_deny", "split_soft_deny", "success_error",
                                  "tool_state_error", "tool_state_error_no_info", "response_state_error", "checkpoint_state_error"])
def test_errors_and_soft_denial_cannot_report_success(tmp_path, mode):
    result, _ = execute(tmp_path, mode)
    assert result["status"] == "failed" and result["terminated"] is True
    assert result["native"]["backend_session_id"] == "fixture-conversation-123"
    assert "private diagnostic" not in json.dumps(result)
    if mode in {"tool_error", "tool_state_error", "tool_state_error_no_info"}:
        assert result["native"]["tool_error_count"] == 1
    if "state_error" in mode:
        assert result["native"]["step_error_count"] == 1
        assert result["native"]["result_status"] == "SUCCESS"
        assert result["native"][DISPATCH_RECEIPT_KEY]["process_exit_code"] == 0
    if "soft_deny" in mode:
        assert result["native"]["soft_denied"] is True
        assert result["error_code"] == "ANTIGRAVITY_PERMISSION_REVIEW_REQUIRED"
    elif mode in {"error", "INVALID"}:
        assert result["error_code"] == "ANTIGRAVITY_RUN_FAILED"
    else:
        assert result["error_code"] == "ANTIGRAVITY_RUN_INCOMPLETE"


def test_os_permission_denied_without_review_evidence_is_incomplete(tmp_path):
    result, _ = execute(tmp_path, "stderr_eacces")
    assert result["status"] == "failed" and result["terminated"] is True
    assert result["error_code"] == "ANTIGRAVITY_RUN_INCOMPLETE"
    assert result["native"]["soft_denied"] is True
    assert result["summary"] == "fixture response"


def test_pre_session_native_error_can_be_confirmed(tmp_path):
    result, _ = execute(tmp_path, "error_no_id")
    assert result["status"] == "failed"
    assert "backend_session_id" not in result["native"]
    assert result["native"][DISPATCH_RECEIPT_KEY]["process_exit_code"] == 2


@pytest.mark.parametrize("mode,reason", [
    ("malformed", "malformed_json"), ("eof", "missing_result"),
    ("result_only", "missing_init"),
    ("oversize", "frame_limit"), ("stderr_flood", "stderr_limit"),
    ("stdout_flood", "stdout_limit"), ("event_flood", "event_limit"),
    ("unterminated", "unterminated_frame"), ("duplicate_json", "duplicate_json_key"),
    ("id_mismatch", "conversation_id_mismatch"), ("step_mismatch", "conversation_id_mismatch"),
    ("double_init", "invalid_init"), ("unknown_event", "unknown_event"),
    ("bad_usage", "invalid_usage"), ("bad_counter", "invalid_result_counter"),
    ("duplicate_result", "event_after_result"), ("double_done", "step_after_done"),
    ("event_after_error", "step_after_error"),
    ("active_tool", "contradictory_result"), ("active_response", "contradictory_result"),
    ("changed_step", "step_type_changed"), ("retyped_tool", "step_type_changed"),
    ("renamed_subagent", "step_type_changed"), ("tool_renamed", "step_tool_changed"),
    ("success_nonzero", "contradictory_result"),
    ("cwd_mismatch", "workspace_mismatch"), ("unsafe_permission", "unsafe_permission_mode"),
    ("unknown_permission", "unsafe_permission_mode"), ("missing_permission", "unsafe_permission_mode"),
    ("strict_permission", "unsafe_permission_mode"), ("yolo_permission", "unsafe_permission_mode"),
    ("WAITING", "nonterminal_result"), ("RUNNING", "nonterminal_result"), ("FUTURE", "nonterminal_result"),
])
def test_lost_or_ambiguous_result_preserves_native_reference(tmp_path, mode, reason):
    result, _ = execute(tmp_path, mode)
    assert result["status"] == "pending_reconcile" and result["terminated"] is False
    assert result["error_code"] == "REMOTE_STATE_UNKNOWN"
    assert result["native"]["backend_session_id"] == "fixture-conversation-123"
    receipt = result["native"][DISPATCH_RECEIPT_KEY]
    assert receipt["prompt_attempted"] is True
    assert receipt["failure_type"] == reason


@pytest.mark.parametrize("mode,model", [("happy", "fixture-model"), ("missing_model", None)])
def test_optional_init_model_and_explicit_model_match(tmp_path, mode, model):
    result, _ = execute(tmp_path, mode, expected_model=model)
    assert result["status"] == "succeeded"


@pytest.mark.parametrize("mode,model", [("happy", "another-model"), ("missing_model", "fixture-model")])
def test_missing_or_different_required_model_preserves_unknown_reference(tmp_path, mode, model):
    result, _ = execute(tmp_path, mode, expected_model=model)
    assert result["status"] == "pending_reconcile" and result["terminated"] is False
    assert result["native"]["backend_session_id"] == "fixture-conversation-123"
    assert result["native"][DISPATCH_RECEIPT_KEY]["failure_type"] == "model_mismatch"


def test_spawn_failure_proves_prompt_not_sent(tmp_path):
    runtime = AntigravityRuntime()
    result = runtime.run(RunId("missing"), "private prompt", command=[str(tmp_path / "not-found")],
                         env={}, cwd=tmp_path, publish=lambda _: None, stopping=threading.Event())
    assert result["status"] == "failed" and result["terminated"] is True
    receipt = result["native"][DISPATCH_RECEIPT_KEY]
    assert receipt["prompt_attempted"] is False and receipt["delivery"] == "not_sent"


def test_cancel_before_spawn_is_recorded(tmp_path):
    runtime = AntigravityRuntime()
    assert runtime.cancel(RunId("run_fixture")) == {"cancel_requested": True, "terminated": False}
    result, updates = execute(tmp_path, runtime=runtime)
    assert result["status"] == "cancelled" and result["terminated"] is True
    receipt = result["native"][DISPATCH_RECEIPT_KEY]
    assert receipt["stage"] == "preflight"
    assert receipt["failure_type"] == "cancelled_before_spawn"
    assert receipt["prompt_attempted"] is False
    assert receipt["delivery"] == "not_sent"
    assert receipt["process_exit_code"] is None
    assert receipt["process_started"] is False
    assert "backend_session_id" not in result["native"]
    assert not updates


def _start_active(tmp_path, mode, runtime, stopping):
    holder = {}
    worker = threading.Thread(target=lambda: holder.update(result=execute(tmp_path, mode, runtime=runtime, stopping=stopping)[0]))
    worker.start()
    deadline = time.monotonic() + 3
    while not (tmp_path / "ready").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert (tmp_path / "ready").exists()
    return worker, holder, int((tmp_path / "ready").read_text())


@pytest.mark.parametrize("mode,status", [("cancel_ack", "cancelled"), ("cancel_nack", "pending_reconcile")])
def test_cancel_requires_native_ack_and_process_exit(tmp_path, monkeypatch, mode, status):
    monkeypatch.setattr(module, "CANCEL_GRACE", 0.1)
    runtime, stopping = AntigravityRuntime(timeout=3), threading.Event()
    worker, holder, pid = _start_active(tmp_path, mode, runtime, stopping)
    assert runtime.cancel(RunId("run_fixture"))["terminated"] is False
    worker.join(3)
    assert not worker.is_alive()
    assert holder["result"]["status"] == status
    assert holder["result"]["terminated"] is (status == "cancelled")
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.parametrize("action", ["stopping", "close"])
def test_core_stop_cleans_process_without_forging_cancel(tmp_path, action):
    runtime, stopping = AntigravityRuntime(timeout=3), threading.Event()
    worker, holder, pid = _start_active(tmp_path, "core_stop", runtime, stopping)
    stopping.set() if action == "stopping" else runtime.close()
    worker.join(3)
    assert not worker.is_alive()
    assert holder["result"]["status"] == "pending_reconcile"
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.parametrize("mode,text", [("timeout", "private prompt"), ("no_input", "x" * (512 * 1024))])
def test_timeout_bounds_missing_result_and_blocked_input(tmp_path, mode, text):
    started = time.monotonic()
    result, _ = execute(tmp_path, mode, runtime=AntigravityRuntime(timeout=0.15), text=text)
    assert time.monotonic() - started < 2
    assert result["status"] == "pending_reconcile"
    assert result["native"][DISPATCH_RECEIPT_KEY]["failure_type"] == "timeout"


def test_failed_persistence_ack_never_sends_prompt(tmp_path):
    def broken_publish(_result):
        raise RuntimeError("private persistence error")
    result, _ = execute(tmp_path, "timeout", publish=broken_publish)
    assert result["status"] == "pending_reconcile"
    assert not (tmp_path / "ready").exists()
    assert "private persistence error" not in json.dumps(result)


def test_native_interrupt_without_process_exit_is_not_confirmed(tmp_path):
    result, _ = execute(tmp_path, "result_no_exit", runtime=AntigravityRuntime(timeout=0.15))
    assert result["status"] == "pending_reconcile" and result["terminated"] is False
    assert result["native"]["result_status"] == "INTERRUPTED"
    assert result["native"][DISPATCH_RECEIPT_KEY]["failure_type"] == "timeout"


def test_summary_is_bounded_in_bytes(tmp_path):
    result, _ = execute(tmp_path, "large_response")
    assert result["status"] == "succeeded"
    assert len(result["summary"].encode("utf-8")) <= module.MAX_SUMMARY_BYTES
    assert result["native"]["summary_truncated"] is True
    assert len(json.dumps(result).encode("utf-8")) < 256 * 1024


def test_already_stopped_does_not_spawn(tmp_path):
    stopping = threading.Event()
    stopping.set()
    result, updates = execute(tmp_path, stopping=stopping)
    assert result["status"] == "pending_reconcile" and not updates
    assert result["native"][DISPATCH_RECEIPT_KEY]["prompt_attempted"] is False


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True])
def test_timeout_must_be_finite_positive(timeout):
    with pytest.raises(ValueError):
        AntigravityRuntime(timeout=timeout)


def test_subagent_relabel_is_accepted_but_never_reported_as_success(tmp_path):
    result, _ = execute(tmp_path, "subagent")
    assert result["status"] == "failed" and result["terminated"] is True
    assert result["error_code"] == "ANTIGRAVITY_RUN_INCOMPLETE"
    assert result["native"]["subagent_count"] == 1
    assert result["native"][DISPATCH_RECEIPT_KEY]["failure_type"] == "subagent_delegated"


def test_plain_success_reports_no_subagents(tmp_path):
    result, _ = execute(tmp_path, "happy")
    assert result["status"] == "succeeded" and result["native"]["subagent_count"] == 0


def test_subagent_that_outlives_the_result_is_stopped_promptly(tmp_path):
    started = time.monotonic()
    result, _ = execute(tmp_path, "subagent_lingers")
    assert time.monotonic() - started < 2.5
    assert result["status"] == "failed" and result["terminated"] is True
    assert result["error_code"] == "ANTIGRAVITY_RUN_INCOMPLETE"
    assert result["native"]["subagent_count"] == 1


@pytest.mark.parametrize("mode", ["subagent_no_result", "subagent_step_only", "subagent_lingers"])
def test_any_subagent_signal_ends_the_run_within_seconds(tmp_path, mode):
    runtime = AntigravityRuntime(timeout=20)
    started = time.monotonic()
    result, _ = execute(tmp_path, mode, runtime=runtime)
    assert time.monotonic() - started < 3
    assert result["status"] == "failed" and result["terminated"] is True
    assert result["error_code"] == "ANTIGRAVITY_RUN_INCOMPLETE"
    assert result["native"]["subagent_count"] == 1
    receipt = result["native"][DISPATCH_RECEIPT_KEY]
    assert receipt["failure_type"] == "subagent_delegated"
    assert receipt["process_exit_code"] is not None


def test_subagent_stop_kills_the_whole_process_group(tmp_path):
    result, _ = execute(tmp_path, "subagent_with_child", runtime=AntigravityRuntime(timeout=20))
    assert result["status"] == "failed" and result["native"][DISPATCH_RECEIPT_KEY]["failure_type"] == "subagent_delegated"
    child = int((tmp_path / "ready").read_text())
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("grandchild survived the subagent stop")


def test_unconfirmed_group_exit_stays_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(AntigravityRuntime, "_group_gone", staticmethod(lambda proc: False))
    result, _ = execute(tmp_path, "subagent_no_result", runtime=AntigravityRuntime(timeout=20))
    assert result["status"] == "pending_reconcile"
    assert result["native"][DISPATCH_RECEIPT_KEY]["failure_type"] == "subagent_process_alive"


def test_confirmed_subagent_stop_signals_the_group_only_once(tmp_path, monkeypatch):
    calls = []
    real = AntigravityRuntime._cleanup.__func__
    monkeypatch.setattr(AntigravityRuntime, "_cleanup", classmethod(lambda cls, proc: calls.append(proc.pid) or real(cls, proc)))
    result, _ = execute(tmp_path, "subagent_no_result", runtime=AntigravityRuntime(timeout=20))
    assert result["native"][DISPATCH_RECEIPT_KEY]["failure_type"] == "subagent_delegated"
    assert len(calls) == 1


def test_runtime_without_group_ownership_never_confirms_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_OWNS_PROCESS_GROUP", False)
    result, _ = execute(tmp_path, "subagent_no_result", runtime=AntigravityRuntime(timeout=20))
    assert result["status"] == "pending_reconcile"
    assert result["native"][DISPATCH_RECEIPT_KEY]["failure_type"] == "subagent_process_alive"


def test_policy_hard_denial_with_final_answer_succeeds(tmp_path):
    result, _ = execute(tmp_path, "policy_denied_recovered")
    assert result["status"] == "succeeded" and result["terminated"] is True
    assert "error_code" not in result
    assert result["summary"] == "final plan"
    native = result["native"]
    assert native["policy_denied_count"] == 1
    assert native["policy_denied_tools"] == ["run_command"]
    assert native["tool_error_count"] == 1
    assert native["unexplained_error_count"] == 0
    assert "private diagnostic" not in json.dumps(result)


def test_multiline_policy_denial_with_final_answer_succeeds(tmp_path):
    result, _ = execute(tmp_path, "policy_denied_multiline")
    assert result["status"] == "succeeded" and result["terminated"] is True
    assert result["summary"] == "final plan"
    assert result["native"]["policy_denied_count"] == 1
    assert result["native"]["policy_denied_tools"] == ["run_command"]


def test_policy_hard_denial_without_answer_is_incomplete(tmp_path):
    result, _ = execute(tmp_path, "policy_denied_no_answer")
    assert result["status"] == "failed" and result["terminated"] is True
    assert result["error_code"] == "ANTIGRAVITY_RUN_INCOMPLETE"
    assert result["native"][DISPATCH_RECEIPT_KEY]["failure_type"] == "no_final_response"
    assert result["summary"] == "Antigravity 的工具调用被权限策略拒绝后没有产出最终结果"
    assert "private diagnostic" not in json.dumps(result)


def test_policy_denial_plus_other_tool_error_is_incomplete(tmp_path):
    result, _ = execute(tmp_path, "policy_denied_plus_error")
    assert result["status"] == "failed" and result["terminated"] is True
    assert result["error_code"] == "ANTIGRAVITY_RUN_INCOMPLETE"
    assert result["native"][DISPATCH_RECEIPT_KEY]["failure_type"] is None
    assert result["summary"] == "final plan"
    assert "private diagnostic" not in json.dumps(result)


def test_soft_deny_review_names_the_tool_and_keeps_partial_text(tmp_path):
    result, _ = execute(tmp_path, "soft_deny_review")
    assert result["status"] == "failed" and result["terminated"] is True
    assert result["error_code"] == "ANTIGRAVITY_PERMISSION_REVIEW_REQUIRED"
    assert result["native"]["soft_denied"] is True
    assert result["native"]["soft_denied_tools"] == ["ReadFile"]
    assert result["native"][DISPATCH_RECEIPT_KEY]["failure_type"] == "soft_denied_review"
    assert "ReadFile" in result["summary"] and "partial" in result["summary"]


def test_soft_deny_with_tool_error_stays_review_required(tmp_path):
    result, _ = execute(tmp_path, "soft_deny_with_tool_error")
    assert result["status"] == "failed" and result["terminated"] is True
    assert result["error_code"] == "ANTIGRAVITY_PERMISSION_REVIEW_REQUIRED"
    assert "另有 1 个" in result["summary"]
    assert result["summary"].index("另有 1 个") < result["summary"].index("partial")
    assert result["native"]["unexplained_error_count"] == 1
    assert result["native"][DISPATCH_RECEIPT_KEY]["failure_type"] == "soft_denied_review"
    assert "private diagnostic" not in json.dumps(result)


def test_policy_denial_scan_ignores_non_string_error_type():
    state = module._Run("run_regex")
    state.init_seen = True
    state.session_id = "fixture-conversation-123"
    full = (
        'permission check failed for unsandboxed "private diagnostic": '
        "Permission denied for unsandboxed(private diagnostic). "
        "Matches user-configured deny rule."
    )

    def tool_error(index, error, name="run_command"):
        line = json.dumps({"event": "step_update", "step_update": {
            "conversation_id": "fixture-conversation-123", "step_index": index,
            "state": "ERROR", "step_type": "tool",
            "tool_info": {"name": name, "error": error},
        }}).encode()
        assert AntigravityRuntime._event(state, line) is False

    tool_error(0, {"type": 7, "flag": True, "message": None,
                   "detail": {"message": "permission check failed"}})
    assert state.policy_denials == set()
    tool_error(1, {"type": ["TOOL_ERROR"], "message": full})
    assert state.policy_denials == set()
    tool_error(2, {"type": "TOOL_ERROR", "message": "permission check failed for unsandboxed"})
    assert state.policy_denials == set()
    tool_error(3, {"type": "TOOL_ERROR", "message": full})
    assert state.policy_denials == {3}
    assert state.policy_denied_tools == {"run_command"}
    tool_error(4, {"type": {"code": 1}, "message": "private diagnostic"})
    assert 4 not in state.policy_denials


@pytest.mark.parametrize("mode", [
    "policy_lookalike_path", "policy_denied_extra_field", "policy_denied_prefix_suffix",
])
def test_policy_lookalikes_stay_incomplete(tmp_path, mode):
    result, _ = execute(tmp_path, mode)
    assert result["status"] == "failed" and result["terminated"] is True
    assert result["error_code"] == "ANTIGRAVITY_RUN_INCOMPLETE"
    assert result["native"]["policy_denied_count"] == 0
    assert result["native"]["policy_denied_tools"] == []
    assert result["summary"] == "final plan"
    public = json.dumps(result)
    assert "permission check failed" not in public
    assert "private diagnostic" not in public
    assert "/workspace/permission check failed/input.txt" not in public


def test_unknown_tool_names_publish_only_other(tmp_path):
    secret = "PRIVATE_CUSTOMER_TOKEN_123"
    denied, _ = execute(tmp_path, "policy_denied_secret_tool")
    assert denied["status"] == "succeeded" and denied["terminated"] is True
    assert denied["native"]["policy_denied_tools"] == ["other"]
    assert secret not in json.dumps(denied)
    assert secret not in denied["summary"]
    soft, _ = execute(tmp_path, "soft_deny_secret_tool")
    assert soft["status"] == "failed" and soft["terminated"] is True
    assert soft["error_code"] == "ANTIGRAVITY_PERMISSION_REVIEW_REQUIRED"
    assert soft["native"]["soft_denied_tools"] == ["other"]
    assert "other" in soft["summary"]
    assert secret not in json.dumps(soft)
    assert secret not in soft["summary"]


def _policy_message(tool, quoted, raw):
    return (
        f'permission check failed for {tool} "{quoted}": '
        f"Permission denied for {tool}({raw}). "
        "Matches user-configured deny rule."
    )


# Real agy deny for a heredoc: the quoted side is Go %q, the parenthesised side
# is the original command with real newlines.
_MULTILINE_QUOTED = "cd /tmp && python3 - <<'EOF'\\nimport json\\nprint(\\\"x\\\")\\nEOF"
_MULTILINE_RAW = "cd /tmp && python3 - <<'EOF'\nimport json\nprint(\"x\")\nEOF"
_REAL_POLICY = (
    'permission check failed for unsandboxed "cd /tmp && python3 - <<\'EOF\'\\nimport json\\nprint(\\"x\\")\\nEOF": '
    'Permission denied for unsandboxed(cd /tmp && python3 - <<\'EOF\'\nimport json\nprint("x")\nEOF). '
    "Matches user-configured deny rule."
)


def test_go_unquote_decodes_simple_escapes_and_the_heredoc():
    body = r"\n\t\r\a\b\f\v\\" + r'\"'
    assert module._go_unquote(body) == "\n\t\r\a\b\f\v\\" + '"'
    assert _policy_message("unsandboxed", _MULTILINE_QUOTED, _MULTILINE_RAW) == _REAL_POLICY
    assert module._go_unquote(_MULTILINE_QUOTED) == _MULTILINE_RAW


def test_hex_and_octal_escapes_are_utf8_bytes_not_code_points():
    # \xc3\xa9 is the UTF-8 of é. Reading those escapes as code points yields U+00C3 U+00A9.
    assert module._go_unquote(r"\xc3\xa9") == "é"
    assert module._go_unquote(r"\xc3\xa9") != "\u00c3\u00a9"
    assert module._go_unquote(r"\303\251") == "é"
    assert module._go_unquote(r"\377") is None
    assert module._go_unquote(r"\xff") is None
    matched = {"type": "TOOL_ERROR", "message": _policy_message("unsandboxed", r"\xc3\xa9", "é")}
    mojibake = {"type": "TOOL_ERROR", "message": _policy_message("unsandboxed", r"\xc3\xa9", "\u00c3\u00a9")}
    assert module._is_policy_denial(matched) is True
    assert module._is_policy_denial(mojibake) is False
    assert module._is_review_denial(
        {"type": "TOOL_ERROR", "message": _review_sentence("read_file", r"\xc3\xa9", "é")}) == "read_file"
    assert module._is_review_denial(
        {"type": "TOOL_ERROR", "message": _review_sentence("read_file", r"\xc3\xa9", "\u00c3\u00a9")}) is None


@pytest.mark.parametrize("body,expected", [
    (r"\x41\u0042\U00000043", "ABC"),
    (r"\101\040", "A "),
    (r"\000", "\x00"),
    (r"\377", None),
    (r"\xff", None),
    (r"\xc3\xa9", "é"),
    (r"\303\251", "é"),
    (r"\xc3\xbf", "\u00ff"),
    (r"\u00e9", "é"),
    (r"\xc3\x28", None),
    ("é", "é"),
    (r"\1234", "S4"),
    (r"\x00", "\x00"),
    (r"\uD7FF", "\uD7FF"),
    (r"\uE000", "\uE000"),
    (r"\U0010FFFF", "\U0010FFFF"),
    (r"\0", None),
    (r"\00", None),
    (r"\7", None),
    (r"\08", None),
    (r"\12", None),
    (r"\78", None),
    (r"\128", None),
    (r"\008", None),
    (r"\777", None),
    (r"\8", None),
    (r"\9", None),
    (r"\q", None),
    ("\n", None),
    ("\r", None),
    ('"', None),
    ("\\", None),
    ("\\\n", None),
    (r"\x", None),
    (r"\x4", None),
    (r"\xGG", None),
    (r"\x-1", None),
    (r"\x+1", None),
    (r"\x0_", None),
    (r"\x_1", None),
    (r"\u041", None),
    (r"\u+041", None),
    (r"\u0_41", None),
    (r"\u00_1", None),
    (r"\u-041", None),
    (r"\U0010FFF", None),
    (r"\U+0000041", None),
    (r"\U-0000041", None),
    (r"\uD800", None),
    (r"\ud800", None),
    (r"\uDFFF", None),
    (r"\U0000D800", None),
    (r"\U0000DFFF", None),
    (r"\U00110000", None),
    (r"\UFFFFFFFF", None),
    (r"\400", None),
    ("a" * 16384, "a" * 16384),
    ("a" * 16385, None),
    (None, None),
])
def test_go_unquote_decodes_percent_q_body(body, expected):
    assert module._go_unquote(body) == expected


@pytest.mark.parametrize("message,expected", [
    (
        'permission check failed for unsandboxed "grep -n -A30 -i \\"T59\\" /workspace/x.md | head -80": '
        'Permission denied for unsandboxed(grep -n -A30 -i "T59" /workspace/x.md | head -80). '
        "Matches user-configured deny rule.",
        True,
    ),
    (_REAL_POLICY, True),
    (_policy_message("unsandboxed", _MULTILINE_QUOTED, _MULTILINE_RAW.replace('print("x")', 'print("y")', 1)), False),
    (_policy_message("unsandboxed", _MULTILINE_QUOTED, _MULTILINE_RAW + "\nextra"), False),
    (
        _policy_message(
            "unsandboxed",
            _MULTILINE_QUOTED,
            _MULTILINE_RAW + "). Matches user-configured deny rule. more",
        ),
        False,
    ),
    (_policy_message("unsandboxed", "ls\\q", "ls\\q"), False),
    (
        'permission check failed for unsandboxed "ls": Permission denied for unsandboxed(ls). '
        "Matches user-configured deny rule.",
        True,
    ),
    (
        'permission check failed for unsandboxed "ls "extra": '
        'Permission denied for unsandboxed(ls "extra). Matches user-configured deny rule. '
        "Another sentence.",
        False,
    ),
    (
        'permission check failed for unsandboxed "ls\\": '
        "Permission denied for unsandboxed(ls). Matches user-configured deny rule.",
        False,
    ),
    (
        'permission check failed for unsandboxed "ls\rwhoami": '
        "Permission denied for unsandboxed(ls). Matches user-configured deny rule.",
        False,
    ),
    (
        'permission check failed for unsandboxed "ls": '
        "Permission denied for unsandboxed(ls\r\nwhoami). Matches user-configured deny rule.",
        False,
    ),
    ("File not found: /workspace/permission check failed/input.txt", False),
])
def test_is_policy_denial_requires_escaped_quotes_inside_the_command(message, expected):
    assert module._is_policy_denial({"type": "TOOL_ERROR", "message": message}) is expected


_REVIEW_SECOND = (
    "Do not attempt to circumvent this denial by rephrasing the command, "
    "using alternative tools/scripts (e.g. python, sh, curl), or accessing "
    "the same target resource. Proceed without performing this action."
)


def _review_sentence(tool, quoted, raw):
    return (
        f'permission check failed for {tool} "{quoted}": '
        f"user denied permission for {tool}({raw})\n{_REVIEW_SECOND}"
    )


_REVIEW_REAL = _review_sentence(
    "read_file", "/outside/private diagnostic.md", "/outside/private diagnostic.md",
)
_REVIEW_ESCAPED = _review_sentence(
    "command",
    'grep -n -A30 -i \\"T59\\" /workspace/x.md | head -80',
    'grep -n -A30 -i "T59" /workspace/x.md | head -80',
)


@pytest.mark.parametrize("error_info,expected", [
    ({"type": "TOOL_ERROR", "message": _REVIEW_REAL}, "read_file"),
    ({"type": "TOOL_ERROR", "message": _REVIEW_ESCAPED}, "command"),
    ({"type": "TOOL_ERROR", "message": _review_sentence("unsandboxed", _MULTILINE_QUOTED, _MULTILINE_RAW)}, "unsandboxed"),
    (
        {"type": "TOOL_ERROR", "message": _review_sentence(
            "unsandboxed", _MULTILINE_QUOTED, _MULTILINE_RAW.replace('print("x")', 'print("y")', 1))},
        None,
    ),
    ({"type": "TOOL_ERROR", "message": _review_sentence("read_file", "ls\rwhoami", "ls\rwhoami")}, None),
    ({"type": "TOOL_ERROR", "message": _REVIEW_REAL.replace("\n", "\r\n")}, None),
    ({"type": "TOOL_ERROR", "message": _REVIEW_REAL, "detail": "extra"}, None),
    ({"type": 7, "message": _REVIEW_REAL}, None),
    ({"type": "TOOL_ERROR", "message": _review_sentence("read_file", "/x", "/y")}, None),
])
def test_is_review_denial_requires_exact_sentence_and_matching_args(error_info, expected):
    assert module._is_review_denial(error_info) == expected


def test_review_denied_stream_is_review_required(tmp_path):
    result, _ = execute(tmp_path, "review_denied_stream")
    assert result["status"] == "failed" and result["terminated"] is True
    assert result["error_code"] == "ANTIGRAVITY_PERMISSION_REVIEW_REQUIRED"
    assert result["native"]["soft_denied"] is True
    assert result["native"]["soft_denied_tools"] == ["read_file"]
    assert result["native"]["review_denied_count"] == 1
    assert result["native"]["unexplained_error_count"] == 0
    assert "private diagnostic" not in json.dumps(result)


@pytest.mark.parametrize("mode", ["review_denied_forged", "review_denied_suffix"])
def test_forged_or_suffixed_review_denial_stays_incomplete(tmp_path, mode):
    result, _ = execute(tmp_path, mode)
    assert result["status"] == "failed" and result["terminated"] is True
    assert result["error_code"] == "ANTIGRAVITY_RUN_INCOMPLETE"
    assert result["native"]["review_denied_count"] == 0
    assert result["native"]["soft_denied_tools"] == []


def test_review_denial_plus_unrelated_tool_error_counts_the_other_error(tmp_path):
    result, _ = execute(tmp_path, "review_plus_tool_error")
    assert result["status"] == "failed" and result["terminated"] is True
    assert result["error_code"] == "ANTIGRAVITY_PERMISSION_REVIEW_REQUIRED"
    assert "另有 1 个" in result["summary"]
    assert result["native"]["unexplained_error_count"] == 1
    assert result["native"]["review_denied_count"] == 1
    assert "private diagnostic" not in json.dumps(result)


@pytest.mark.parametrize("quoted,raw", [
    (r"\0", "\x00"),
    (r"\08", "\x008"),
    (r"\12", "\n"),
    (r"\00", "\x00"),
    (r"\7", "\x07"),
    (r"\400", r"\400"),
    (r"\777", r"\777"),
    (r"\x-1", r"\x-1"),
    (r"\x+1", "\x01"),
    (r"\x4", "\x04"),
    (r"\u+041", "A"),
    (r"\u0_41", "A"),
    (r"\u00_1", "\x01"),
    (r"\u041", "A"),
    (r"\u-041", "\ud800"),
    (r"\U+0000041", "A"),
    (r"\U-0000041", r"\U-0000041"),
    (r"\U0010FFF", "\U00010fff"),
    (r"\uD800", "\ud800"),
    (r"\uDFFF", "\udfff"),
    (r"\U0000D800", "\ud800"),
    (r"\U00110000", r"\U00110000"),
    (r"\UFFFFFFFF", r"\UFFFFFFFF"),
])
def test_invalid_escapes_are_not_policy_or_review_denials(quoted, raw):
    policy = {"type": "TOOL_ERROR", "message": _policy_message("unsandboxed", quoted, raw)}
    review = {"type": "TOOL_ERROR", "message": _review_sentence("read_file", quoted, raw)}
    assert module._is_policy_denial(policy) is False
    assert module._is_review_denial(review) is None


@pytest.mark.parametrize("mode", ["policy_denied_bad_escape", "review_denied_bad_escape"])
def test_invalid_escape_denial_is_an_ordinary_tool_error(tmp_path, mode):
    result, _ = execute(tmp_path, mode)
    assert result["status"] == "failed" and result["terminated"] is True
    assert result["error_code"] == "ANTIGRAVITY_RUN_INCOMPLETE"
    native = result["native"]
    assert native["policy_denied_count"] == 0
    assert native["review_denied_count"] == 0
    assert native["soft_denied"] is False
    assert native["soft_denied_tools"] == []
    assert native["unexplained_error_count"] == 1
    assert "需要人工审批" not in result["summary"]
    if mode == "policy_denied_bad_escape":
        assert result["summary"] == "final plan"
    assert "private diagnostic" not in json.dumps(result)


@pytest.mark.parametrize("mode,status,code,count_key", [
    ("policy_utf8_match", "succeeded", None, "policy_denied_count"),
    ("policy_utf8_mojibake", "failed", "ANTIGRAVITY_RUN_INCOMPLETE", "policy_denied_count"),
    ("review_utf8_match", "failed", "ANTIGRAVITY_PERMISSION_REVIEW_REQUIRED", "review_denied_count"),
    ("review_utf8_mojibake", "failed", "ANTIGRAVITY_RUN_INCOMPLETE", "review_denied_count"),
])
def test_utf8_byte_escapes_match_the_character_not_its_code_points(tmp_path, mode, status, code, count_key):
    result, _ = execute(tmp_path, mode)
    assert result["status"] == status and result["terminated"] is True
    native = result["native"]
    if code is None:
        assert "error_code" not in result
        assert native[count_key] == 1
        assert native["unexplained_error_count"] == 0
    elif code == "ANTIGRAVITY_PERMISSION_REVIEW_REQUIRED":
        assert result["error_code"] == code
        assert native[count_key] == 1
        assert native["unexplained_error_count"] == 0
    else:
        assert result["error_code"] == code
        assert native["policy_denied_count"] == 0
        assert native["review_denied_count"] == 0
        assert native["unexplained_error_count"] == 1
        assert native["soft_denied"] is False
    assert "private diagnostic" not in json.dumps(result)


@pytest.mark.parametrize("mode", ["policy_active_deny_then_error", "review_active_deny_then_error"])
def test_nonterminal_deny_does_not_explain_a_later_ordinary_error(tmp_path, mode):
    result, _ = execute(tmp_path, mode)
    assert result["status"] == "failed" and result["terminated"] is True
    assert result["error_code"] == "ANTIGRAVITY_RUN_INCOMPLETE"
    native = result["native"]
    assert native["unexplained_error_count"] == 1
    assert native["policy_denied_count"] == 0
    assert native["review_denied_count"] == 0
    assert native["soft_denied"] is False
    assert native["policy_denied_tools"] == []
    assert native["soft_denied_tools"] == []
    assert "需要人工审批" not in result["summary"]
    assert "private diagnostic" not in json.dumps(result)


def test_soft_deny_and_hard_deny_in_one_run(tmp_path):
    result, _ = execute(tmp_path, "soft_and_hard_deny")
    assert result["status"] == "failed" and result["terminated"] is True
    assert result["error_code"] == "ANTIGRAVITY_PERMISSION_REVIEW_REQUIRED"
    native = result["native"]
    assert native["soft_denied"] is True
    assert native["soft_denied_tools"] == ["read_file"]
    assert native["review_denied_count"] == 1
    assert native["policy_denied_count"] == 1
    assert native["policy_denied_tools"] == ["run_command"]
    assert native["unexplained_error_count"] == 0
    assert native[DISPATCH_RECEIPT_KEY]["failure_type"] == "soft_denied_review"
    assert "另有" not in result["summary"]
    assert "read_file" in result["summary"]
    assert "final plan" in result["summary"]
    assert "private diagnostic" not in json.dumps(result)
