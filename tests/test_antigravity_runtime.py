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


@pytest.mark.parametrize("message,expected", [
    (
        'permission check failed for unsandboxed "grep -n -A30 -i \\"T59\\" /workspace/x.md | head -80": '
        'Permission denied for unsandboxed(grep -n -A30 -i "T59" /workspace/x.md | head -80). '
        "Matches user-configured deny rule.",
        True,
    ),
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
