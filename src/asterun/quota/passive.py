"""把 Codex 运行结束时带回的速率窗口写入额度缓存。失败不得影响 save_run。"""

from __future__ import annotations

from asterun.contracts import TERMINAL_RUN_STATUSES
from asterun.quality import digest
from asterun.quota.codex_probe import parse_rate_limits
from asterun.quota.service import scopes_for_backend
from asterun.quota.settings import codex_quota_settings
from asterun.usage.models import timestamp


def persist_run_quota(app, run) -> int:
    """终态 Codex 运行把 scrub 后的 account/rateLimits/updated 写成 passive_codex 观测。

    同一 run 与同一 observed_at 只写一次。被动开关关闭、范围无法唯一确定、或任何异常都返回 0。
    """
    try:
        return _persist_run_quota(app, run)
    except Exception:
        return 0


def _persist_run_quota(app, run) -> int:
    settings = codex_quota_settings(getattr(app, "config", None))
    if settings["passive"]["codex_rate_limits_updated"] is not True:
        return 0
    if getattr(run, "status", None) not in TERMINAL_RUN_STATUSES:
        return 0
    native = getattr(run, "native", None)
    captured = native.get("quota_rate_limits") if isinstance(native, dict) else None
    if not isinstance(captured, dict) or not captured:
        return 0
    params = captured.get("params")
    observed_raw = captured.get("observed_at")
    if not isinstance(params, dict) or not params or not isinstance(observed_raw, str) or not observed_raw:
        return 0
    config = app.config
    alias = run.backend.value
    backend = config.get_backend(alias)
    if backend.kind != "codex":
        return 0
    if config.schema_version == 2:
        scopes = scopes_for_backend(config, alias)
        if len(scopes) != 1:
            return 0
        scope = scopes[0]
    else:
        scope = f"backend:{alias}"
    observed_at = timestamp(observed_raw, "observed_at")
    ttl_s = settings["probes"]["codex_app_server"]["ttl_seconds"]
    observations = parse_rate_limits(
        params,
        scope_ref=scope,
        observed_at=observed_at,
        ttl_s=ttl_s,
        source_kind="passive_codex",
    )
    if not observations:
        return 0
    raw_digest = digest(f"{run.id.value}{observed_raw}")
    from asterun.plugin_store import PluginStore

    store = PluginStore(app.store._conn)
    if any(row.get("raw_digest") == raw_digest for row in store.quota_snapshots(scope)):
        return 0
    written = 0
    for item in observations:
        observation = dict(item)
        observation["scope_ref"] = scope
        observation["raw_digest"] = raw_digest
        store.append_quota_snapshot(observation)
        written += 1
    return written
