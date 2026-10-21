"""codex / agy 标记续段集成：标记、处理量、子代理与派发约定。

fake 不在 MARKER_KINDS 里时，plan 把 fake 写成 turn_limit。把它加进 MARKER_KINDS 后，
plan 在 turn_limit 返回之前走 marker 分支（trigger=marker、max_turns 为空、segment_soft 取自 profile）。
"""
from __future__ import annotations

from asterun.backends.fake import FakeBackend
from asterun.context_budget.segment import MARKER, MARKER_KINDS
from asterun.contracts import RunStatus
from asterun.usage.breakdown import normalize_breakdown
from tests.test_context_budget_observe import managed
from tests.test_context_segment_continue import _by_index, _enforce, _settle, _start

_REAL_DISPATCH = FakeBackend.dispatch


def _marker_kinds(monkeypatch):
    monkeypatch.setattr(
        "asterun.context_budget.segment.MARKER_KINDS",
        frozenset({*MARKER_KINDS, "fake"}),
    )


def _processed_native(processed, **extra):
    native = {
        "usage_source": "native_headless_report",
        "usage": {
            "input_tokens": processed,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": processed,
        },
    }
    native.update(extra)
    return native


def _patch_marker_dispatch(monkeypatch, *, summary, processed, subagent_count=0):
    native = _processed_native(processed)
    if subagent_count:
        native["subagent_count"] = subagent_count

    def dispatch(self, task_id, run_id, script, text, *, cwd=None):
        dispatch.calls += 1
        dispatch.texts.append(text)
        if dispatch.calls == 1:
            return {
                "status": "succeeded",
                "terminated": True,
                "summary": summary,
                "events": [],
                "native": native,
            }
        return _REAL_DISPATCH(self, task_id, run_id, "success", text, cwd=cwd)

    dispatch.calls = 0
    dispatch.texts = []
    dispatch.native = native
    monkeypatch.setattr(FakeBackend, "dispatch", dispatch)
    return dispatch


def _record_dispatch(monkeypatch):
    def dispatch(self, task_id, run_id, script, text, *, cwd=None):
        dispatch.texts.append(text)
        return _REAL_DISPATCH(self, task_id, run_id, script, text, cwd=cwd)

    dispatch.texts = []
    monkeypatch.setattr(FakeBackend, "dispatch", dispatch)
    return dispatch


def _finish(app):
    task, source, runs = _start(app)
    _settle(app)
    task = app.store.get_task(task.id)
    source = app.store.get_run(source.id)
    runs = list(app.store.list_runs(task.id))
    return task, source, runs


def test_marker_and_enough_processed_continues_once(managed, monkeypatch):
    _enforce(managed)
    _marker_kinds(monkeypatch)
    summary = "本段到此。\n" + MARKER
    dispatch = _patch_marker_dispatch(monkeypatch, summary=summary, processed=60)
    assert normalize_breakdown(dispatch.native)["processed"] >= 60
    task, source, runs = _finish(managed)
    assert task.run_count == 2
    assert len(runs) == 2
    assert source.native["segment"]["trigger"] == "marker"
    assert source.status == RunStatus.SUCCEEDED
    successor = _by_index(runs)[1]
    assert successor.native.get("segment_resume") == "new_session"
    assert "续段说明" in successor.prompt
    sent = dispatch.texts[1]
    assert "分段约定" in sent
    assert "续段说明" in sent
    assert source.native["segment_end"]["continued_by"] == successor.id.value


def test_missing_marker_does_not_continue(managed, monkeypatch):
    _enforce(managed)
    _marker_kinds(monkeypatch)
    _patch_marker_dispatch(monkeypatch, summary="本段到此，没有标记。", processed=60)
    task, source, runs = _finish(managed)
    assert task.run_count == 1
    assert len(runs) == 1
    assert source.status == RunStatus.SUCCEEDED
    assert "segment_end" not in source.native


def test_processed_below_soft_does_not_continue(managed, monkeypatch):
    _enforce(managed)
    _marker_kinds(monkeypatch)
    dispatch = _patch_marker_dispatch(monkeypatch, summary="本段到此。\n" + MARKER, processed=59)
    assert normalize_breakdown(dispatch.native)["processed"] < 60
    task, _source, runs = _finish(managed)
    assert task.run_count == 1
    assert len(runs) == 1


def test_subagent_does_not_continue(managed, monkeypatch):
    _enforce(managed)
    _marker_kinds(monkeypatch)
    dispatch = _patch_marker_dispatch(
        monkeypatch, summary="本段到此。\n" + MARKER, processed=60, subagent_count=1,
    )
    assert dispatch.native["subagent_count"] == 1
    task, source, runs = _finish(managed)
    assert task.run_count == 1
    assert len(runs) == 1
    assert source.native.get("subagent_count") == 1


def test_marker_dispatch_includes_convention_turn_limit_does_not(managed, monkeypatch):
    _enforce(managed)
    plain = _record_dispatch(monkeypatch)
    _task, source, _runs = _finish(managed)
    assert source.native["segment"]["trigger"] == "turn_limit"
    assert "分段约定" not in plain.texts[0]

    _marker_kinds(monkeypatch)
    marked = _record_dispatch(monkeypatch)
    _task, source, _runs = _finish(managed)
    assert source.native["segment"]["trigger"] == "marker"
    assert source.native["segment"]["max_turns"] is None
    assert source.native["segment"]["segment_soft"] == 60
    assert "分段约定" in marked.texts[0]
