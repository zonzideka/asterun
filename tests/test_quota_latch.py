"""额度闩锁。直接构造运行记录，沿用 grok 失败测试里持久化的 native 字段。"""
from pathlib import Path

import pytest

from asterun.contracts import Run, RunStatus
from asterun.control_protocol import digest
from asterun.errors import AsterunError
from asterun.ids import AccountRef, BackendId, RunId, RuntimeRef, TaskId
from asterun.quota_latch import active_latch, guard, model_bucket, scopes


class _Backend:
    def __init__(self, kind, home=None, model=None, quota_epoch=None, connection_ref=None):
        self.kind = kind
        self.home = home
        self.model = model
        self.quota_epoch = quota_epoch
        self.connection_ref = connection_ref


class _Config:
    def __init__(self, backends):
        self.backends = backends
        self.connections = {}
        self.billing_pools = {}

    def get_backend(self, name):
        return self.backends[name]


class _Store:
    def __init__(self, runs):
        self._runs = list(runs)

    def list_task_ids(self):
        seen = []
        for run in self._runs:
            if run.task_id not in seen:
                seen.append(run.task_id)
        return seen

    def list_runs(self, task_id):
        return [run for run in self._runs if run.task_id == task_id]


class _App:
    def __init__(self, backends, runs=()):
        self.config = _Config(backends)
        self.store = _Store(runs)
        self.account_ref = AccountRef("acct-1")
        self.runtime_ref = RuntimeRef("rt-1")


def _run(backend_name, native):
    return Run(
        id=RunId("run_old"),
        task_id=TaskId("tsk_old"),
        backend=BackendId(backend_name),
        status=RunStatus.PENDING_RECONCILE,
        created_at="2026-01-01T00:00:00Z",
        native=native,
    )


def test_grok_digest_matches_inlined_legacy_formula(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    backend = _Backend("grok", home=str(home), quota_epoch="2026-09")
    app = _App({"worker": backend})
    assert scopes(app, "worker") == [digest({
        "grok_home": str(Path(backend.home).resolve()),
        "account": app.account_ref.value,
        "runtime": app.runtime_ref.value,
        "epoch": backend.quota_epoch or "default",
    })]
    backend.connection_ref = "conn"
    app.config.connections["conn"] = {"billing_pool_refs": ["pool-a", "pool-b"]}
    app.config.billing_pools["pool-a"] = {"window_id": "win-a"}
    app.config.billing_pools["pool-b"] = {}
    assert scopes(app, "worker") == [
        digest({"pool": ref, "window": app.config.billing_pools[ref].get("window_id", "local")})
        for ref in app.config.connections["conn"]["billing_pool_refs"]
    ]


def test_legacy_grok_quota_scopes_still_latch(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    app = _App({"worker": _Backend("grok", home=str(home))})
    run = _run("worker", {
        "provider_failure": {"kind": "quota_exhausted", "status_code": 402},
        "grok_quota_scopes": scopes(app, "worker"),
    })
    app.store._runs.append(run)
    latch = active_latch(app, "worker")
    assert latch == {"source_run_id": "run_old", "provider": "grok", "status_code": 402}
    with pytest.raises(AsterunError) as captured:
        guard(app, "worker")
    assert captured.value.code == "BUDGET_EXHAUSTED"
    assert captured.value.message == "同计费池已有持久的 Grok 402 记录，后续派发已停止"
    assert captured.value.next_action == "核实额度恢复后显式更新计费窗口；v1 更新 quota_epoch。未知运行仍需独立对账。"
    assert captured.value.details == {"source_run_id": "run_old", "balance": "unknown", "auto_retry": False}


def test_antigravity_bucket_blocks_same_bucket_only(tmp_path):
    home = tmp_path / "agy"
    home.mkdir()
    source = _Backend("antigravity", home=str(home), model="gemini-2.5-pro")
    other = _Backend("antigravity", home=str(home), model=None)
    claude = _Backend("antigravity", home=str(home), model="claude-sonnet-4")
    gpt = _Backend("antigravity", home=str(home), model="gpt-4.1")
    app = _App({"source": source, "other": other, "claude": claude, "gpt": gpt})
    assert model_bucket(None) == "gemini"
    assert model_bucket("gemini-2.5-pro") == "gemini"
    assert model_bucket("Claude-3") == "claude_gpt"
    assert model_bucket("gpt-4.1") == "claude_gpt"
    assert scopes(app, "source") == scopes(app, "other")
    assert scopes(app, "source") == [digest({
        "agy_home": str(Path(home).resolve()),
        "bucket": "gemini",
        "account": app.account_ref.value,
        "runtime": app.runtime_ref.value,
        "epoch": "default",
    })]
    run = _run("source", {
        "provider_failure": {"kind": "quota_exhausted", "status_code": 429},
        "quota_latch_scopes": scopes(app, "source"),
    })
    app.store._runs.append(run)
    latch = active_latch(app, "other")
    assert latch["provider"] == "antigravity"
    assert latch["status_code"] == 429
    with pytest.raises(AsterunError) as captured:
        guard(app, "other")
    assert captured.value.message == "同额度桶已有持久的 Antigravity 429 额度耗尽记录，后续派发已停止"
    assert captured.value.next_action == "核实额度恢复后：v1 更新该后端的 quota_epoch；v2 更新计费窗口。"
    guard(app, "claude")
    assert active_latch(app, "claude") is None
    other.quota_epoch = "confirmed-next-window"
    assert active_latch(app, "other") is None
    guard(app, "other")
    guard(app, "gpt")
    assert scopes(app, "gpt") == scopes(app, "claude")


def test_rate_limited_does_not_latch(tmp_path):
    home = tmp_path / "agy"
    home.mkdir()
    backend = _Backend("antigravity", home=str(home), model="gemini-2.5-pro")
    app = _App({"agy": backend})
    app.store._runs.append(_run("agy", {
        "provider_failure": {"kind": "rate_limited", "status_code": 429},
        "quota_latch_scopes": scopes(app, "agy"),
    }))
    assert active_latch(app, "agy") is None
    guard(app, "agy")
