from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import pytest

from asterun.application import Application
from asterun.cli import _load_request, build_parser
from asterun.contracts import EventType
from asterun.ids import RunId
from asterun.mcp_server import handle_rpc
from asterun.observe import COMPACT_EVENT_PAGE_BYTES, COMPACT_USAGE_BYTES, watch_task
from asterun.requests import REQUEST_SCHEMAS, validate_request
from asterun.service import LocalClient, MAX_MESSAGE


@pytest.fixture
def large_service(config_path):
    # 短 socket 路径；只启动隔离 fake 核心，不调用账户或模型。
    with tempfile.TemporaryDirectory(prefix="asterun-compact-ipc-", dir="/tmp") as temporary:
        state = Path(temporary)
        seed = Application.from_paths(config_path, state)
        try:
            submitted = seed.handle("task.submit", {"workspace": "demo", "text": "fake compact test"})
            assert submitted.ok
            task_id, run_id = submitted.ids["task_id"], submitted.ids["run_id"]
            run = seed.store.get_run(RunId(run_id))
            run.native.update(thread_id="native-thread", turn_id="native-turn", token_usage={
                "threadId": "native-thread", "turnId": "native-turn", "tokenUsage": {
                    "total": {"inputTokens": 100, "cachedInputTokens": 80, "outputTokens": 20,
                              "reasoningOutputTokens": 5, "totalTokens": 120}},
                "unrecognized": "x" * (MAX_MESSAGE - 500)}, usage_scope="thread_cumulative")
            # 这份原生通知本身低于1MiB，但整个task.get信封高于IPC上限。
            assert len(json.dumps(run.native["token_usage"]).encode()) < MAX_MESSAGE
            seed.store.save_run(run)
            for index in range(24):
                seed._event(run.id, EventType.MESSAGE, "backend.fake", {
                    "kind": "tool_call", "toolName": "n" * 900, "toolCallId": f"call-{index}-" + "c" * 900,
                    "text": "大事件正文" * 16000,
                    "modelUsage": {"model-" + str(i) + "x" * 700: {"inputTokens": i} for i in range(8)}})
            last_seq = seed.store.list_events(run.id, cursor=0, page_size=500)[-1].seq
        finally:
            seed.close()
        command = [sys.executable, "-m", "asterun", "--config", str(config_path), "--state-dir", str(state)]
        process = subprocess.Popen([*command, "serve"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        client = LocalClient(state / "asterun.sock", timeout=5)
        try:
            deadline = time.monotonic() + 5
            while not client.path.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            assert client.path.exists(), process.poll()
            yield client, command, task_id, run_id, last_seq
        finally:
            if process.poll() is None:
                process.terminate()
            output, error = process.communicate(timeout=5)
            assert process.returncode == 0, (output, error)


def test_compact_task_get_crosses_real_ipc_with_near_limit_native_notice(large_service):
    client, _, task_id, run_id, _ = large_service
    assert not client.handle("task.get", {"task_id": task_id}).ok
    result = client.handle("task.get", {"task_id": task_id, "compact": True})
    assert result.ok, result.error
    assert result.data["run"]["status"] == "succeeded"
    assert result.data["run"]["id"] == run_id
    native = result.data["run"]["native"]
    assert native["thread_id"] == "native-thread" and native["turn_id"] == "native-turn"
    assert native["usage_truncated"] is True and native["usage_byte_limit"] == COMPACT_USAGE_BYTES
    assert native["token_usage"]["tokenUsage"]["total"]["totalTokens"] == 120
    assert len(json.dumps(result.to_dict(), ensure_ascii=False).encode()) < 32000


def test_compact_watch_requests_server_pages_before_ipc_and_retains_every_cursor(large_service):
    client, _, task_id, run_id, last_seq = large_service
    assert not client.handle("task.events", {"task_id": task_id, "page_size": 100}).ok
    pages = []
    result = watch_task(client.path, task_id, compact=True, timeout=5, request_timeout=2,
                        page_size=100, on_events=pages.append)
    assert result.ok, result.error
    assert result.data["reason"] == "terminal"
    assert result.data["run_id"] == run_id and result.data["cursor"] == last_seq
    assert len(pages) > 1 and pages[0]["has_more"] is True
    returned = [event["seq"] for page in pages for event in page["events"]]
    assert returned == list(range(1, last_seq + 1))
    for page in pages:
        assert len(json.dumps(page, ensure_ascii=False).encode()) <= COMPACT_EVENT_PAGE_BYTES
        assert page["next_cursor"] == page["events"][-1]["seq"]
        assert all("text" not in event.get("payload", {}) for event in page["events"])
    empty = client.handle("task.events", {"task_id": task_id, "cursor": last_seq, "compact": True})
    assert empty.ok and empty.data["events"] == [] and empty.data["next_cursor"] == last_seq
    no_events = watch_task(client.path, task_id, compact=True, include_events=False,
                           cursor=last_seq, run_id=run_id, timeout=5)
    assert no_events.ok and no_events.data["requests"] == 1
    assert no_events.data["cursor"] == last_seq and no_events.data["events_skipped"] is True


def test_task_events_cli_flag_is_transmitted_to_resident_core(large_service):
    _, command, task_id, _, _ = large_service
    result = subprocess.run([*command, "--connect", "task-events", task_id, "--compact", "--page-size", "100"],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    envelope = json.loads(result.stdout)
    assert envelope["ok"] and envelope["data"]["compact"] is True
    assert envelope["data"]["has_more"] is True


def test_compact_event_entry_schema_and_mcp_match_application(app):
    submitted = app.handle("task.submit", {"workspace": "demo", "text": "entry check"})
    task_id = submitted.ids["task_id"]
    payload = {"task_id": task_id, "cursor": 0, "page_size": 100, "compact": True}
    assert validate_request("task.events", payload) == payload
    parsed = build_parser().parse_args(["task-events", task_id, "--compact"])
    assert _load_request(parsed) == payload
    listing = handle_rpc(app, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    tool = next(item for item in listing["result"]["tools"] if item["name"] == "task_events")
    assert tool["inputSchema"] == REQUEST_SCHEMAS["task.events"]
    response = handle_rpc(app, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                              "params": {"name": "task_events", "arguments": payload}})
    envelope = json.loads(response["result"]["content"][0]["text"])
    assert envelope["ok"] and envelope["data"]["compact"] is True
    full = app.handle("task.events", {"task_id": task_id})
    assert full.ok and "compact" not in full.data and "has_more" not in full.data
