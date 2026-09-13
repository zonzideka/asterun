from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from threading import Event
import time

import pytest

from asterun.backends.grok_code import RECEIPT_KEY, _tool, run_code


SESSION_ID = "bc01032f-eddd-4f49-994e-34a0f33395fd"
SCRIPT = r'''
import json, os, pathlib, subprocess, sys, time
mode, session = sys.argv[1:]
pathlib.Path("started").write_text("started")
def emit(kind, **values):
    print(json.dumps({"type": kind, **values}), flush=True)
def end(reason="end_turn", **values):
    emit("end", stopReason=reason, sessionId=session, requestId="request-1", **values)
def tool(number, name, inputs, output):
    emit("tool_call", toolCallId=str(number), toolName=name, status="in_progress", rawInput=inputs)
    emit("tool_call_update", toolCallId=str(number), status="completed", rawOutput=output)
if mode == "workflow":
    source = pathlib.Path("maths.py")
    tool(1, "read_file", {"path": "maths.py"}, {"content": source.read_text()})
    check = [sys.executable, "-c", "from maths import add; assert add(2, 3) == 5; assert add(-1, 3) == 2"]
    first = subprocess.run(check, capture_output=True)
    tool(2, "run_terminal_command", {"command": "python check.py"}, {"exit_code": first.returncode})
    source.write_text("def add(a, b): return 5\n")
    tool(3, "search_replace", {"path": "maths.py", "new_string": source.read_text()}, {"success": True})
    second = subprocess.run(check, capture_output=True)
    tool(4, "run_terminal_command", {"command": "python check.py"}, {"exit_code": second.returncode})
    source.write_text("def add(a, b): return a + b\n")
    tool(5, "search_replace", {"path": "maths.py", "new_string": source.read_text()}, {"success": True})
    third = subprocess.run(check, capture_output=True)
    tool(6, "run_terminal_command", {"command": "python check.py"}, {"exit_code": third.returncode,
        "output": list(b"two checks passed"), "private": {"secret": "UNPROJECTED"}})
    emit("thought", data="THOUGHT_MUST_NOT_SURVIVE")
    emit("text", data="Implemented and tested.")
    end(num_turns=4, usage={"input_tokens": 20, "output_tokens": 5, "thought": "UNPROJECTED"},
        modelUsage={"grok-test": {"modelCalls": 4, "costUSD": 0.1, "private": "UNPROJECTED"}},
        total_cost_usd=0.1, signature="UNPROJECTED")
elif mode == "thought":
    emit("thought", data="THOUGHT_MUST_NOT_SURVIVE", sessionId="wrong")
    emit("text", data="Public answer")
    emit("tool_call", toolCallId="1", toolName="read_file", rawInput={"path": "a", "thought": "UNPROJECTED"})
    emit("tool_call_update", toolCallId="1", content=[{"type": "content", "content": {
        "type": "text", "text": "Public output"}}, {"type": "thought", "text": "UNPROJECTED"}],
        rawOutput={"output": list(b"ok"), "exit_code": 0, "secret": "UNPROJECTED"})
    end(usage={"input_tokens": 3}, modelUsage={"grok-test": {"modelCalls": 1}},
        signature="UNPROJECTED", rawResponse={"thought": "UNPROJECTED"})
elif mode == "mismatch":
    emit("end", stopReason="end_turn", sessionId="wrong")
elif mode == "early_mismatch":
    emit("text", data="Wrong thread", sessionId="wrong")
    end()
elif mode == "missing_session":
    emit("end", stopReason="end_turn")
elif mode == "bad_stdout":
    print("not JSON", flush=True)
    end()
elif mode == "no_end":
    emit("text", data="Looks successful")
elif mode == "nonzero":
    end()
    sys.exit(1)
elif mode == "timeout":
    emit("text", data="Started")
    time.sleep(10)
elif mode == "fork_tool":
    subprocess.Popen([sys.executable, "-c", "import pathlib, time\nwhile True:\n pathlib.Path('heartbeat').write_text(str(time.monotonic()))\n time.sleep(0.01)"])
    emit("text", data="Tool running")
    time.sleep(10)
elif mode == "after_end":
    end()
    emit("text", data="Too late")
elif mode == "error":
    emit("error", message="private diagnostic UNPROJECTED", rawResponse={"thought": "UNPROJECTED"})
    end()
elif mode == "budget":
    emit("max_turns_reached")
    end()
elif mode == "incomplete_usage":
    end(cost_is_partial=True, usage_is_incomplete=True, usage={"input_tokens": 12},
        total_cost_usd=2, total_cost_usd_ticks=20000000000,
        modelUsage={"grok-test": {"modelCalls": 3, "costUSD": 2}})
elif mode == "stderr":
    sys.stderr.write("PRIVATE_STDERR" * 1000)
    sys.stderr.flush()
    emit("text", data="Public answer")
    end()
elif mode == "large":
    emit("text", data="x" * 80000)
    end()
elif mode == "fast_tail":
    for i in range(12):
        emit("text", data=str(i) + " ")
    end(num_turns=2, usage={"input_tokens": 4, "output_tokens": 12})
else:
    emit("text", data="Public answer")
    end(mode)
'''


@pytest.fixture
def invoke(tmp_path):
    script = tmp_path / "native.py"
    script.write_text(SCRIPT)
    (tmp_path / "maths.py").write_text("def add(a, b): return 0\n")

    def call(mode="end_turn", publish=None, stopping=None, timeout=5):
        return run_code(command=[sys.executable, str(script), mode, SESSION_ID],
            env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"},
            cwd=tmp_path, session_id=SESSION_ID, task_id="tsk_test", run_id="run_test",
            timeout_seconds=timeout, publish=publish, stopping=stopping or Event())

    return call


def test_native_tool_loop_edits_runs_failing_tests_and_repairs(invoke, tmp_path):
    updates = []

    def persist(update):
        if not updates:
            assert not (tmp_path / "started").exists()
            assert update["native"]["session_id"] == SESSION_ID
            receipt = update["native"][RECEIPT_KEY]
            assert receipt["stage"] == "spawn" and receipt["prompt_attempted"] is False
        updates.append(update)

    result = invoke("workflow", persist)
    assert result["status"] == "succeeded" and result["terminated"] is True
    assert result["events"] == []  # Progress was already acknowledged.
    assert result["summary"] == "Implemented and tested."
    assert (tmp_path / "maths.py").read_text() == "def add(a, b): return a + b\n"
    namespace = {}
    exec((tmp_path / "maths.py").read_text(), namespace)
    assert namespace["add"](2, 3) == 5 and namespace["add"](-1, 3) == 2
    events = [event for update in updates for event in update["events"]]
    codes = [event["output"]["exit_code"] for event in events if "exit_code" in event.get("output", {})]
    assert codes == [1, 1, 0]
    assert any(event.get("output", {}).get("output") == "two checks passed" for event in events)
    assert result["native"]["tool_calls_count"] == 6
    assert result["native"]["num_turns"] == 4
    assert result["native"]["modelUsage"] == {"grok-test": {"modelCalls": 4, "costUSD": 0.1}}
    assert all(update["status"] != "succeeded" for update in updates)
    assert "THOUGHT_MUST_NOT_SURVIVE" not in json.dumps([updates, result])
    assert "UNPROJECTED" not in json.dumps([updates, result])


def test_sync_retains_public_result_but_never_thought_or_unknown_raw_fields(invoke):
    result = invoke("thought")
    assert result["status"] == "succeeded"
    assert result["summary"] == "Public answer"
    raw = json.dumps(result)
    assert "Public output" in raw and '"output": "ok"' in raw
    assert "THOUGHT" not in raw and "UNPROJECTED" not in raw
    assert result["native"]["native_resumed"] is False
    assert result["native"]["execution_profile"] == "workspace-code-v1"
    assert result["native"]["transport"] == "headless"
    assert result["native"][RECEIPT_KEY]["delivery"] == "may_have_been_sent"


@pytest.mark.parametrize("mode,failure", [
    ("mismatch", "session_mismatch"), ("early_mismatch", "session_mismatch"),
    ("missing_session", "invalid_end_binding"), ("bad_stdout", "invalid_stdout_json"),
    ("no_end", "missing_end"), ("nonzero", "nonzero_exit"), ("after_end", "event_after_end"),
    ("unknown", "unknown_stop_reason"), ("error", "native_error"),
])
def test_uncertain_or_unbound_result_never_succeeds(invoke, mode, failure):
    result = invoke(mode)
    assert result["status"] == "pending_reconcile" and result["terminated"] is False
    assert result["error_code"] == "REMOTE_STATE_UNKNOWN"
    assert result["native"]["session_id"] == SESSION_ID
    receipt = result["native"][RECEIPT_KEY]
    assert receipt["failure_type"] == failure and receipt["prompt_attempted"] is True
    assert receipt["delivery"] == "may_have_been_sent"
    assert "UNPROJECTED" not in json.dumps(result)


@pytest.mark.parametrize("mode", ["max_tokens", "max_turn_requests", "refusal", "budget"])
def test_explicit_incomplete_stop_is_failed(invoke, mode):
    result = invoke(mode)
    assert result["status"] == "failed" and result["terminated"] is True
    assert result["error_code"] == "GROK_RUN_INCOMPLETE"


def test_native_cancel_requires_bound_end_and_zero_exit(invoke):
    result = invoke("cancelled")
    assert result["status"] == "cancelled" and result["terminated"] is True
    assert result["native"]["stop_reason"] == "cancelled"


def test_runtime_timeout_keeps_reference_and_uncertainty(invoke):
    result = invoke("timeout", timeout=0.1)
    assert result["status"] == "pending_reconcile" and result["terminated"] is False
    assert result["native"][RECEIPT_KEY]["failure_type"] == "runtime_timeout"


def test_observation_stop_does_not_claim_native_cancellation(invoke):
    stop = Event()

    def persist(update):
        if update["status"] == "running":
            stop.set()

    result = invoke("timeout", persist, stop)
    assert result["status"] == "pending_reconcile" and result["terminated"] is False
    assert result["native"][RECEIPT_KEY]["failure_type"] == "observation_stopped"


def test_native_reference_persistence_failure_prevents_spawn(invoke, tmp_path):
    def fail(_):
        raise RuntimeError("PRIVATE_FAILURE")

    result = invoke(publish=fail)
    assert not (tmp_path / "started").exists()
    assert result["status"] == "failed" and result["terminated"] is True
    assert result["native"][RECEIPT_KEY]["delivery"] == "not_sent"
    assert "PRIVATE_FAILURE" not in json.dumps(result)


def test_progress_persistence_failure_cannot_be_overwritten_by_success(invoke):
    updates = []

    def persist(update):
        updates.append(update)
        if len(updates) == 2:
            raise RuntimeError("PRIVATE_FAILURE")

    result = invoke(publish=persist)
    assert result["status"] == "pending_reconcile" and result["terminated"] is False
    assert len(updates) == 2
    assert result["native"][RECEIPT_KEY]["prompt_attempted"] is True
    assert "PRIVATE_FAILURE" not in json.dumps(result)


def test_spawn_failure_is_known_not_sent(tmp_path):
    result = run_code(command=[str(tmp_path / "missing")], env={}, cwd=tmp_path,
        session_id=SESSION_ID, task_id="tsk_test", run_id="run_test", timeout_seconds=1,
        publish=None, stopping=Event())
    assert result["status"] == "failed" and result["terminated"] is True
    assert result["native"][RECEIPT_KEY]["stage"] == "spawn"
    assert result["native"][RECEIPT_KEY]["delivery"] == "not_sent"


def test_stopping_before_spawn_does_not_start_process(invoke, tmp_path):
    stop = Event()
    stop.set()
    result = invoke(stopping=stop)
    assert not (tmp_path / "started").exists()
    assert result["native"][RECEIPT_KEY]["delivery"] == "not_sent"


def test_incomplete_native_usage_never_presents_complete_cost(invoke):
    result = invoke("incomplete_usage")
    native = result["native"]
    assert native["usage_is_incomplete"] and native["cost_is_partial"]
    assert "total_cost_usd" not in native and "total_cost_usd_ticks" not in native
    assert "costUSD" not in native["modelUsage"]["grok-test"]
    assert native["usage"] == {"input_tokens": 12}


def test_stderr_is_drained_without_persisting_private_diagnostics(invoke):
    result = invoke("stderr")
    assert result["status"] == "succeeded"
    assert "PRIVATE_STDERR" not in json.dumps(result)


def test_public_summary_size_is_bounded_and_marked(invoke):
    result = invoke("large")
    assert result["status"] == "succeeded"
    assert len(result["summary"]) == 65536
    assert result["native"]["summary_truncated"] is True


def test_oversized_stdout_line_is_unknown_not_silent_success(invoke, monkeypatch):
    monkeypatch.setattr("asterun.backends.grok_code.MAX_LINE_BYTES", 1024)
    result = invoke("large")
    assert result["status"] == "pending_reconcile"
    assert result["native"][RECEIPT_KEY]["failure_type"] == "oversized_line"


@pytest.mark.parametrize("mode,status", [("end_turn", "succeeded"), ("no_end", "pending_reconcile")])
def test_cleanup_failure_does_not_replace_received_outcome(invoke, monkeypatch, mode, status):
    def fail(_):
        raise OSError("PRIVATE_CLEANUP_FAILURE")

    monkeypatch.setattr("asterun.backends.grok_code._stop_process", fail)
    result = invoke(mode)
    assert result["status"] == status
    assert result["native"]["cleanup_incomplete"] is True
    assert "PRIVATE_CLEANUP_FAILURE" not in json.dumps(result)


def test_timeout_stops_native_child_tool_process_group(invoke, tmp_path):
    result = invoke("fork_tool", timeout=0.25)
    assert result["status"] == "pending_reconcile"
    heartbeat = (tmp_path / "heartbeat").read_text()
    time.sleep(0.05)
    assert (tmp_path / "heartbeat").read_text() == heartbeat


def test_stderr_limit_is_bounded_and_unknown(invoke, monkeypatch):
    monkeypatch.setattr("asterun.backends.grok_code.MAX_STREAM_BYTES", 1024)
    result = invoke("stderr")
    assert result["status"] == "pending_reconcile"
    assert result["native"][RECEIPT_KEY]["failure_type"] == "stderr_limit"


def test_sync_event_capture_is_bounded_with_full_summary(invoke, monkeypatch):
    monkeypatch.setattr("asterun.backends.grok_code.MAX_TOOL_EVENTS", 3)
    result = invoke("workflow")
    assert result["status"] == "succeeded"
    assert len(result["events"]) == 3
    assert result["native"]["events_truncated"] is True
    assert result["summary"] == "Implemented and tested."


def test_event_limit_is_explicit_unknown(invoke, monkeypatch):
    monkeypatch.setattr("asterun.backends.grok_code.MAX_EVENTS", 2)
    result = invoke("workflow")
    assert result["status"] == "pending_reconcile"
    assert result["native"][RECEIPT_KEY]["failure_type"] == "event_limit"


def test_slow_host_acknowledgements_do_not_discard_a_completed_native_tail(invoke):
    def persist(update):
        if any(event.get("kind") == "text" for event in update["events"]):
            time.sleep(0.12)

    started = time.monotonic()
    result = invoke("fast_tail", publish=persist)
    assert time.monotonic() - started > 1
    assert result["status"] == "succeeded"
    assert result["summary"] == "".join(str(i) + " " for i in range(12))
    assert result["native"]["stop_reason"] == "end_turn"
    assert result["native"]["usage"] == {"input_tokens": 4, "output_tokens": 12}


@pytest.mark.parametrize("event_type", ["tool_call", "tool_call_update"])
@pytest.mark.parametrize("tool,key,path", [
    ("read_file", "target_file", "src/module.py"),
    ("list_dir", "target_directory", "src"),
])
def test_native_file_tool_paths_survive_public_event_projection(event_type, tool, key, path):
    event = {"type": event_type, "toolCallId": "native-tool", "toolName": tool,
             "rawInput": {key: path, "variant": "native-detail", "_meta": {"private": "hidden"}}}
    projected = _tool(event)
    assert projected["input"] == {key: path}
    assert "hidden" not in json.dumps(projected)
