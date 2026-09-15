from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from asterun.application import Application
from asterun.ids import RunId
from asterun.backends.codex import CodexSession
from tests.conftest import write_config


@pytest.fixture
def native_config(isolated_env, workspace_root, monkeypatch):
    binary = isolated_env / "codex-stub"
    binary.write_text(f"#!{sys.executable}\n" + (Path(__file__).parent / "fixtures/codex_server.py").read_text())
    binary.chmod(0o700)
    monkeypatch.setenv("ASTERUN_RUN_CODEX", "1")
    return write_config(isolated_env / "native-config.json", workspace_root,
                        extra_backends={"codex": {"kind": "codex", "bin": str(binary)}})


def wait_for(app, task_id, condition, timeout=5):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        app.poll()
        last = app.handle("task.get", {"task_id": task_id})
        assert last.ok, last.error
        if condition(last.data):
            return last.data
        time.sleep(0.02)
    pytest.fail(f"未等到预期状态：{last}")


def submit(app, text, **kwargs):
    result = app.handle("task.submit", {"workspace": "demo", "backend": "codex", "text": text, **kwargs})
    assert result.ok, result.error
    return result


def test_admission_returns_before_native_process_completes_and_idle_poll_persists(native_config, isolated_env):
    app = Application.from_paths(native_config, isolated_env / "state", background=True)
    try:
        result = submit(app, "finish")
        assert result.data["run"]["status"] == "dispatching"
        run_id = RunId(result.ids["run_id"])
        deadline = time.monotonic() + 5
        # 没有 task.get/events；宿主循环仍应把公开结果和终态落盘。
        while app.store.get_run(run_id).status != "succeeded" and time.monotonic() < deadline:
            app.poll()
            time.sleep(0.02)
        run = app.store.get_run(run_id)
        assert run.status == "succeeded"
        assert run.summary == "隔离进程已完成"
        assert run.native["thread_id"] == "native-thread-1"
        assert run.native["turn_id"] == "native-turn-1"
        assert not app.scheduler.inflight
    finally:
        app.close()


@pytest.mark.parametrize("with_usage", [False, True])
def test_codex_runtime_preserves_only_observed_thread_usage(native_config, isolated_env, workspace_root, with_usage):
    state_dir = isolated_env / "usage-state"
    app = Application.from_paths(native_config, state_dir, background=True)
    try:
        submitted = submit(app, "finish with-token-usage" if with_usage else "finish")
        done = wait_for(app, submitted.ids["task_id"], lambda data: data["run"]["status"] == "succeeded")
        native = done["run"]["native"]
        usage = app.handle("usage.report", {"task_id": submitted.ids["task_id"], "include_runs": True})
        assert usage.ok
        row = usage.data["runs"][0]
        if with_usage:
            expected = {"threadId": "native-thread-1", "turnId": "native-turn-1", "tokenUsage": {
                "total": {"inputTokens": 100, "cachedInputTokens": 80, "outputTokens": 20,
                          "reasoningOutputTokens": 8, "totalTokens": 120},
                "last": {"inputTokens": 10, "cachedInputTokens": 0, "outputTokens": 2,
                         "reasoningOutputTokens": 1, "totalTokens": 12}}}
            assert native["token_usage"] == expected
            assert native["usage_scope"] == "thread_cumulative"
            assert native["usage_source"] == "codex_app_server"
            assert row["scope"] == "thread_cumulative"
            assert row["observed_tokens"]["total_tokens"] == 120
            assert row["aggregation_eligible"] is False
            assert usage.data["totals"]["tokens"]["total_tokens"]["known_sum"] is None
        else:
            assert not {"token_usage", "usage_scope", "usage_source"}.intersection(native)
            assert row["observed_tokens"]["total_tokens"] is None
        calls = [json.loads(line) for line in (workspace_root / "native-calls.jsonl").read_text().splitlines()]
        assert sum(call.get("method") == "turn/start" for call in calls) == 1
    finally:
        app.close()
    reopened = Application.from_paths(native_config, state_dir, background=True)
    try:
        persisted = reopened.store.get_run(RunId(submitted.ids["run_id"])).native
        assert persisted == native
    finally:
        reopened.close()


def test_cancel_is_requested_then_confirmed_by_native_terminal(native_config, isolated_env):
    app = Application.from_paths(native_config, isolated_env / "state", background=True)
    try:
        result = submit(app, "slow")
        wait_for(app, result.ids["task_id"], lambda data: data["run"]["status"] == "running")
        cancelled = app.handle("task.cancel", {"task_id": result.ids["task_id"]})
        assert cancelled.ok and not cancelled.data["terminated"]
        final = wait_for(app, result.ids["task_id"], lambda data: data["run"]["status"] == "cancelled")
        assert final["run"]["terminated"] and final["run"]["cancel_requested"]
    finally:
        app.close()


@pytest.mark.parametrize("decision,expected", [("approve", "succeeded"), ("deny", "cancelled")])
def test_native_approval_uses_bound_request_and_cannot_be_repeated(native_config, isolated_env, decision, expected):
    app = Application.from_paths(native_config, isolated_env / "state", background=True)
    try:
        result = submit(app, "approval")
        data = wait_for(app, result.ids["task_id"], lambda data: bool(data["approval"]))
        approval = data["approval"]
        assert approval["native_request"]["id"] == "native-approval"
        wrong = app.handle("approval.respond", {"approval_id": approval["id"], "decision": decision,
                                                "expected_target_hash": "wrong"})
        assert not wrong.ok
        response = app.handle("approval.respond", {"approval_id": approval["id"], "decision": decision,
                                                   "expected_target_hash": approval["target_hash"]})
        assert response.ok, response.error
        repeat = app.handle("approval.respond", {"approval_id": approval["id"], "decision": decision})
        assert not repeat.ok
        wait_for(app, result.ids["task_id"], lambda data: data["run"]["status"] == expected)
    finally:
        app.close()


def test_resume_across_core_restart_keeps_native_thread(native_config, isolated_env, workspace_root):
    state_dir = isolated_env / "state"
    app = Application.from_paths(native_config, state_dir, background=True)
    result = submit(app, "first")
    wait_for(app, result.ids["task_id"], lambda data: data["run"]["status"] == "succeeded")
    app.close()
    app = Application.from_paths(native_config, state_dir, background=True)
    try:
        resumed = app.handle("session.resume", {"conversation_id": result.ids["conversation_id"], "native": True})
        assert resumed.ok and resumed.data["native_resumed"], resumed.error
        second = submit(app, "second", conversation_id=result.ids["conversation_id"])
        final = wait_for(app, second.ids["task_id"], lambda data: data["run"]["status"] == "succeeded")
        assert final["run"]["native"]["thread_id"] == "native-thread-1"
        assert final["run"]["native"]["turn_id"] == "native-turn-2"
        calls = [json.loads(line) for line in (workspace_root / "native-calls.jsonl").read_text().splitlines()]
        assert sum(call.get("method") == "thread/start" for call in calls) == 1
        assert sum(call.get("method") == "turn/start" for call in calls) == 2
    finally:
        app.close()


def test_disconnected_run_reconciles_exact_turn_without_redispatch(native_config, isolated_env, workspace_root):
    app = Application.from_paths(native_config, isolated_env / "state", background=True)
    result = submit(app, "disconnect")
    data = wait_for(app, result.ids["task_id"], lambda data: data["run"]["status"] == "pending_reconcile")
    assert data["run"]["native"]["turn_id"] == "native-turn-1"
    app.close()
    native = json.loads((workspace_root / "native.json").read_text())
    native["native-thread-1"]["turns"][0].update(status="completed", items=[{"type": "agentMessage", "text": "已核对"}])
    (workspace_root / "native.json").write_text(json.dumps(native))
    app = Application.from_paths(native_config, isolated_env / "state", background=True)
    try:
        reconciled = app.handle("task.reconcile", {"task_id": result.ids["task_id"]})
        assert reconciled.ok and reconciled.data["reports"][0]["terminal"], reconciled.error
        assert app.handle("task.get", {"task_id": result.ids["task_id"]}).data["run"]["summary"] == "已核对"
        calls = [json.loads(line) for line in (workspace_root / "native-calls.jsonl").read_text().splitlines()]
        assert sum(call.get("method") == "turn/start" for call in calls) == 1
    finally:
        app.close()


def test_missing_turn_id_never_guesses_latest_remote_turn(native_config, isolated_env, workspace_root):
    app = Application.from_paths(native_config, isolated_env / "state", background=True)
    try:
        result = submit(app, "withheld-id")
        data = wait_for(app, result.ids["task_id"], lambda data: data["run"]["status"] == "pending_reconcile")
        assert not data["run"]["native"].get("turn_id")
        for _ in range(3):
            app.handle("task.reconcile", {"task_id": result.ids["task_id"]})
        assert app.handle("task.get", {"task_id": result.ids["task_id"]}).data["run"]["status"] == "pending_reconcile"
        calls = [json.loads(line) for line in (workspace_root / "native-calls.jsonl").read_text().splitlines()]
        assert sum(call.get("method") == "turn/start" for call in calls) == 1
    finally:
        app.close()


def test_active_conversation_rejects_new_work_but_reuses_same_intent(native_config, isolated_env):
    app = Application.from_paths(native_config, isolated_env / "state", background=True)
    try:
        first = submit(app, "slow", conversation_id="conversation-isolated", idempotency_key="original")
        again = submit(app, "slow", conversation_id="conversation-isolated", idempotency_key="original")
        assert first.ids["run_id"] == again.ids["run_id"]
        second = app.handle("task.submit", {"workspace": "demo", "backend": "codex", "text": "another",
                                           "conversation_id": "conversation-isolated"})
        assert not second.ok and second.error["code"] == "INVALID_REQUEST"
    finally:
        app.close()


def test_changed_native_latest_turn_prevents_resume(native_config, isolated_env, workspace_root):
    app = Application.from_paths(native_config, isolated_env / "state", background=True)
    try:
        first = submit(app, "done")
        wait_for(app, first.ids["task_id"], lambda data: data["run"]["status"] == "succeeded")
        state = json.loads((workspace_root / "native.json").read_text())
        state["native-thread-1"]["turns"].append({"id": "external-turn", "status": "completed", "items": []})
        (workspace_root / "native.json").write_text(json.dumps(state))
        resumed = app.handle("session.resume", {"conversation_id": first.ids["conversation_id"], "native": True})
        assert not resumed.ok and resumed.error["code"] == "BINDING_MISMATCH"
    finally:
        app.close()


def test_shutdown_reaps_native_process_and_leaves_active_run_unknown(native_config, isolated_env):
    app = Application.from_paths(native_config, isolated_env / "state", background=True)
    first = submit(app, "slow")
    wait_for(app, first.ids["task_id"], lambda data: data["run"]["status"] == "running")
    transport = app.backends["codex"].runtime.sessions[first.ids["run_id"]].transport
    process = transport._transport.proc
    app.close()
    assert process.poll() is not None
    reopened = Application.from_paths(native_config, isolated_env / "state")
    try:
        run = reopened.handle("task.get", {"task_id": first.ids["task_id"]}).data["run"]
        assert run["status"] == "pending_reconcile" and not run["terminated"]
    finally:
        reopened.close()


def test_native_notifications_are_filtered_by_thread_turn_and_public_content():
    session = CodexSession("expected-thread", None, "/tmp", None, "")
    session.current_turn_id = "expected-turn"
    session.handle_message({"method": "turn/completed", "params": {
        "threadId": "different-thread", "turn": {"id": "expected-turn", "status": "completed"}}})
    session.handle_message({"method": "turn/completed", "params": {
        "threadId": "expected-thread", "turn": {"id": "old-turn", "status": "completed"}}})
    session.handle_message({"method": "item/reasoning/textDelta", "params": {"delta": "private reasoning"}})
    session.handle_message({"method": "error", "params": {"willRetry": True}})
    assert not session._turn_complete.is_set()
    assert "private reasoning" not in str(session.recent_events)


def test_two_backend_names_cannot_write_same_workspace_concurrently(native_config, isolated_env):
    config = json.loads(native_config.read_text())
    config["backends"]["other-codex"] = dict(config["backends"]["codex"])
    native_config.write_text(json.dumps(config))
    app = Application.from_paths(native_config, isolated_env / "state", background=True)
    try:
        first = submit(app, "slow")
        second = app.handle("task.submit", {"workspace": "demo", "backend": "other-codex", "text": "finish"})
        assert second.ok and second.data["queued"] and not app.backends["other-codex"].dispatch_count
        wait_for(app, first.ids["task_id"], lambda data: data["run"]["status"] == "running")
        app.handle("task.cancel", {"task_id": first.ids["task_id"]})
        wait_for(app, second.ids["task_id"], lambda data: data["run"]["status"] == "succeeded")
        assert app.backends["other-codex"].dispatch_count == 1
    finally:
        app.close()


def test_handoff_stops_orchestration_without_claiming_native_process_paused(native_config, isolated_env):
    app = Application.from_paths(native_config, isolated_env / "state", background=True)
    try:
        first = submit(app, "slow")
        wait_for(app, first.ids["task_id"], lambda data: data["run"]["status"] == "running")
        handoff = app.handle("task.handoff", {"task_id": first.ids["task_id"], "action": "pause"})
        assert handoff.ok and handoff.data["auto_advance_stopped"]
        assert not handoff.data["native_execution_stopped"]
        assert handoff.data["run"]["status"] == "running"
        app.handle("task.cancel", {"task_id": first.ids["task_id"]})
        wait_for(app, first.ids["task_id"], lambda data: data["run"]["status"] == "cancelled")
    finally:
        app.close()


def test_native_reference_persistence_failure_prevents_turn_start(native_config, isolated_env, workspace_root, monkeypatch):
    app = Application.from_paths(native_config, isolated_env / "state", background=True)
    original = app.store.save_session
    failed = []

    def save(session):
        if session.backend_session_id and not failed:
            failed.append(True)
            raise OSError("injected persistence failure")
        return original(session)

    monkeypatch.setattr(app.store, "save_session", save)
    try:
        result = submit(app, "must not start a turn")
        deadline = time.monotonic() + 5
        while not failed and time.monotonic() < deadline:
            try:
                app.poll()
            except OSError:
                pass
            time.sleep(0.02)
        assert failed
        wait_for(app, result.ids["task_id"], lambda data: data["run"]["status"] == "pending_reconcile")
        calls = [json.loads(line) for line in (workspace_root / "native-calls.jsonl").read_text().splitlines()]
        assert any(call.get("method") == "thread/start" for call in calls)
        assert not any(call.get("method") == "turn/start" for call in calls)
    finally:
        app.close()
