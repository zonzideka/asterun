"""Codex app-server 只读额度探针。只调用 account/rateLimits/read，不建线程、不读账户。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import re
import shutil
import tempfile
import time
from typing import Any

from asterun.backends.codex import CodexAppServerTransport, discover_codex_bin, live_codex_enabled
from asterun.errors import AsterunError
from asterun.quota.models import canonical_utc
from asterun.usage.models import timestamp

_BUCKET = re.compile(r"^[A-Za-z0-9_.-]{1,1024}$")
_SOURCE = re.compile(r"^(?:probe|passive)_[A-Za-z0-9._-]{1,64}$")
_REACHED = re.compile(r"^[a-z_]{1,32}$")
_ROLES = frozenset({"primary", "secondary"})
_SNAPSHOT_KEYS = ("rateLimits", "rateLimitsByLimitId", "credits", "rateLimitResetCredits", "rateLimitReachedType")
_MAX_INTEGER = 9007199254740991
_FIVE_HOUR_MINUTES = 300
_WEEKLY_MINUTES = 10080
_USED_PERCENT_MAX = 1000
_REACHED_LIMIT = 8
# close() 最多约 5 秒（TERM 2 秒、KILL 2 秒、reader join 1 秒）。租约留更大的关闭余量，启动另计。
CLOSE_GRACE_S = 10
SPAWN_GRACE_S = 5


def probe_lifetime_s(timeout_s: float) -> float:
    """租约长度。最大 timeout 30 秒时为 45 秒，短于 60 秒最小间隔。"""
    return timeout_s + SPAWN_GRACE_S + CLOSE_GRACE_S


def scrub_rate_limits(params) -> dict:
    """只保留速率窗口、积分开关和重置券到期时间。余额、邮箱、套餐和其他字段丢掉。

    通知可能与 account/rateLimits/read 的结果相同，也可能再把这份结果包进 rateLimits。
    """
    body = _snapshot(params)
    if not body:
        return {}
    kept: dict[str, Any] = {}
    limits = _scrub_limit_body(body.get("rateLimits"))
    if limits:
        kept["rateLimits"] = limits
    by_id = body.get("rateLimitsByLimitId")
    if isinstance(by_id, dict):
        scrubbed_ids = {}
        for limit_id, limit_body in by_id.items():
            if not isinstance(limit_id, str) or _BUCKET.fullmatch(limit_id) is None:
                continue
            windows = _scrub_limit_body(limit_body)
            if windows:
                scrubbed_ids[limit_id] = windows
        if scrubbed_ids:
            kept["rateLimitsByLimitId"] = scrubbed_ids
    credits = _scrub_credits(body.get("credits"))
    if credits:
        kept["credits"] = credits
    grants = _scrub_grants(body.get("rateLimitResetCredits"))
    if grants:
        kept["rateLimitResetCredits"] = grants
    reached = _scrub_reached(body.get("rateLimitReachedType"))
    if reached is not None:
        kept["rateLimitReachedType"] = reached
    return kept


def parse_rate_limits(
    result: dict,
    *,
    scope_ref,
    observed_at: datetime,
    ttl_s: float = 600,
    source_kind: str = "probe_codex",
    native_version: str | None = None,
) -> list[dict]:
    """把 account/rateLimits/read 的结果收成观测 dict。结构不符时返回空列表，不抛异常。

    不读取、不保存 email、planType、balance，以及其他账户字段。
    """
    if not isinstance(result, dict):
        return []
    try:
        return _parse_rate_limits(
            result,
            scope_ref=scope_ref,
            observed_at=observed_at,
            ttl_s=ttl_s,
            source_kind=source_kind,
            native_version=native_version,
        )
    except Exception:
        return []


def probe_codex(
    backend_config,
    *,
    scope_ref,
    now,
    timeout_s: float = 10.0,
    ttl_s: float = 600,
    transport_factory=CodexAppServerTransport,
) -> dict:
    """只读速率窗口。不调用 account/read、thread/* 或 turn/*。"""
    if not live_codex_enabled():
        return {"ok": False, "error_code": "LIVE_DISABLED"}
    try:
        binary = discover_codex_bin(backend_config.bin)
    except Exception:
        return {"ok": False, "error_code": "PROBE_FAILED"}
    if binary is None:
        return {"ok": False, "error_code": "BINARY_NOT_FOUND"}
    transport = None
    tmpdir = None
    try:
        transport = transport_factory(codex_bin=str(binary), extra_env=backend_config.extra_env)
        tmpdir = tempfile.mkdtemp(prefix="asterun-codex-quota-")
        transport.spawn(tmpdir)
        # 两次 RPC 共用 timeout_s。启动和关闭不占这段时间，由租约里的 grace 覆盖。
        deadline = time.monotonic() + timeout_s
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"ok": False, "error_code": "PROBE_TIMEOUT"}
        initialized = transport.initialize(remaining)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"ok": False, "error_code": "PROBE_TIMEOUT"}
        payload = transport.read_rate_limits(remaining)
        version = _version_from_initialize(initialized)
        observations = parse_rate_limits(
            payload,
            scope_ref=scope_ref,
            observed_at=now,
            ttl_s=ttl_s,
            native_version=version,
        )
        return {"ok": True, "observations": observations, "native_version": version}
    except Exception:
        return {"ok": False, "error_code": "PROBE_FAILED"}
    finally:
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass
        if tmpdir is not None:
            shutil.rmtree(tmpdir, ignore_errors=True)


def _snapshot(params: Any) -> dict:
    if not isinstance(params, dict):
        return {}
    inner = params.get("rateLimits")
    if isinstance(inner, dict) and _is_snapshot(inner) and not _is_window_map(inner):
        return inner
    if _is_snapshot(params):
        return params
    return {}


def _is_snapshot(value: dict) -> bool:
    return any(key in value for key in _SNAPSHOT_KEYS)


def _is_window_map(value: dict) -> bool:
    return any(key in value for key in ("primary", "secondary")) and "rateLimits" not in value


def _scrub_window(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    kept: dict[str, Any] = {}
    if "usedPercent" in raw:
        used = _finite_number(raw["usedPercent"])
        if used is not None and 0 <= used <= _USED_PERCENT_MAX:
            kept["usedPercent"] = raw["usedPercent"]
    if "windowDurationMins" in raw and _positive_int(raw["windowDurationMins"]):
        kept["windowDurationMins"] = raw["windowDurationMins"]
    if "resetsAt" in raw:
        resets = _finite_number(raw["resetsAt"])
        if resets is not None and resets > 0:
            kept["resetsAt"] = raw["resetsAt"]
    return kept or None


def _scrub_limit_body(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    kept = {}
    for role in ("primary", "secondary"):
        if role not in raw:
            continue
        window = _scrub_window(raw[role])
        if window is not None:
            kept[role] = window
    return kept or None


def _scrub_credits(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    kept = {key: raw[key] for key in ("hasCredits", "unlimited") if key in raw and type(raw[key]) is bool}
    return kept or None


def _scrub_grants(raw: Any) -> list[dict] | None:
    if not isinstance(raw, list):
        return None
    grants = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kept = {}
        for key in ("expiresAt", "expires_at"):
            if key not in item:
                continue
            seconds = _grant_unix_seconds(item[key])
            if seconds is not None:
                kept[key] = seconds
        if kept:
            grants.append(kept)
    return grants or None


def _scrub_reached(raw: Any) -> str | list[str] | None:
    if isinstance(raw, str):
        return raw if _REACHED.fullmatch(raw) else None
    if (
        isinstance(raw, list)
        and len(raw) <= _REACHED_LIMIT
        and all(isinstance(item, str) and _REACHED.fullmatch(item) for item in raw)
    ):
        return list(raw)
    return None


def _parse_rate_limits(result, *, scope_ref, observed_at, ttl_s, source_kind, native_version) -> list[dict]:
    if not isinstance(scope_ref, str) or not _plain_text(scope_ref):
        return []
    if not isinstance(source_kind, str) or _SOURCE.fullmatch(source_kind) is None:
        return []
    if isinstance(ttl_s, bool) or not isinstance(ttl_s, (int, float)) or not math.isfinite(ttl_s) or ttl_s <= 0:
        return []
    observed = _as_utc(observed_at)
    version = _plain_version(native_version)
    reached = _reached_roles(result.get("rateLimitReachedType"))
    windows: list[tuple[dict, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(bucket_id: str, role: str, raw: Any) -> None:
        observation = _window_observation(
            raw,
            scope_ref=scope_ref,
            bucket_id=bucket_id,
            role=role,
            observed=observed,
            ttl_s=float(ttl_s),
            source_kind=source_kind,
            native_version=version,
            reached=reached,
        )
        if observation is None:
            return
        key = (observation["bucket_id"], observation["window_kind"])
        if key in seen:
            return
        seen.add(key)
        windows.append((observation, role))

    limits = result.get("rateLimits")
    if isinstance(limits, dict):
        add("codex", "primary", limits.get("primary"))
        add("codex", "secondary", limits.get("secondary"))
    by_id = result.get("rateLimitsByLimitId")
    if isinstance(by_id, dict):
        for limit_id, body in by_id.items():
            if not isinstance(limit_id, str) or _BUCKET.fullmatch(limit_id) is None or not isinstance(body, dict):
                continue
            add(limit_id, "primary", body.get("primary"))
            add(limit_id, "secondary", body.get("secondary"))

    observations = [item for item, _role in windows]
    credits = _credits_observation(
        result.get("credits"),
        scope_ref=scope_ref,
        observed=observed,
        ttl_s=float(ttl_s),
        source_kind=source_kind,
        native_version=version,
    )
    if credits is not None and (credits["bucket_id"], credits["window_kind"]) not in seen:
        observations.append(credits)
    grants = _reset_grants(result.get("rateLimitResetCredits"))
    if grants:
        target = next((item for item, role in windows if role == "primary"), None)
        if target is None and observations:
            target = observations[0]
        if target is not None:
            target["expiring_grants"] = grants
    return observations


def _window_observation(
    raw,
    *,
    scope_ref,
    bucket_id,
    role,
    observed,
    ttl_s,
    source_kind,
    native_version,
    reached,
) -> dict | None:
    if not isinstance(raw, dict) or not isinstance(bucket_id, str) or _BUCKET.fullmatch(bucket_id) is None:
        return None
    used = _finite(raw.get("usedPercent"))
    minutes = _finite(raw.get("windowDurationMins"))
    resets = _unix_datetime(raw.get("resetsAt"))
    if used is None or minutes is None or minutes < 0 or resets is None or resets <= observed:
        return None
    if minutes == _FIVE_HOUR_MINUTES:
        window_kind = "5h"
        duration = None
    elif minutes == _WEEKLY_MINUTES:
        window_kind = "weekly"
        duration = None
    else:
        window_kind = "other"
        duration = round(minutes * 60)
        if not isinstance(duration, int) or duration < 0 or duration > _MAX_INTEGER:
            return None
    valid = min(observed + timedelta(seconds=ttl_s), resets)
    if valid <= observed:
        return None
    observation = {
        "scope_ref": scope_ref,
        "bucket_id": bucket_id,
        "window_kind": window_kind,
        "source_kind": source_kind,
        "confidence": "high",
        "paid_overage": False,
        "observed_at": canonical_utc(observed),
        "valid_until": canonical_utc(valid),
        "used_bp": _used_bp(used),
        "resets_at": canonical_utc(resets),
    }
    if duration is not None:
        observation["window_duration_s"] = duration
    if native_version is not None:
        observation["native_version"] = native_version
    if role in reached and used >= 100:
        observation["exhausted_hint"] = "exhausted"
    return observation


def _credits_observation(raw, *, scope_ref, observed, ttl_s, source_kind, native_version) -> dict | None:
    if not isinstance(raw, dict) or raw.get("hasCredits") is not True:
        return None
    valid = observed + timedelta(seconds=ttl_s)
    if valid <= observed:
        return None
    observation = {
        "scope_ref": scope_ref,
        "bucket_id": "codex-credits",
        "window_kind": "other",
        "source_kind": source_kind,
        "confidence": "high",
        "paid_overage": True,
        "observed_at": canonical_utc(observed),
        "valid_until": canonical_utc(valid),
    }
    if native_version is not None:
        observation["native_version"] = native_version
    return observation


def _reset_grants(raw) -> list[dict]:
    if not isinstance(raw, list):
        return []
    grouped: dict[str, int] = {}
    order: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        if "expiresAt" in item and item.get("expiresAt") is not None:
            moment = _grant_time(item.get("expiresAt"))
        elif "expires_at" in item:
            moment = _grant_time(item.get("expires_at"))
        else:
            continue
        if moment is None:
            continue
        key = canonical_utc(moment)
        if key not in grouped:
            order.append(key)
            grouped[key] = 0
        grouped[key] += 1
        if len(order) > 64:
            break
    return [
        {"count": grouped[key], "expires_at": key, "kind": "codex_full_reset"}
        for key in order[:64]
    ]


def _grant_time(value) -> datetime | None:
    if isinstance(value, str):
        try:
            return timestamp(value, "expires_at")
        except AsterunError:
            return None
    return _unix_datetime(value)


def _reached_roles(value) -> set[str]:
    if isinstance(value, str):
        return {value} if value in _ROLES else set()
    if isinstance(value, list):
        return {item for item in value if item in _ROLES}
    return set()


def _used_bp(used: float) -> int:
    scaled = round(used * 100)
    if not isinstance(scaled, int):
        scaled = int(scaled)
    return max(0, min(10000, scaled))


def _finite(value) -> float | None:
    return _finite_number(value)


def _finite_number(value) -> float | None:
    try:
        if type(value) is int:
            if abs(value) > 10**15:
                return None
        elif type(value) is not float:
            return None
        if not math.isfinite(value):
            return None
        return float(value)
    except (OverflowError, ValueError):
        return None


def _positive_int(value) -> bool:
    return type(value) is int and value > 0


def _grant_unix_seconds(value) -> int | None:
    """解析成时间后再写成 int unix 秒。数字和 ISO 字符串都可以，失败则丢掉。"""
    if type(value) in {int, float}:
        if _finite_number(value) is None or value <= 0:
            return None
        moment = _unix_datetime(value)
    elif isinstance(value, str):
        moment = _parse_iso_utc(value)
    else:
        return None
    if moment is None:
        return None
    try:
        seconds = round(moment.timestamp())
    except (OverflowError, OSError, ValueError):
        return None
    if type(seconds) is not int or seconds <= 0:
        return None
    return seconds


def _parse_iso_utc(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.astimezone(timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _unix_datetime(value) -> datetime | None:
    number = _finite(value)
    if number is None:
        return None
    try:
        return datetime.fromtimestamp(number, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _as_utc(value) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("observed_at 必须是时间")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _plain_text(value: str) -> bool:
    return bool(value) and value == value.strip() and len(value) <= 1024 and all(ord(char) >= 32 for char in value)


def _plain_version(value) -> str | None:
    if not isinstance(value, str) or not _plain_text(value) or "@" in value:
        return None
    return value


def _version_from_initialize(initialized) -> str | None:
    if not isinstance(initialized, dict):
        return None
    user_agent = _plain_version(initialized.get("userAgent"))
    if user_agent is not None:
        return user_agent
    info = initialized.get("serverInfo")
    if isinstance(info, dict):
        return _plain_version(info.get("version"))
    return None
