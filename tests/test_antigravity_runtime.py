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
    ("changed_step", "step_type_changed"), ("success_nonzero", "contradictory_result"),
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
