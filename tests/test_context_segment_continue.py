"""分段续段集成：回合上限后续段、段数上限、幂等与不续段条件。"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from asterun.application import Application
from asterun.backends.fake import FakeBackend
from asterun.context_budget.segment import continue_segment, guard_continuation, plan
from asterun.contracts import RunId, RunStatus, TaskId
from asterun.errors import AsterunError
from tests.test_context_budget_observe import _enable, managed

_SUBMIT = {"workspace": "demo", "backend": "fake", "text": "hi", "script": "success"}


def _enforce(app, **overrides):
    profile = {
        "segment_turns": 5,
        "segment_soft": 60,
        "task_hard": 100000,
        "max_segments": 3,
    }
    profile.update(overrides)
    budget = _enable(app, **profile)
    budget["mode"] = "enforce"
    budget["segmentation"]["backends"] = ["fake"]
    return budget


def _failure(kind):
    return {
        "status": "failed",
        "error_code": "GROK_RUN_INCOMPLETE",
        "terminated": True,
        "summary": "turn limit",
        "events": [],
        "native": {"provider_failure": {"kind": kind, "source": "native_end"}},
    }


def _patch_dispatch(monkeypatch, *, k, kind="turn_limit"):
    original = FakeBackend.dispatch

    def dispatch(self, task_id, run_id, script, text, *, cwd=None):
        dispatch.calls += 1
        if dispatch.calls <= k:
            return _failure(kind)
        return original(self, task_id, run_id, "success", text, cwd=cwd)

    dispatch.calls = 0
    monkeypatch.setattr(FakeBackend, "dispatch", dispatch)
    return dispatch


def _settle(app):
    for _ in range(4):
        app._drain_queue()


def _start(app):
    result = app.handle("task.submit", dict(_SUBMIT))
    assert result.ok, getattr(result, "error", result)
    _settle(app)
    task = app.store.get_task(TaskId(result.ids["task_id"]))
    source = app.store.get_run(RunId(result.ids["run_id"]))
    runs = list(app.store.list_runs(task.id))
    return task, source, runs


def _by_index(runs):
    return {run.native["segment"]["index"]: run for run in runs}


@pytest.mark.parametrize("gate", ["observe", "empty_backends"])
def test_observe_or_empty_whitelist_does_not_continue(managed, monkeypatch, gate):
    budget = _enable(managed, segment_turns=5, segment_soft=60, task_hard=100000, max_segments=3)
    if gate == "empty_backends":
        budget["mode"] = "enforce"
        budget["segmentation"]["backends"] = []
    else:
        assert budget["mode"] == "observe"
    _patch_dispatch(monkeypatch, k=1)
    task, source, runs = _start(managed)
    assert task.run_count == 1
    assert len(runs) == 1
    assert source.native.get("segment") is None


def test_enforce_turn_limit_continues_once(managed, monkeypatch):
    _enforce(managed)
    _patch_dispatch(monkeypatch, k=1)
    task, source, runs = _start(managed)
    assert task.run_count == 2
    assert task.repair_count == 0
    assert len(runs) == 2
    successor = _by_index(runs)[1]
    assert successor.role == source.role
    assert successor.native["segment"]["index"] == 1
    assert successor.native.get("restored_from_capsule_id")
    assert successor.native.get("segment_resume") == "new_session"
    assert source.native["segment_end"]["continued_by"] == successor.id.value
    assert successor.status == RunStatus.SUCCEEDED
    assert "续段说明" in successor.prompt


def test_max_segments_stops_without_a_third_segment(managed, monkeypatch):
    _enforce(managed, max_segments=2)
    _patch_dispatch(monkeypatch, k=5)
    task, source, runs = _start(managed)
    assert task.run_count == 2
    assert len(runs) == 2
    ended = _by_index(runs)[1]
    assert ended.native["segment_end"]["reason"] == "max_segments"
    assert source.id != ended.id
    assert all((run.native.get("segment") or {}).get("index") != 2 for run in runs)


def test_continue_segment_is_idempotent(managed, monkeypatch):
    _enforce(managed)
    _patch_dispatch(monkeypatch, k=1)
    task, source, runs = _start(managed)
    assert task.run_count == 2
    assert len(runs) == 2
    task = managed.store.get_task(task.id)
    source = managed.store.get_run(source.id)
    again = continue_segment(managed, task, source)
    _settle(managed)
    reloaded = managed.store.get_task(task.id)
    assert again is None or (isinstance(again, dict) and again.get("reused") is True)
    assert reloaded.run_count == task.run_count
    assert len(list(managed.store.list_runs(task.id))) == 2


def test_queue_full_does_not_continue(managed, monkeypatch):
    _enforce(managed)
    _patch_dispatch(monkeypatch, k=1)
    monkeypatch.setattr(managed.scheduler, "can_enqueue", lambda: False)
    task, source, runs = _start(managed)
    assert task.run_count == 1
    assert len(runs) == 1
    assert source.native["segment_end"]["reason"] == "queue_full"
    assert source.native["segment_end"].get("continued_by") is None


def test_control_owned_source_segment_records_reason(managed, monkeypatch):
    _enforce(managed)
    _patch_dispatch(monkeypatch, k=1)
    monkeypatch.setattr("asterun.context_budget.segment.continue_segment", lambda *args, **kwargs: None)
    task, source, runs = _start(managed)
    assert len(runs) == 1
    assert source.native.get("segment")
    assert "segment_end" not in source.native
    managed.store._conn.execute(
        "UPDATE operations SET idempotency_key=? WHERE run_id=?",
        ("rcp_segment_source", source.id.value),
    )
    result = continue_segment(
        managed,
        managed.store.get_task(task.id),
        managed.store.get_run(source.id),
    )
    assert result == {"stopped": "control_owned"}
    source = managed.store.get_run(source.id)
    assert source.native["segment_end"]["reason"] == "control_owned"
    assert source.native["segment_end"].get("continued_by") is None
    assert managed.store.get_task(task.id).run_count == 1
    assert len(list(managed.store.list_runs(task.id))) == 1


def test_restart_replays_unjudged_terminal_segment_once(managed, monkeypatch):
    _enforce(managed)
    source_path = managed.config.source_path
    raw = json.loads(source_path.read_text(encoding="utf-8"))
    raw["context_budget"] = managed.config.context_budget
    source_path.write_text(json.dumps(raw), encoding="utf-8")
    state = managed.state_dir
    calls = {"n": 0}
    real = continue_segment

    def skip_first(app, task, run):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real(app, task, run)

    monkeypatch.setattr("asterun.context_budget.segment.continue_segment", skip_first)
    _patch_dispatch(monkeypatch, k=1)
    task, source, runs = _start(managed)
    assert calls["n"] == 1
    assert task.run_count == 1
    assert len(runs) == 1
    assert source.native.get("segment")
    assert source.native["segment"]["index"] == 0
    assert "segment_end" not in source.native
    task_id = task.id
    managed.close()
    reopened = Application.from_paths(source_path, state)
    try:
        task = reopened.store.get_task(task_id)
        current = reopened.store.get_run(task.current_run_id)
        runs = list(reopened.store.list_runs(task.id))
        assert task.run_count == 2
        assert len(runs) == 2
        assert current.native["segment"]["index"] == 1
        assert task.current_run_id == current.id
        successor_id = current.id
        reopened.close()
        reopened = Application.from_paths(source_path, state)
        task = reopened.store.get_task(task_id)
        runs = list(reopened.store.list_runs(task.id))
        assert task.run_count == 2
        assert len(runs) == 2
        assert task.current_run_id == successor_id
        assert {run.native["segment"]["index"] for run in runs} == {0, 1}
    finally:
        reopened.close()


def test_quota_exhausted_does_not_continue(managed, monkeypatch):
    _enforce(managed)
    _patch_dispatch(monkeypatch, k=1, kind="quota_exhausted")
    task, source, runs = _start(managed)
    assert task.run_count == 1
    assert len(runs) == 1
    assert source.native["provider_failure"]["kind"] == "quota_exhausted"
    assert source.native.get("segment") is not None
    assert "segment_end" not in source.native


def test_capsule_not_ready_does_not_continue(managed, monkeypatch):
    def not_ready(app, task, run, *, trigger):
        return {"ready": 0, "capsule_id": "cap_blocked", "trigger": trigger, "blob_sha256": None}

    _enforce(managed)
    _patch_dispatch(monkeypatch, k=1)
    monkeypatch.setattr("asterun.context_budget.segment.record_capsule", not_ready)
    task, source, runs = _start(managed)
    assert task.run_count == 1
    assert len(runs) == 1
    assert source.native["segment_end"]["reason"] == "capsule_not_ready"


def _queue_successor(app, monkeypatch):
    """首段回合上限结束后拦住自动 drain，使后继停在队列里。"""
    dispatch = _patch_dispatch(monkeypatch, k=1)
    real_drain = app._drain_queue
    state = {"block": True}

    def drain():
        if not state["block"]:
            real_drain()

    monkeypatch.setattr(app, "_drain_queue", drain)
    result = app.handle("task.submit", dict(_SUBMIT))
    assert result.ok, getattr(result, "error", result)
    task = app.store.get_task(TaskId(result.ids["task_id"]))
    runs = list(app.store.list_runs(task.id))
    successor = _by_index(runs)[1]
    assert successor.status == RunStatus.QUEUED
    assert dispatch.calls == 1
    state["block"] = False
    return task, successor, dispatch


def test_queued_successor_fails_when_segmentation_disabled(managed, monkeypatch):
    _enforce(managed)
    _task, successor, dispatch = _queue_successor(managed, monkeypatch)
    managed.config.context_budget["mode"] = "observe"
    managed._drain_queue()
    successor = managed.store.get_run(successor.id)
    assert successor.status == RunStatus.FAILED
    assert successor.error_code == "CAPABILITY_UNSUPPORTED"
    assert dispatch.calls == 1


def test_queued_successor_fails_when_mandatory_changes(managed, monkeypatch):
    _enforce(managed)
    task, successor, dispatch = _queue_successor(managed, monkeypatch)
    task = managed.store.get_task(task.id)
    managed.store.save_task(replace(task, findings=[{"id": "f-1", "text": "changed"}]))
    managed._drain_queue()
    successor = managed.store.get_run(successor.id)
    assert successor.status == RunStatus.FAILED
    assert successor.error_code == "REVISION_CONFLICT"
    assert dispatch.calls == 1


def test_max_runs_stops_without_a_successor(managed, monkeypatch):
    _enforce(managed)
    managed.config.scheduler.max_runs_per_task = 1
    _patch_dispatch(monkeypatch, k=1)
    task, source, runs = _start(managed)
    assert task.run_count == 1
    assert len(runs) == 1
    assert source.native["segment_end"]["reason"] == "max_runs"
    assert source.native["segment_end"].get("continued_by") is None


def test_marker_write_failure_rolls_back_successor(managed, monkeypatch):
    _enforce(managed)
    real_save = managed.store.save_run
    state = {"thrown": False}

    def save_run(run):
        native = run.native if isinstance(getattr(run, "native", None), dict) else {}
        marker = native.get("segment_end")
        if (not state["thrown"] and isinstance(marker, dict) and marker.get("reason") == "turn_budget"
                and marker.get("continued_by")):
            state["thrown"] = True
            raise RuntimeError("marker write failed")
        return real_save(run)

    monkeypatch.setattr(managed.store, "save_run", save_run)
    _patch_dispatch(monkeypatch, k=1)
    result = managed.handle("task.submit", dict(_SUBMIT))
    assert result.ok, getattr(result, "error", result)
    _settle(managed)
    task = managed.store.get_task(TaskId(result.ids["task_id"]))
    source = managed.store.get_run(RunId(result.ids["run_id"]))
    assert state["thrown"]
    assert task.run_count == 1
    assert task.current_run_id == source.id
    assert len(list(managed.store.list_runs(task.id))) == 1
    assert "segment_end" not in source.native
    events = managed.store.list_events(source.id)
    assert any((event.payload or {}).get("segment_continue_error") == "RuntimeError" for event in events)
    again = continue_segment(managed, task, source)
    _settle(managed)
    task = managed.store.get_task(task.id)
    runs = list(managed.store.list_runs(task.id))
    assert again is not None and again.get("continued")
    assert task.run_count == 2
    assert len(runs) == 2
    successor = _by_index(runs)[1]
    source = managed.store.get_run(source.id)
    assert source.native["segment_end"]["reason"] == "turn_budget"
    assert source.native["segment_end"]["continued_by"] == successor.id.value
    assert successor.status == RunStatus.SUCCEEDED


def test_guard_restores_missing_continued_by(managed, monkeypatch):
    _enforce(managed)
    task, successor, dispatch = _queue_successor(managed, monkeypatch)
    source = _by_index(list(managed.store.list_runs(task.id)))[0]
    source.native["segment_end"].pop("continued_by")
    managed.store.save_run(source)
    assert "continued_by" not in managed.store.get_run(source.id).native["segment_end"]
    managed._drain_queue()
    successor = managed.store.get_run(successor.id)
    source = managed.store.get_run(source.id)
    assert successor.status == RunStatus.SUCCEEDED
    assert source.native["segment_end"]["continued_by"] == successor.id.value
    assert dispatch.calls == 2


def test_plan_returns_none_for_quality_or_target_hash(managed):
    from types import SimpleNamespace

    _enforce(managed)
    plain = SimpleNamespace(
        id=SimpleNamespace(value="run_plain_segment"),
        role="implementation",
        backend=SimpleNamespace(value="fake"),
        native={},
    )
    assert plan(managed, SimpleNamespace(id=SimpleNamespace(value="tsk_plain_segment")), plain) is not None
    assert plan(managed, SimpleNamespace(quality={"phase": "review"}), plain) is None
    bound = SimpleNamespace(
        role="implementation", backend=SimpleNamespace(value="fake"), native={}, target_hash="abc",
    )
    assert plan(managed, SimpleNamespace(), bound) is None
    assert plan(managed, SimpleNamespace(read_scope={"path": "src"}), plain) is None
    assert plan(managed, SimpleNamespace(capability="local.echo"), plain) is None
    assert plan(managed, SimpleNamespace(dependencies=["task_other"]), plain) is None
    repair = SimpleNamespace(
        role="implementation",
        backend=SimpleNamespace(value="fake"),
        native={"external_repair_target": {"target_hash": "abc"}},
    )
    assert plan(managed, SimpleNamespace(), repair) is None


def _control_app():
    from types import SimpleNamespace

    return SimpleNamespace(
        store=SimpleNamespace(
            list_runs=lambda task_id: [],
            find_operation_for_run=lambda run_id: None,
        ),
    ), SimpleNamespace(id=SimpleNamespace(value="tsk_control")), SimpleNamespace(
        id=SimpleNamespace(value="run_control"),
        role="implementation",
        backend=SimpleNamespace(value="fake"),
        native={},
    )


def test_plan_rcp_operation_returns_none(managed, monkeypatch):
    from types import SimpleNamespace

    _enforce(managed)
    app, task, run = _control_app()
    app.config = managed.config
    monkeypatch.setattr(app.store, "find_operation_for_run", lambda run_id: SimpleNamespace(idempotency_key="rcp_x"))
    assert plan(app, task, run) is None


def test_plan_quality_control_operation_returns_none(managed, monkeypatch):
    from types import SimpleNamespace

    _enforce(managed)
    app, task, run = _control_app()
    app.config = managed.config
    monkeypatch.setattr(
        app.store,
        "find_operation_for_run",
        lambda run_id: SimpleNamespace(idempotency_key="idem_plain"),
    )
    app.capabilities = SimpleNamespace(quality_control_for_operation=lambda operation: True)
    assert plan(app, task, run) is None


def test_plan_operation_lookup_error_returns_none(managed, monkeypatch):
    _enforce(managed)
    app, task, run = _control_app()
    app.config = managed.config

    def find(_run_id):
        raise RuntimeError("store down")

    monkeypatch.setattr(app.store, "find_operation_for_run", find)
    assert plan(app, task, run) is None


def test_guard_continuation_rejects_control_owned(monkeypatch):
    from types import SimpleNamespace

    app, task, run = _control_app()
    run.native = {"segment_source_run_id": "run_source", "segment": {"index": 1}}
    monkeypatch.setattr(app.store, "find_operation_for_run", lambda run_id: SimpleNamespace(idempotency_key="rcp_x"))
    with pytest.raises(AsterunError) as caught:
        guard_continuation(app, task, run)
    assert caught.value.code == "CAPABILITY_UNSUPPORTED"
    assert caught.value.message == "runner-control 任务本版不分段，未派发"


def test_segmentation_disabled_persists_and_blocks_later_continue(managed, monkeypatch):
    _enforce(managed)
    _patch_dispatch(monkeypatch, k=1)
    monkeypatch.setattr("asterun.context_budget.segment.continue_segment", lambda *args, **kwargs: None)
    task, source, runs = _start(managed)
    assert len(runs) == 1
    assert source.native.get("segment")
    assert "segment_end" not in source.native
    managed.config.context_budget["mode"] = "observe"
    result = continue_segment(
        managed,
        managed.store.get_task(task.id),
        managed.store.get_run(source.id),
    )
    assert result == {"stopped": "segmentation_disabled"}
    source = managed.store.get_run(source.id)
    assert source.native["segment_end"]["reason"] == "segmentation_disabled"
    assert source.native["segment_end"].get("continued_by") is None
    managed.config.context_budget["mode"] = "enforce"
    again = continue_segment(
        managed,
        managed.store.get_task(task.id),
        managed.store.get_run(source.id),
    )
    _settle(managed)
    task = managed.store.get_task(task.id)
    runs = list(managed.store.list_runs(task.id))
    assert again == {"stopped": "segmentation_disabled"}
    assert task.run_count == 1
    assert len(runs) == 1


def test_handoff_resume_continues_unjudged_terminal_segment(managed, monkeypatch):
    _enforce(managed)
    original = FakeBackend.dispatch

    def dispatch(self, task_id, run_id, script, text, *, cwd=None):
        dispatch.calls += 1
        if dispatch.calls == 1:
            current = managed.store.get_task(task_id)
            managed.store.save_task(replace(current, orchestration_owner="human"))
            return _failure("turn_limit")
        return original(self, task_id, run_id, "success", text, cwd=cwd)

    dispatch.calls = 0
    monkeypatch.setattr(FakeBackend, "dispatch", dispatch)
    result = managed.handle("task.submit", dict(_SUBMIT))
    assert result.ok, getattr(result, "error", result)
    _settle(managed)
    task = managed.store.get_task(TaskId(result.ids["task_id"]))
    source = managed.store.get_run(RunId(result.ids["run_id"]))
    runs = list(managed.store.list_runs(task.id))
    assert task.orchestration_owner == "human"
    assert task.run_count == 1
    assert len(runs) == 1
    assert source.native.get("segment")
    assert "segment_end" not in source.native
    resumed = managed.handle("task.handoff", {"task_id": task.id.value, "action": "resume"})
    assert resumed.ok, getattr(resumed, "error", resumed)
    _settle(managed)
    task = managed.store.get_task(task.id)
    source = managed.store.get_run(source.id)
    runs = list(managed.store.list_runs(task.id))
    assert task.run_count == 2
    assert len(runs) == 2
    successor = _by_index(runs)[1]
    assert successor.native["segment"]["index"] == 1
    assert source.native["segment_end"]["continued_by"] == successor.id.value
