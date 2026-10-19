"""Grok 未公开 ACP 扩展 `_x.ai/billing` 的只读额度探针。默认不调用。

只做 initialize 与 `_x.ai/billing`。不调用 session/new 或 session/prompt，不保存金额、邮箱或用户标识。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import shutil
import subprocess
import tempfile
import time
from typing import Any

from asterun.backends.grok import (
    GROK_ACP_METHODS,
    GROK_QUOTA_METHODS,
    GrokAcpTransport,
    GrokBackend,
    live_grok_enabled,
)
from asterun.grok_profile import build_environment
from asterun.quota.models import canonical_utc
from asterun.transport import JsonRpcError
from asterun.usage.models import MAX_INTEGER

GROK_BILLING_METHOD = "_x.ai/billing"
_USED_PERCENT_MAX = 1000
_MS_THRESHOLD = 1_000_000_000_000

assert GROK_BILLING_METHOD == "_x.ai/billing"
assert GROK_QUOTA_METHODS == ("initialize", GROK_BILLING_METHOD)
assert GROK_BILLING_METHOD not in GROK_ACP_METHODS


def parse_billing(result, *, scope_ref, observed_at, ttl_s) -> list[dict]:
    """白名单解析账单对象。结构不符时返回空列表，不抛异常，也不保留金额或身份字段。"""
    try:
        return _parse_billing(result, scope_ref=scope_ref, observed_at=observed_at, ttl_s=ttl_s)
    except Exception:
        return []


def probe_grok(
    backend_config,
    *,
    scope_ref,
    now,
    timeout_s: float = 10.0,
    ttl_s: float = 900,
    transport_factory=GrokAcpTransport,
) -> dict:
    """只读账单。不调用 session/new 或 session/prompt。错误文本不返回给调用方。"""
    if not live_grok_enabled():
        return {"ok": False, "error_code": "LIVE_DISABLED"}
    assert GROK_QUOTA_METHODS == ("initialize", GROK_BILLING_METHOD)
    assert GROK_BILLING_METHOD not in GROK_ACP_METHODS
    transport = None
    tmpdir = None
    invoked: list[str] = []
    try:
        backend = GrokBackend(backend_config)
        binary = backend.discover_bin()
        if binary is None:
            return {"ok": False, "error_code": "PROBE_FAILED"}
        home = backend._prepared_home()
        env = build_environment(home)
        transport = transport_factory(grok_bin=str(binary), env=env)
        tmpdir = tempfile.mkdtemp(prefix="asterun-grok-quota-")
        transport.spawn(tmpdir)
        # 两次 RPC 共用 timeout_s。启动和关闭不占这段时间，由服务侧租约里的 probe_lifetime_s 覆盖。
        deadline = time.monotonic() + timeout_s
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"ok": False, "error_code": "PROBE_TIMEOUT"}
        _record_quota_method(invoked, "initialize")
        transport.initialize(remaining)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"ok": False, "error_code": "PROBE_TIMEOUT"}
        _record_quota_method(invoked, GROK_BILLING_METHOD)
        payload = transport.read_billing(remaining)
        assert tuple(invoked) == GROK_QUOTA_METHODS
        observations = parse_billing(payload, scope_ref=scope_ref, observed_at=now, ttl_s=ttl_s)
        if not observations:
            return {"ok": False, "error_code": "SCHEMA_MISMATCH"}
        return {"ok": True, "observations": observations}
    except Exception as exc:
        return {"ok": False, "error_code": _failure_code(exc)}
    finally:
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass
        if tmpdir is not None:
            shutil.rmtree(tmpdir, ignore_errors=True)


def _record_quota_method(invoked: list[str], method: str) -> None:
    if method not in GROK_QUOTA_METHODS:
        raise AssertionError("quota method")
    invoked.append(method)


def _failure_code(exc: BaseException) -> str:
    """只返回错误码。超时优先于正文里的 auth/login 字样。"""
    if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
        return "PROBE_TIMEOUT"
    if isinstance(exc, JsonRpcError) and exc.code == -32000:
        return "AUTH_REQUIRED"
    message = str(exc).lower()
    if "auth" in message or "login" in message:
        return "AUTH_REQUIRED"
    return "PROBE_FAILED"


def _parse_billing(result, *, scope_ref, observed_at, ttl_s) -> list[dict]:
    body = _billing_body(result)
    if body is None or not _scope_ok(scope_ref):
        return []
    ttl = _ttl_seconds(ttl_s)
    observed = _as_utc(observed_at)
    if ttl is None or observed is None or "creditUsagePercent" not in body:
        return []
    percent = _percent(body.get("creditUsagePercent"))
    if percent is None:
        return []
    end = _moment(body.get("billingPeriodEnd")) if "billingPeriodEnd" in body else None
    start = _moment(body.get("billingPeriodStart")) if "billingPeriodStart" in body else None
    duration = _duration(start, end)
    resets = end if end is not None and end > observed else None
    valid = observed + timedelta(seconds=ttl)
    if resets is not None:
        valid = min(valid, resets)
    if valid <= observed:
        return []
    main = _observation(
        scope_ref,
        bucket_id="grok",
        window_kind="billing_period",
        paid_overage=False,
        observed=observed,
        valid=valid,
        resets=resets,
        duration=duration,
        used_bp=_used_bp(percent),
    )
    observations = [main]
    if _on_demand(body):
        observations.append(_observation(
            scope_ref,
            bucket_id="grok-on-demand",
            window_kind="other",
            paid_overage=True,
            observed=observed,
            valid=valid,
            resets=resets,
            duration=duration,
            used_bp=None,
        ))
    return observations


def _billing_body(result: Any) -> dict | None:
    if not isinstance(result, dict):
        return None
    if "creditUsagePercent" in result:
        return result
    for key in ("billing", "usage"):
        inner = result.get(key)
        if isinstance(inner, dict):
            return inner
    return result


def _observation(
    scope_ref: str,
    *,
    bucket_id: str,
    window_kind: str,
    paid_overage: bool,
    observed: datetime,
    valid: datetime,
    resets: datetime | None,
    duration: int | None,
    used_bp: int | None,
) -> dict:
    item = {
        "scope_ref": scope_ref,
        "bucket_id": bucket_id,
        "window_kind": window_kind,
        "source_kind": "probe_grok",
        "confidence": "medium",
        "paid_overage": paid_overage,
        "observed_at": canonical_utc(observed),
        "valid_until": canonical_utc(valid),
    }
    if used_bp is not None:
        item["used_bp"] = used_bp
    if resets is not None:
        item["resets_at"] = canonical_utc(resets)
    if duration is not None:
        item["window_duration_s"] = duration
    return item


def _on_demand(body: dict) -> bool:
    for key, value in body.items():
        if not isinstance(key, str) or not key.startswith("onDemand"):
            continue
        if value is True:
            return True
        number = _finite(value)
        if number is not None and number > 0:
            return True
    return False


def _duration(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    seconds = (end - start).total_seconds()
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    rounded = round(seconds)
    if type(rounded) is not int or rounded <= 0 or rounded > MAX_INTEGER:
        return None
    return rounded


def _percent(value: Any) -> float | None:
    number = _finite(value)
    if number is None or number < 0 or number > _USED_PERCENT_MAX:
        return None
    return number


def _used_bp(percent: float) -> int:
    scaled = round(percent * 100)
    if type(scaled) is not int:
        scaled = int(scaled)
    return max(0, min(10000, scaled))


def _moment(value: Any) -> datetime | None:
    if isinstance(value, str):
        return _iso_utc(value)
    number = _finite(value)
    if number is None or number <= 0:
        return None
    if number > _MS_THRESHOLD:
        number = number / 1000.0
    try:
        return datetime.fromtimestamp(number, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _iso_utc(value: str) -> datetime | None:
    text = value.strip()
    if not text.endswith(("Z", "+00:00")) or "T" not in text:
        return None
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    offset = parsed.utcoffset()
    if offset is None or offset != timedelta(0):
        return None
    try:
        return parsed.astimezone(timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _finite(value: Any) -> float | None:
    if type(value) is bool:
        return None
    if type(value) is int:
        if abs(value) > 10**15:
            return None
        return float(value)
    if type(value) is not float or not math.isfinite(value):
        return None
    return float(value)


def _ttl_seconds(value: Any) -> float | None:
    if type(value) is bool or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)) or value <= 0:
        return None
    return float(value)


def _as_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _scope_ok(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value) <= 1024
        and "@" not in value
        and all(ord(char) >= 32 for char in value)
    )
