"""额度快照的 inspect / set / clear。只读写本地缓存，不探活、不启动客户端。

quota.read 与 usage.inspect 一样针对整个实例（资源 "*"）。
quota.write 与 config.apply 一样针对整个实例。默认操作者策略不按动作名区分，因此两者都放行。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from asterun.errors import CONFIG_REQUIRED, INVALID_CONFIG, INVALID_REQUEST, AsterunError
from asterun.plugin_api.protocol import json_copy
from asterun.policy import enforce, reject_self_reported_authority
from asterun.quota.models import (
    MANUAL_MAX_TTL,
    canonical_utc,
    freshness,
    merge_observations,
    used_bp_from_percent,
)
from asterun.usage.models import timestamp

READ_ACTION = "quota.read"
WRITE_ACTION = "quota.write"
_NAMESPACE = "ns_local"
_TIGHT = ("exhausted", "latched")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fail(message: str, code: str = INVALID_REQUEST, **details: Any) -> None:
    raise AsterunError(code, message, details=details or None)


def configured_scope_refs(config) -> list[str]:
    """v2 用计费池 ref；v1 只有 backends，用 backend:<alias>。"""
    if config.schema_version == 2:
        return sorted(config.billing_pools)
    return [f"backend:{name}" for name in sorted(config.backends)]


def scopes_for_backend(config, alias: str) -> list[str]:
    """把 --backend 换算成 scope_ref。v2 取该后端连接上的计费池。"""
    backend = config.get_backend(alias)
    if config.schema_version != 2:
        return [f"backend:{backend.name}"]
    connection = config.connections.get(backend.connection_ref) if backend.connection_ref else None
    pools = []
    if isinstance(connection, dict):
        pools = [ref for ref in connection.get("billing_pool_refs") or [] if ref in config.billing_pools]
    if not pools:
        _fail("该后端没有可换算的计费池 scope", INVALID_CONFIG, backend=backend.name)
    return list(pools)


def select_scopes(config, payload: dict, *, single: bool) -> list[str]:
    scope = payload.get("scope_ref")
    backend = payload.get("backend")
    if scope is not None and backend is not None:
        _fail("scope_ref 与 backend 只能指定一个")
    if backend is not None:
        scopes = scopes_for_backend(config, backend)
        if single and len(scopes) != 1:
            _fail("该后端对应多个计费池，请改用 scope_ref", scopes=scopes)
        return scopes
    known = configured_scope_refs(config)
    if scope is not None:
        if scope not in known:
            _fail("未知额度 scope", INVALID_CONFIG, scope_ref=scope, known=known)
        return [scope]
    if single:
        _fail("需要 scope_ref 或 backend")
    return known


def _authorize(app, action: str) -> None:
    enforce(app.policy.authorize(app.principal, action, "*"))
    app._require_grant("*")
    if not hasattr(app.store, "_conn"):
        raise AsterunError(CONFIG_REQUIRED, "额度快照需要 SQLite 状态目录")
    app._require_applied_config()


def _store(app):
    admission = getattr(app, "admission", None)
    if admission is not None:
        return admission.persistent(), admission.namespace
    from asterun.plugin_store import PluginStore

    return PluginStore(app.store._conn), _NAMESPACE


def _window_view(item: dict, now: datetime) -> dict:
    fresh = freshness(item["observation"], now)
    issues = list(dict.fromkeys([*item["issues"], *fresh["issues"]]))
    return {
        "observation": item["observation"],
        "freshness": fresh["state"],
        "age_s": fresh["age_s"],
        "issues": issues,
    }


def _tightest(windows: list[dict]) -> dict | None:
    """最紧约束：耗尽或闩锁优先，其余取 used_bp 更高、重置更早的窗口。"""
    if not windows:
        return None

    def key(item: dict):
        obs = item["observation"]
        used = obs.get("used_bp")
        used_key = -(used if type(used) is int else -1)
        return (
            0 if item["freshness"] in _TIGHT else 1,
            used_key,
            obs.get("resets_at") or "9999-12-31T23:59:59Z",
            obs.get("bucket_id") or "",
            obs.get("window_kind") or "",
        )

    winner = min(windows, key=key)
    obs = winner["observation"]
    return {
        "bucket_id": obs["bucket_id"],
        "window_kind": obs["window_kind"],
        "used_bp": obs["used_bp"],
        "resets_at": obs["resets_at"],
        "source_kind": obs["source_kind"],
        "confidence": obs["confidence"],
        "freshness": winner["freshness"],
        "age_s": winner["age_s"],
        "issues": list(winner["issues"]),
    }


def inspect_quota(app, payload: dict) -> dict:
    scopes = select_scopes(app.config, payload, single=False)
    store, namespace = _store(app)
    now = _now()
    reports = []
    for scope in scopes:
        merged = merge_observations(store.quota_snapshots(scope, namespace))
        windows = [_window_view(item, now) for item in merged]
        reports.append({"scope_ref": scope, "tightest": _tightest(windows), "windows": windows})
    return {"scopes": reports, "upstream_queried": False}


def set_quota(app, payload: dict) -> dict:
    scope = select_scopes(app.config, payload, single=True)[0]
    now = _now()
    valid = timestamp(payload["valid_until"], "valid_until")
    if valid <= now:
        _fail("valid_until 必须晚于现在")
    if valid > now + MANUAL_MAX_TTL:
        _fail("valid_until 不能晚于现在起 24 小时")
    resets = timestamp(payload["resets_at"], "resets_at") if "resets_at" in payload else None
    if resets is not None and valid > resets:
        _fail("valid_until 不能晚于 resets_at")
    observation = {
        "scope_ref": scope,
        "bucket_id": payload["bucket_id"],
        "window_kind": payload["window_kind"],
        "source_kind": "manual",
        "confidence": "low",
        "paid_overage": False,
        "observed_at": canonical_utc(now),
        "valid_until": canonical_utc(valid),
        "used_bp": used_bp_from_percent(payload["used_percent"]),
    }
    if resets is not None:
        observation["resets_at"] = canonical_utc(resets)
    if "window_duration_s" in payload:
        observation["window_duration_s"] = payload["window_duration_s"]
    if payload.get("exhausted"):
        observation["exhausted_hint"] = "exhausted"
    store, namespace = _store(app)
    saved = store.append_quota_snapshot(observation, namespace)
    return {"scope_ref": scope, "observation": saved["observation"], "upstream_queried": False}


def clear_quota(app, payload: dict) -> dict:
    if payload.get("source", "manual") != "manual":
        _fail("quota.clear 只清除手动录入")
    scope = select_scopes(app.config, payload, single=True)[0]
    store, namespace = _store(app)
    cleared = store.clear_manual(scope, namespace)
    return {"scope_ref": scope, "source": "manual", "cleared": cleared, "upstream_queried": False}


def handle(app, method: str, payload: dict | None) -> dict:
    if method not in {"quota.inspect", "quota.set", "quota.clear"}:
        _fail(f"未知额度方法：{method}")
    payload = json_copy({} if payload is None else payload)
    reject_self_reported_authority(payload)
    _authorize(app, READ_ACTION if method == "quota.inspect" else WRITE_ACTION)
    if method == "quota.inspect":
        return inspect_quota(app, payload)
    if method == "quota.set":
        return set_quota(app, payload)
    return clear_quota(app, payload)
