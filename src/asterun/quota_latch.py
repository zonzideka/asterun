"""从持久原生失败证据拦截同池或同额度桶的后续派发；不猜测余额、不结清未知运行。"""
from __future__ import annotations

from pathlib import Path

from asterun.control_protocol import digest
from asterun.errors import AsterunError


def model_bucket(model):
    name = model.lower() if isinstance(model, str) else ""
    if name.startswith("claude") or name.startswith("gpt"):
        return "claude_gpt"
    return "gemini"


def scopes(app, backend_name):
    backend = app.config.get_backend(backend_name)
    if backend.connection_ref:
        connection = app.config.connections[backend.connection_ref]
        return [digest({"pool": ref, "window": app.config.billing_pools[ref].get("window_id", "local")})
                for ref in connection["billing_pool_refs"]]
    if backend.kind == "grok" and backend.home:
        return [digest({"grok_home": str(Path(backend.home).resolve()),
                        "account": app.account_ref.value, "runtime": app.runtime_ref.value,
                        "epoch": backend.quota_epoch or "default"})]
    if backend.kind == "antigravity":
        home = backend.home
        return [digest({"agy_home": str(Path(home).resolve()) if home else "default",
                        "bucket": model_bucket(backend.model),
                        "account": app.account_ref.value, "runtime": app.runtime_ref.value,
                        "epoch": backend.quota_epoch or "default"})]
    return []


def _stored_scopes(native):
    if "quota_latch_scopes" in native:
        return native.get("quota_latch_scopes") or []
    return native.get("grok_quota_scopes") or []


def _provider(app, run, failure):
    named = failure.get("provider")
    if named in {"grok", "antigravity"}:
        return named
    backend_name = getattr(getattr(run, "backend", None), "value", None)
    backends = getattr(app.config, "backends", {})
    if backend_name in backends:
        kind = app.config.get_backend(backend_name).kind
        if kind in {"grok", "antigravity"}:
            return kind
    return "grok"


def active_latch(app, name):
    current = set(scopes(app, name))
    if not current:
        return None
    # The existing run record is the latch: crash/reopen/backup cannot lose it,
    # and a later successful/in-flight run cannot clear another run's exhaustion.
    for task_id in app.store.list_task_ids():
        for run in app.store.list_runs(task_id):
            failure = run.native.get("provider_failure") or {}
            if not isinstance(failure, dict) or failure.get("kind") != "quota_exhausted":
                continue
            if current.intersection(_stored_scopes(run.native)):
                return {
                    "source_run_id": run.id.value,
                    "provider": _provider(app, run, failure),
                    "status_code": failure.get("status_code"),
                }
    return None


def guard(app, backend_name):
    latch = active_latch(app, backend_name)
    if not latch:
        return
    if latch["provider"] == "antigravity":
        raise AsterunError(
            "BUDGET_EXHAUSTED",
            "同额度桶已有持久的 Antigravity 429 额度耗尽记录，后续派发已停止",
            next_action="核实额度恢复后：v1 更新该后端的 quota_epoch；v2 更新计费窗口。",
            details={"source_run_id": latch["source_run_id"], "balance": "unknown", "auto_retry": False},
        )
    raise AsterunError(
        "BUDGET_EXHAUSTED",
        "同计费池已有持久的 Grok 402 记录，后续派发已停止",
        next_action="核实额度恢复后显式更新计费窗口；v1 更新 quota_epoch。未知运行仍需独立对账。",
        details={"source_run_id": latch["source_run_id"], "balance": "unknown", "auto_retry": False},
    )
