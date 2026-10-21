"""分段续段集成：回合上限后续段、段数上限、幂等与不续段条件。"""
from __future__ import annotations

from dataclasses import replace

import pytest

from asterun.backends.fake import FakeBackend
from asterun.context_budget.segment import continue_segment
from asterun.contracts import RunId, RunStatus, TaskId
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
