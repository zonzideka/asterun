"""额度快照的 inspect / set / clear，以及只读 quota.refresh。

inspect / set / clear 只读写本地缓存。Codex refresh 只调用 account/rateLimits/read。
Grok refresh 只有显式开启未公开探针时才调用 `_x.ai/billing`。最小间隔内不建线程。
quota.read 与 usage.inspect 一样针对整个实例（资源 "*"）。
quota.write 与 config.apply 一样针对整个实例。默认操作者策略不按动作名区分，因此两者都放行。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
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
from asterun.quota import grok_probe
from asterun.quota.codex_probe import probe_codex, probe_lifetime_s
from asterun.quota.settings import codex_quota_settings, grok_quota_settings
from asterun.usage.models import timestamp

READ_ACTION = "quota.read"
WRITE_ACTION = "quota.write"
PROBE_ID = "codex.app_server.rate_limits"
GROK_PROBE_ID = "grok.acp.billing"
MIN_PROBE_INTERVAL = timedelta(seconds=60)
_NAMESPACE = "ns_local"
_TIGHT = ("exhausted", "latched")
_ERROR_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


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


def _reached(raw: Any, now: datetime) -> bool:
    """resets_at / valid_until 已到或无法解析。空值表示没有这一时刻。"""
    if raw is None:
        return False
    if not isinstance(raw, str) or not raw:
        return True
    try:
        return now >= timestamp(raw, "quota_time")
    except AsterunError:
        return True


def _window_usable(item: dict, now: datetime) -> bool:
    """stale、unknown、expired，以及已翻转或已过 valid_until 的普通窗口，都不参与最紧选择。

    闩锁在 resets_at 之前仍然有效，不因短 valid_until 被丢掉。
    """
    state = item.get("freshness")
    if state in {"stale", "unknown", "expired", "absent"}:
        return False
    obs = item.get("observation")
    if not isinstance(obs, dict) or state not in {"fresh", "exhausted", "latched"}:
        return False
    if _reached(obs.get("resets_at"), now):
        return False
    if state != "latched" and _reached(obs.get("valid_until"), now):
        return False
    return True


def _tightest(windows: list[dict], now: datetime) -> dict | None:
    """最紧的仍有效窗口。没有有效窗口时返回 None。

    先去掉过期、未知和已翻转的窗口。耗尽和闩锁优先于 fresh；同级再取 used_bp 更高、重置更早者。
    """
    usable = [item for item in windows if _window_usable(item, now)]
    if not usable:
        return None
    windows = usable

    def rank(item: dict) -> int:
        state = item["freshness"]
        if state in _TIGHT:
            return 0
        if state == "fresh":
            return 1
        return 2

    def key(item: dict):
        obs = item["observation"]
        used = obs.get("used_bp")
        used_key = -(used if type(used) is int else -1)
        return (
            rank(item),
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
    from asterun.plugin_store import read_quota_snapshots

    scopes = select_scopes(app.config, payload, single=False)
    admission = getattr(app, "admission", None)
    namespace = admission.namespace if admission is not None else _NAMESPACE
    conn = app.store._conn
    now = _now()
    reports = []
    for scope in scopes:
        merged = merge_observations(read_quota_snapshots(conn, scope, namespace))
        windows = [_window_view(item, now) for item in merged]
        reports.append({"scope_ref": scope, "tightest": _tightest(windows, now), "windows": windows})
    return {"scopes": reports, "upstream_queried": False}


def set_quota(app, payload: dict) -> dict:
    if payload["window_kind"] == "latch":
        _fail("手动录入不能创建闩锁")
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


def _codex_backend(config, scope: str, payload: dict):
    if config.schema_version == 2:
        return config.get_backend(payload.get("backend"))
    alias = scope.removeprefix("backend:")
    if alias == scope or not alias:
        _fail("未知额度 scope", INVALID_CONFIG, scope_ref=scope)
    return config.get_backend(alias)


def _probe_interval(probe_settings: dict) -> timedelta:
    raw = probe_settings.get("min_interval_seconds")
    seconds = raw if type(raw) is int else int(MIN_PROBE_INTERVAL.total_seconds())
    return timedelta(seconds=max(int(MIN_PROBE_INTERVAL.total_seconds()), seconds))


def _probe_due(state, now: datetime, interval: timedelta = MIN_PROBE_INTERVAL) -> bool:
    if not state or not state.get("last_attempt_at"):
        return True
    elapsed = now - timestamp(state["last_attempt_at"], "last_attempt_at")
    return elapsed >= interval


def _lease_active(state, now: datetime) -> bool:
    raw = None if not state else state.get("lease_until")
    if not raw:
        return False
    return timestamp(raw, "lease_until") > now


def _error_code(value) -> str:
    if isinstance(value, str) and _ERROR_CODE.fullmatch(value) is not None:
        return value
    return "PROBE_FAILED"


def _scope_result(scope: str, *, probe_id: str, ok: bool, error_code, written: int, skipped) -> dict:
    return {
        "scope_ref": scope,
        "probe_id": probe_id,
        "ok": ok,
        "error_code": error_code,
        "written": written,
        "skipped": skipped,
    }


def _save_probe_state(store, namespace, scope: str, now: datetime, previous, *, probe_id: str, ok: bool, error_code,
                      claimed_attempt_at: str, claimed_lease_until: str) -> None:
    """仍持有本次写入的 last_attempt_at 与 lease_until 才写结果并释放租约。"""
    with store.transaction():
        current = store.get_probe_state(scope, probe_id, namespace)
        if (current is None or current.get("last_attempt_at") != claimed_attempt_at
                or current.get("lease_until") != claimed_lease_until):
            return
        if ok:
            last_ok_at = canonical_utc(now)
            code = None
        else:
            last_ok_at = None if previous is None else previous.get("last_ok_at")
            code = error_code
        store.put_probe_state({
            "scope_ref": scope,
            "probe_id": probe_id,
            "last_attempt_at": claimed_attempt_at,
            "last_ok_at": last_ok_at,
            "last_error_code": code,
            "lease_until": None,
        }, namespace)


def _refresh_probe_scope(store, namespace, scope: str, now: datetime, *, probe_id: str, settings: dict, call) -> dict:
    """占用租约、遵守最小间隔，并只在仍持有租约时写回结果。call(timeout_s, ttl_s) 执行具体探针。"""
    interval = _probe_interval(settings)
    raw_timeout = settings["timeout_seconds"]
    timeout_s = raw_timeout if type(raw_timeout) is int else 10
    with store.transaction():
        previous = store.get_probe_state(scope, probe_id, namespace)
        if _lease_active(previous, now):
            claimed = False
            skipped = "in_flight"
        elif not _probe_due(previous, now, interval):
            claimed = False
            skipped = "min_interval"
        else:
            claimed_state = store.put_probe_state({
                "scope_ref": scope,
                "probe_id": probe_id,
                "last_attempt_at": canonical_utc(now),
                "last_ok_at": None if previous is None else previous.get("last_ok_at"),
                "last_error_code": None if previous is None else previous.get("last_error_code"),
                "lease_until": canonical_utc(now + timedelta(seconds=probe_lifetime_s(timeout_s))),
            }, namespace)
            claimed_attempt_at = claimed_state["last_attempt_at"]
            claimed_lease_until = claimed_state["lease_until"]
            claimed = True
            skipped = None
    if not claimed:
        return _scope_result(scope, probe_id=probe_id, ok=False, error_code=None, written=0, skipped=skipped)
    ok = False
    error_code = "PROBE_FAILED"
    written = 0
    try:
        try:
            report = call(raw_timeout, settings["ttl_seconds"])
        except Exception:
            report = {"ok": False, "error_code": "PROBE_FAILED"}
        if not isinstance(report, dict):
            report = {"ok": False, "error_code": "PROBE_FAILED"}
        ok = report.get("ok") is True
        error_code = None if ok else _error_code(report.get("error_code"))
        observations = report.get("observations") if ok else None
        if ok and not isinstance(observations, list):
            ok = False
            error_code = "PROBE_FAILED"
        elif ok and any(not isinstance(item, dict) for item in observations):
            ok = False
            error_code = "PROBE_FAILED"
        elif ok:
            for item in observations:
                observation = dict(item)
                observation["scope_ref"] = scope
                store.append_quota_snapshot(observation, namespace)
                written += 1
    finally:
        _save_probe_state(
            store, namespace, scope, now, previous, probe_id=probe_id, ok=ok, error_code=error_code,
            claimed_attempt_at=claimed_attempt_at, claimed_lease_until=claimed_lease_until,
        )
    return _scope_result(scope, probe_id=probe_id, ok=ok, error_code=error_code, written=written, skipped=None)


def refresh_quota(app, payload: dict) -> dict:
    """按后端种类做只读额度刷新。间隔、超时和缓存时间来自 quota 配置。"""
    if app.config.schema_version == 2 and payload.get("backend") is None:
        _fail("v2 额度刷新目前只支持 backend 参数")
    scopes = select_scopes(app.config, payload, single=False)
    explicit = payload.get("scope_ref") is not None or payload.get("backend") is not None
    store, namespace = _store(app)
    now = _now()
    reports = []
    codex_settings = codex_quota_settings(app.config)["probes"]["codex_app_server"]
    grok_settings = grok_quota_settings(app.config)["probes"]["grok_acp_billing"]
    for scope in scopes:
        backend = _codex_backend(app.config, scope, payload)
        if backend.kind == "codex":
            if codex_settings["enabled"] is not True:
                reports.append(_scope_result(
                    scope, probe_id=PROBE_ID, ok=False, error_code="PROBE_DISABLED", written=0, skipped=None,
                ))
                continue
            reports.append(_refresh_probe_scope(
                store, namespace, scope, now, probe_id=PROBE_ID, settings=codex_settings,
                call=lambda timeout_s, ttl_s, backend=backend, scope=scope: probe_codex(
                    backend, scope_ref=scope, now=now, timeout_s=timeout_s, ttl_s=ttl_s,
                ),
            ))
        elif backend.kind == "grok":
            acknowledged = grok_settings["enabled"] is True and grok_settings["acknowledge_undocumented"] is True
            if not acknowledged:
                reports.append(_scope_result(
                    scope, probe_id=GROK_PROBE_ID, ok=False, error_code="PROBE_DISABLED", written=0, skipped=None,
                ))
                continue
            # 未放行真实 Grok 时由服务层记跳过。这不是探针错误码。
            if not grok_probe.live_grok_enabled():
                reports.append(_scope_result(
                    scope, probe_id=GROK_PROBE_ID, ok=False, error_code=None, written=0, skipped="disabled",
                ))
                continue
            reports.append(_refresh_probe_scope(
                store, namespace, scope, now, probe_id=GROK_PROBE_ID, settings=grok_settings,
                call=lambda timeout_s, ttl_s, backend=backend, scope=scope: grok_probe.probe_grok(
                    backend, scope_ref=scope, now=now, timeout_s=timeout_s, ttl_s=ttl_s,
                ),
            ))
        elif explicit:
            _fail("quota.refresh 只支持 Codex 后端", backend=backend.name)
    return {"scopes": reports, "upstream_queried": True}


def handle(app, method: str, payload: dict | None) -> dict:
    if method not in {"quota.inspect", "quota.set", "quota.clear", "quota.refresh"}:
        _fail(f"未知额度方法：{method}")
    payload = json_copy({} if payload is None else payload)
    reject_self_reported_authority(payload)
    _authorize(app, READ_ACTION if method == "quota.inspect" else WRITE_ACTION)
    if method == "quota.inspect":
        return inspect_quota(app, payload)
    if method == "quota.set":
        return set_quota(app, payload)
    if method == "quota.refresh":
        return refresh_quota(app, payload)
    return clear_quota(app, payload)
