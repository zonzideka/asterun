from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from asterun import observe
from asterun.contracts import Envelope
from asterun.errors import AsterunError, BACKEND_UNAVAILABLE, INVALID_REQUEST, REMOTE_STATE_UNKNOWN


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        now = 0.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    value = Clock()
    monkeypatch.setattr(observe.time, "monotonic", value.monotonic)
    monkeypatch.setattr(observe.time, "sleep", value.sleep)
    return value


def snapshot(status="running", run_id="run_1"):
    return {
        "task": {"id": "tsk_1", "current_run_id": run_id, "text": "完整任务正文" * 4000,
                 "acceptance": "pending", "delivery": "not_configured", "evidence_stale": True,
                 "review_status": "not_configured", "findings": [{"body": "完整finding正文" * 2000}]},
        "run": {"id": run_id, "task_id": "tsk_1", "status": status, "summary": "观察摘要" * 1500,
                "prompt": "完整提示词" * 4000, "terminated": status == "cancelled",
                "cancel_requested": True, "error_code": "原有错误", "output": {"body": "完整结果" * 4000},
                "native": {"session_id": "native_1", "native_resumed": False, "stop_reason": "end_turn",
                           "usage": {"input_tokens": 50, "output_tokens": 25, "reasoning_tokens": 10},
                           "modelUsage": {"model": {"inputTokens": 50, "outputTokens": 25}},
                           "usage_source": "native_headless_report", "usage_is_incomplete": True,
                           "cost_is_partial": True, "total_cost_usd": 0.05,
                           "token_usage": {"threadId": "thread_1", "turnId": "turn_1",
                                           "tokenUsage": {"total": {"totalTokens": 800}}},
                           "usage_scope": "thread_cumulative", "recent_events": ["原生日志" * 2000]}},
        "session": {"conversation_id": "con_1", "backend_session_id": "native_1", "latest_turn_id": "turn_1"},
        "approval": None,
        "notes": {"run_success_is_not_acceptance": True, "fake_is_not_real_connection": True},
    }


def event_page(cursor=0, run_id="run_1"):
    return {"task_id": "tsk_1", "run_id": run_id, "cursor": cursor, "next_cursor": cursor + 1,
            "events": [{"seq": cursor + 1, "type": "message", "source": "backend.fake",
                        "payload": {"kind": "tool_call", "toolName": "read_file", "toolCallId": "call_1",
                                    "status": "completed", "text": "工具正文" * 5000,
                                    "input": {"path": "私有输入内容"}}}]}


def test_compact_snapshot_is_short_preserves_recovery_and_usage_without_mutation():
    original = snapshot("cancelled")
    before = deepcopy(original)
    result = observe.compact_task_snapshot(original)
    assert len(json.dumps(result, ensure_ascii=False)) < len(json.dumps(original, ensure_ascii=False)) / 10
    assert result["run"]["status"] == "cancelled"
    assert result["run"]["terminated"] is True and result["run"]["cancel_requested"] is True
    assert result["run"]["error_code"] == "原有错误"
    assert result["task"]["acceptance"] == "pending" and result["task"]["evidence_stale"] is True
    assert result["task"]["findings_count"] == 1
    assert result["notes"]["run_success_is_not_acceptance"] is True
    assert result["session"]["backend_session_id"] == "native_1"
    assert result["run"]["native"]["usage"] == original["run"]["native"]["usage"]
    assert result["run"]["native"]["usage_is_incomplete"] is True
    assert result["run"]["native"]["cost_is_partial"] is True
    assert result["run"]["native"]["token_usage"] == original["run"]["native"]["token_usage"]
    assert result["run"]["native"]["usage_scope"] == "thread_cumulative"
    assert result["run"]["output_available"] is True
    assert result["run"]["summary_truncated"] is True
    assert result["run"]["summary_chars"] == len(original["run"]["summary"])
    assert len(result["run"]["summary"]) == observe.COMPACT_SUMMARY_CHARS
    assert result["details"] == {"method": "task.get", "compact": False, "task_id": "tsk_1"}
    assert not any(text in json.dumps(result, ensure_ascii=False)
                   for text in ["完整任务正文", "完整提示词", "完整finding正文", "完整结果", "原生日志"])
    assert observe.compact_task_snapshot(result) == result
    result["run"]["native"]["usage"]["input_tokens"] = 999
    assert original == before


def test_compact_does_not_turn_missing_run_into_an_observable_state():
    assert observe.compact_task_snapshot({"task": {"id": "tsk_1"}, "run": None})["run"] is None
    assert "run" not in observe.compact_task_snapshot({"task": {"id": "tsk_1"}})


@pytest.mark.parametrize(("status", "reason"), [
    ("succeeded", "terminal"), ("failed", "terminal"), ("cancelled", "terminal"),
    ("waiting_input", "waiting_input"), ("waiting_auth", "waiting_auth"), ("paused", "paused"),
    ("pending_reconcile", "unknown"), ("future_unknown", "unknown"),
])
def test_no_events_keeps_attention_semantics_and_does_not_consume_cursor(monkeypatch, clock, status, reason):
    calls = []

    def read(path, method, payload, deadline):
        calls.append((method, payload))
        assert method == "task.get"
        return Envelope(ok=True, data=snapshot(status))

    monkeypatch.setattr(observe, "_read", read)
    result = observe.watch_task(Path("unused.sock"), "tsk_1", cursor=15, run_id="run_1",
                                compact=True, include_events=False,
                                on_events=lambda page: pytest.fail("不能交付未读取事件"))
    assert result.data["reason"] == reason
    assert result.ok is (reason != "unknown")
    if reason == "unknown":
        assert result.error["code"] == REMOTE_STATE_UNKNOWN
    assert result.data["cursor"] == 15 and result.data["run_id"] == "run_1"
    assert result.data["events_skipped"] is True and result.data["readonly"] is True
    assert result.data["requests"] == 1 and len(calls) == 1
    assert calls[0][1] == {"task_id": "tsk_1", "compact": True}


@pytest.mark.parametrize(("state", "reason"), [("pending", "waiting_input"), ("forwarding", "unknown")])
def test_compact_approval_preserves_binding_and_requires_full_details(monkeypatch, clock, state, reason):
    value = snapshot()
    value["approval"] = {"id": "apr_1", "task_id": "tsk_1", "run_id": "run_1", "state": state,
                         "target_hash": "digest", "native_confirmed": False, "response_status": "not_sent",
                         "summary": "审批摘要" * 300, "native_request": {"command": "完整审批操作" * 1000}}
    monkeypatch.setattr(observe, "_read", lambda *args: Envelope(ok=True, data=value))
    result = observe.watch_task(Path("unused.sock"), "tsk_1", compact=True, include_events=False)
    approval = result.data["last_snapshot"]["approval"]
    assert result.data["reason"] == reason
    assert approval["id"] == "apr_1" and approval["run_id"] == "run_1"
    assert approval["target_hash"] == "digest" and approval["native_confirmed"] is False
    assert approval["state"] == state and approval["summary_truncated"] is True
    assert "native_request" not in approval


def test_no_events_resets_cursor_only_when_task_changes_run(monkeypatch, clock):
    responses = [snapshot(), snapshot("succeeded", "run_2")]
    monkeypatch.setattr(observe, "_read", lambda *args: Envelope(ok=True, data=responses.pop(0)))
    result = observe.watch_task(Path("unused.sock"), "tsk_1", cursor=15, run_id="run_1", include_events=False)
    assert result.data["reason"] == "terminal"
    assert result.data["run_id"] == "run_2" and result.data["cursor"] == 0
    assert result.data["requests"] == 2


def test_compact_event_pages_omit_native_text_but_keep_resumable_cursor(monkeypatch, clock):
    page = event_page(3)
    before = deepcopy(page)
    def read(path, method, payload, deadline):
        assert payload["compact"] is True
        return Envelope(ok=True, data=snapshot("succeeded") if method == "task.get" else page)

    monkeypatch.setattr(observe, "_read", read)
    emitted = []
    result = observe.watch_task(Path("unused.sock"), "tsk_1", cursor=3, run_id="run_1",
                                compact=True, on_events=emitted.append)
    assert result.data["cursor"] == 4
    assert emitted[0]["next_cursor"] == 4 and emitted[0]["events"][0]["seq"] == 4
    assert emitted[0]["events"][0]["payload"]["toolCallId"] == "call_1"
    assert "text" not in emitted[0]["events"][0]["payload"]
    assert "input" not in emitted[0]["events"][0]["payload"]
    assert page == before


def test_compact_meter_projection_is_byte_bounded_and_idempotent():
    original = snapshot("pending_reconcile")
    native = original["run"]["native"]
    native["usage"]["unknown_vendor_payload"] = "大" * 350000
    native["usage"]["output_tokens_details"] = {"reasoning_tokens": 12, "raw": "x" * 50000}
    native["token_usage"]["unrecognized_notification"] = "x" * 1000000
    native["modelUsage"] = {("model-" + str(i) + "x" * 700): {"inputTokens": i, "raw": "x" * 20000}
                            for i in range(40)}
    native["execution_profile"] = "x" * 20000
    native["cost_reported"] = False
    native["cost_source"] = "unknown"
    native.update(mode="claude", resume_supported=False, claude_session_id="original-native-ref")
    result = observe.compact_task_snapshot(original)
    short = result["run"]["native"]
    assert len(json.dumps(result, ensure_ascii=False).encode()) < 32000
    assert short["usage"]["input_tokens"] == 50
    assert short["usage"]["output_tokens_details"] == {"reasoning_tokens": 12}
    assert short["token_usage"]["tokenUsage"]["total"]["totalTokens"] == 800
    assert short["usage_truncated"] is True
    assert short["model_usage_count"] == 40
    assert 0 < short["model_usage_returned"] <= observe.COMPACT_MODEL_LIMIT
    meter = {name: value for name, value in short.items() if name in {
        "usage", "token_usage", "modelUsage", "model_usage_count", "model_usage_returned",
        "usage_truncated", "usage_byte_limit", "model_usage_limit"}}
    assert len(json.dumps(meter, ensure_ascii=False).encode()) <= observe.COMPACT_USAGE_BYTES
    assert "execution_profile" in short["omitted_fields"]
    assert short["cost_reported"] is False and short["cost_source"] == "unknown"
    assert short["mode"] == "claude" and short["resume_supported"] is False
    assert short["claude_session_id"] == "original-native-ref"
    assert short["session_id"] == "native_1"
    assert result["run"]["status"] == "pending_reconcile"
    assert observe.compact_task_snapshot(result) == result
    assert "unknown_vendor_payload" in native["usage"]


def test_compact_metadata_cannot_reintroduce_unbounded_values():
    current = snapshot()
    current["task"]["id"] = "x" * 1000000
    current["task"].pop("findings")
    current["task"]["findings_count"] = {"unexpected": "x" * 1000000}
    current["run"]["output_available"] = {"unexpected": "x" * 1000000}
    current["run"]["summary_chars"] = 10 ** 5000
    current["session"] = ["x" * 1000000]
    result = observe.compact_task_snapshot(current)
    assert len(json.dumps(result, ensure_ascii=False).encode()) < 32000
    assert "id" in result["task"]["omitted_fields"] and "task_id" not in result["details"]
    assert "findings_count" not in result["task"]
    assert result["run"]["output_available"] is True
    assert result["run"]["summary_chars"] == len(current["run"]["summary"])
    assert result["session"] is None
    assert observe.compact_task_snapshot(result) == result


def test_compact_event_budget_advances_only_through_returned_events():
    page = event_page()
    base = page["events"][0]
    base["payload"].update(toolName="n" * 1000, toolCallId="c" * 1000,
                           modelUsage={"model-" + str(i) + "x" * 700: {"inputTokens": i} for i in range(8)})
    page["events"] = [{**deepcopy(base), "seq": i} for i in range(1, 41)]
    page["next_cursor"] = 40
    result = observe.compact_event_page(page)
    assert 0 < len(result["events"]) < 40
    assert result["has_more"] is True
    assert result["next_cursor"] == result["events"][-1]["seq"]
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= observe.COMPACT_EVENT_PAGE_BYTES
    assert observe.compact_event_page(result) == result
    empty = observe.compact_event_page({**page, "cursor": 40, "next_cursor": 40, "events": []})
    assert empty["next_cursor"] == 40 and empty["events"] == []


def test_default_watch_retains_full_snapshot_and_event_payload(monkeypatch, clock):
    current, page = snapshot("succeeded"), event_page()

    def read(path, method, payload, deadline):
        if method == "task.get":
            assert payload == {"task_id": "tsk_1"}
        return Envelope(ok=True, data=current if method == "task.get" else page)

    monkeypatch.setattr(observe, "_read", read)
    emitted = []
    result = observe.watch_task(Path("unused.sock"), "tsk_1", on_events=emitted.append)
    assert result.data["last_snapshot"] == current and emitted == [page]
    assert "compact" not in result.data and "events_skipped" not in result.data


def test_compact_request_rejected_by_older_core_does_not_retry_full_read(monkeypatch, clock):
    calls = []

    def read(path, method, payload, deadline):
        calls.append((method, payload))
        return Envelope(ok=False, error={"code": INVALID_REQUEST, "message": "未知compact参数"})

    monkeypatch.setattr(observe, "_read", read)
    result = observe.watch_task(Path("unused.sock"), "tsk_1", compact=True, include_events=False)
    assert result.ok is False and result.error["code"] == INVALID_REQUEST
    assert result.data["reason"] == "read_error" and result.data["last_snapshot"] is None
    assert calls == [("task.get", {"task_id": "tsk_1", "compact": True})]


def test_no_events_keeps_fixed_deadline_and_snapshot(monkeypatch, clock):
    calls = []

    def read(path, method, payload, deadline):
        calls.append((clock.now, deadline, method))
        clock.now += min(0.1, deadline - clock.now)
        return Envelope(ok=True, data=snapshot())

    monkeypatch.setattr(observe, "_read", read)
    result = observe.watch_task(Path("unused.sock"), "tsk_1", timeout=1, request_timeout=0.3,
                                interval=0.2, compact=True, include_events=False)
    assert result.data["reason"] == "deadline" and clock.now == 1.0
    assert all(method == "task.get" and deadline <= 1 and deadline - start <= 0.300001
               for start, deadline, method in calls)
    assert result.data["last_snapshot"]["run"]["status"] == "running"
    assert result.data["cursor"] == 0


@pytest.mark.parametrize(("error", "reason"), [(OSError("断连"), "read_error"), (KeyboardInterrupt(), "interrupted")])
def test_no_events_failure_retains_last_snapshot_without_retry(monkeypatch, clock, error, reason):
    calls = []

    def read(path, method, payload, deadline):
        calls.append(method)
        if len(calls) == 1:
            return Envelope(ok=True, data=snapshot())
        raise error

    monkeypatch.setattr(observe, "_read", read)
    result = observe.watch_task(Path("unused.sock"), "tsk_1", compact=True, include_events=False)
    assert result.data["reason"] == reason and len(calls) == 2
    assert result.data["last_snapshot"]["run"]["id"] == "run_1"
    if reason == "read_error":
        assert result.error["code"] == BACKEND_UNAVAILABLE


@pytest.mark.parametrize("options", [{"compact": "true"}, {"include_events": 0},
                                      {"include_events": False, "cursor": 10}])
def test_invalid_modes_or_unbound_cursor_are_rejected_before_io(monkeypatch, options):
    monkeypatch.setattr(observe, "_read", lambda *args: pytest.fail("参数无效时不读服务"))
    with pytest.raises(AsterunError) as error:
        observe.watch_task(Path("unused.sock"), "tsk_1", **options)
    assert error.value.code == INVALID_REQUEST
