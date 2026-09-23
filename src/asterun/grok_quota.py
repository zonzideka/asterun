"""从持久原生失败证据拦截同池后续派发；不猜测余额、不结清未知运行。"""
from __future__ import annotations

from pathlib import Path

from asterun.control_protocol import digest
from asterun.errors import AsterunError


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
    return []


def guard(app, backend_name):
    current = set(scopes(app, backend_name))
    if not current:
        return
    # The existing run record is the latch: crash/reopen/backup cannot lose it,
    # and a later successful/in-flight run cannot clear another run's 402.
    for task_id in app.store.list_task_ids():
        for run in app.store.list_runs(task_id):
            if (run.native.get("provider_failure", {}).get("kind") == "quota_exhausted"
                    and current.intersection(run.native.get("grok_quota_scopes", []))):
                raise AsterunError("BUDGET_EXHAUSTED", "同计费池已有持久的 Grok 402 记录，后续派发已停止",
                    next_action="核实额度恢复后显式更新计费窗口；v1 更新 quota_epoch。未知运行仍需独立对账。",
                    details={"source_run_id": run.id.value, "balance": "unknown", "auto_retry": False})
