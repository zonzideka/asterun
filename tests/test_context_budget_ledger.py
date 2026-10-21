"""context_budget enforce：admission_only 预留、终态结算、超限拒绝。"""
from __future__ import annotations

import json

from asterun.application import Application, _record_budget_capsule
from asterun.config_migration import migrate_v1_to_v2
from asterun.backends.fake import FakeBackend
from asterun.context_budget import ledger
from asterun.context_budget.ledger import NAMESPACE, budget_id, budget_view, reservation_id
from asterun.contracts import TERMINAL_RUN_STATUSES, RunId, RunStatus, TaskId
from asterun.control_store import ControlStore
from asterun.usage.breakdown import normalize_breakdown
from tests.test_context_budget_observe import _enable, _native, managed

_SUBMIT = {"workspace": "demo", "backend": "fake", "text": "hi"}


def _enforce(app):
    budget = _enable(app, segment_soft=60, task_hard=100)
    budget["mode"] = "enforce"
    return budget


def _submit(app):
    return app.handle("task.submit", _SUBMIT)


def _control(app) -> ControlStore:
    return ControlStore(app.store._conn, initialize=False)


def _reservation(app, run_id: str) -> dict:
    return _control(app).get("Reservation", reservation_id(run_id), NAMESPACE)


def _load_run(app, run_id: str):
    return app.store.get_run(RunId(run_id))


def _token_amount(actual):
    if isinstance(actual, list):
        for item in actual:
            if isinstance(item, dict) and item.get("meter") == "tokens":
                return item.get("amount")
    if isinstance(actual, dict):
        if actual.get("meter") == "tokens" and "amount" in actual:
            return actual["amount"]
        tokens = actual.get("tokens")
        if isinstance(tokens, dict):
            return tokens.get("amount", tokens.get("tokens"))
        if "amount" in actual:
            return actual["amount"]
    return actual


def test_enforce_submit_reserves_budget_and_task_get(managed):
    _enforce(managed)
    result = _submit(managed)
    assert result.ok
    task_id = result.ids["task_id"]
    run_id = result.ids["run_id"]

    reservation = _reservation(managed, run_id)
    run = _load_run(managed, run_id)
    if run.status in TERMINAL_RUN_STATUSES:
        assert reservation["status"] in {"SETTLED", "UNCERTAIN"}
    else:
        assert reservation["status"] == "HELD"

    budget = _control(managed).get("Budget", budget_id(task_id), NAMESPACE)
    assert budget["limits"][0]["limit"] == 100
    assert budget["limits"][0]["enforcement"] == "admission_only"

    view = managed.handle("task.get", {"task_id": task_id})
    assert view.ok
    data = view.data
    context = data.get("context_budget") if isinstance(data, dict) else None
    nested = context.get("budget") if isinstance(context, dict) else None
    if isinstance(nested, dict) and "task_hard" in nested:
        assert nested["task_hard"] == 100
    else:
        assert '"task_hard": 100' in json.dumps(data)


def test_terminal_settle_then_budget_soft_capsule(managed):
    _enforce(managed)
    result = _submit(managed)
    assert result.ok
    task_id = result.ids["task_id"]
    run_id = result.ids["run_id"]

    run = _load_run(managed, run_id)
    run.native.update(_native())
    run.status = RunStatus.SUCCEEDED
    managed.store.save_run(run)

    reservation = _reservation(managed, run_id)
    assert reservation["status"] in {"SETTLED", "UNCERTAIN"}
    if reservation["status"] == "SETTLED":
        processed = normalize_breakdown(run.native)["processed"]
        assert _token_amount(reservation["actual"]) == processed

    task = managed.store.get_task(TaskId(task_id))
    _record_budget_capsule(managed, task, type("Rejected", (), {"id": RunId("run-rejected")})())
    conn = managed.store._conn
    try:
        rows = conn.execute("SELECT trigger FROM context_capsules").fetchall()
        triggers = [row[0] for row in rows]
        assert "budget_soft" in triggers
    except Exception:
        rows = conn.execute("SELECT * FROM context_capsules").fetchall()
        blob = json.dumps([tuple(row) for row in rows], ensure_ascii=False, default=str)
        assert "budget_soft" in blob


def test_enforce_exhausted_rejects_without_dispatch(managed, monkeypatch):
    _enforce(managed)
    original = ledger.ensure_budget

    def wrapped(store, task_id, profile_key, task_hard, policy_sha):
        bud = original(store, task_id, profile_key, task_hard, policy_sha)
        control = ControlStore(store._conn, initialize=False)
        seed = ledger._entity(
            "Reservation",
            "res_seed",
            task_id=task_id,
            budget_id=bud["id"],
            action_id="seed",
            reserved=[{"meter": "tokens", "amount": 80, "billing_pool_ref": None}],
            actual=None,
            status="HELD",
            receipt_evidence_ids=[],
        )
        control.reserve(seed)
        control.settle(
            NAMESPACE,
            "res_seed",
            [{"meter": "tokens", "amount": 80, "billing_pool_ref": None}],
        )
        return bud

    monkeypatch.setattr(ledger, "ensure_budget", wrapped)
    calls = {"n": 0}
    dispatch = FakeBackend.dispatch

    def counting_dispatch(self, *args, **kwargs):
        calls["n"] += 1
        return dispatch(self, *args, **kwargs)

    monkeypatch.setattr(FakeBackend, "dispatch", counting_dispatch)

    result = _submit(managed)
    assert not result.ok
    assert result.error["code"] == "BUDGET_EXHAUSTED"
    assert calls["n"] == 0
    for task_id in managed.store.list_task_ids():
        for run in managed.store.list_runs(task_id):
            assert run.status not in {RunStatus.RUNNING, RunStatus.SUCCEEDED}


def test_observe_submit_does_not_open_budget(managed):
    _enable(managed, segment_soft=60, task_hard=100)
    result = _submit(managed)
    assert result.ok
    assert budget_view(managed.store._conn, result.ids["task_id"]) is None


def test_observe_task_get_omits_historical_budget(managed):
    _enforce(managed)
    result = _submit(managed)
    assert result.ok
    task_id = result.ids["task_id"]
    assert budget_view(managed.store._conn, task_id) is not None
    managed.config.context_budget["mode"] = "observe"
    view = managed.handle("task.get", {"task_id": task_id})
    assert view.ok
    context = view.data.get("context_budget") if isinstance(view.data, dict) else None
    if isinstance(context, dict):
        assert "budget" not in context
    else:
        assert context is None


def _codex_thread_cumulative(*, fresh=False):
    """与 breakdown._codex 相同的 token_usage.total 形状：totalTokens = input + output。"""
    native = {
        "token_usage": {
            "tokenUsage": {
                "total": {
                    "inputTokens": 800_000,
                    "cachedInputTokens": 100_000,
                    "outputTokens": 50_000,
                    "reasoningOutputTokens": 1_000,
                    "totalTokens": 850_000,
                },
            },
        },
    }
    if fresh:
        native["segment_fresh_thread"] = True
    return native


def _submit_held(app):
    """非终态脚本留下 HELD 预留，随后把这条 run 写成终态再结算。"""
    _enforce(app)
    result = app.handle("task.submit", {**_SUBMIT, "script": "timeout"})
    assert result.ok
    run_id = result.ids["run_id"]
    assert _reservation(app, run_id)["status"] == "HELD"
    assert _load_run(app, run_id).status not in TERMINAL_RUN_STATUSES
    return run_id


def test_codex_fresh_thread_cumulative_settles_processed(managed):
    """新开线程的 codex thread_cumulative 等于本段用量，终态按 processed 结算。"""
    run_id = _submit_held(managed)
    run = _load_run(managed, run_id)
    run.native.update(_codex_thread_cumulative(fresh=True))
    run.status = RunStatus.SUCCEEDED
    managed.store.save_run(run)

    picture = normalize_breakdown(run.native)
    assert picture["convention"] == "codex_app_server"
    assert picture["scope"] == "thread_cumulative"
    assert picture["attributable"] is False
    assert picture["processed"] == 850_000
    reservation = _reservation(managed, run_id)
    assert reservation["status"] == "SETTLED"
    assert reservation["status"] != "UNCERTAIN"
    assert _token_amount(reservation["actual"]) == picture["processed"]


def test_codex_thread_cumulative_without_fresh_thread_is_uncertain(managed):
    """没有 segment_fresh_thread 时线程累计不可归属，预留转为 UNCERTAIN。"""
    run_id = _submit_held(managed)
    run = _load_run(managed, run_id)
    run.native.update(_codex_thread_cumulative())
    assert run.native.get("segment_fresh_thread") is not True
    run.status = RunStatus.SUCCEEDED
    managed.store.save_run(run)

    picture = normalize_breakdown(run.native)
    assert picture["convention"] == "codex_app_server"
    assert picture["scope"] == "thread_cumulative"
    assert picture["attributable"] is False
    assert picture["processed"] == 850_000
    reservation = _reservation(managed, run_id)
    assert reservation["status"] == "UNCERTAIN"


def test_enforce_memory_store_submit_returns_budget_unknown(config_path):
    raw = migrate_v1_to_v2(config_path)["config"]
    config_path.write_text(json.dumps(raw))
    app = Application.from_paths(config_path)
    try:
        assert not hasattr(app.store, "_conn")
        _enforce(app)
        result = _submit(app)
        assert not result.ok
        assert result.error["code"] == "BUDGET_UNKNOWN"
    finally:
        app.close()
