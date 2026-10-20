"""QuotaWindowObs 的校验、新鲜度与同键合并。dict 进、dict 出，不写库、不探活。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Any

from asterun.errors import INVALID_REQUEST, AsterunError
from asterun.plugin_api.protocol import json_copy
from asterun.usage.models import integer, text, timestamp

SCHEMA_VERSION = 1
WINDOW_KINDS = frozenset({"5h", "weekly", "monthly", "billing_period", "latch", "other"})
CONFIDENCE = frozenset({"high", "medium", "low"})
FRESHNESS_STATES = frozenset({"fresh", "stale", "unknown", "exhausted", "latched", "absent"})
CONFLICT_BP = 1000
CONFLICT_WINDOW = timedelta(minutes=5)
CLOCK_FUTURE = timedelta(minutes=2)
CLOCK_SLACK = timedelta(minutes=5)
MANUAL_MAX_TTL = timedelta(hours=24)
_SOURCE = re.compile(r"^(?:probe_[A-Za-z0-9._-]{1,64}|passive_[A-Za-z0-9._-]{1,64}|manual|latch)$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ERROR_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_REQUIRED = {"scope_ref", "bucket_id", "window_kind", "source_kind", "confidence", "paid_overage", "observed_at", "valid_until"}
_OPTIONAL = {
    "window_duration_s", "used_bp", "resets_at", "window_epoch", "native_time", "exhausted_hint",
    "expiring_grants", "adapter_version", "native_version", "schema_version", "raw_digest",
}
_SECRET_KEYS = {
    "env", "environment", "secret", "secrets", "credentials", "api_key", "apikey",
    "access_token", "refresh_token", "authorization", "password", "token", "client_secret",
    "private_key", "email", "e_mail", "balance", "account_id",
}
_SECRET_PARTS = {"email", "token", "secret", "password", "balance", "credential", "credentials"}
_SECRET_SUFFIXES = ("_api_key", "_token", "_secret", "_password", "_email", "_balance")
_PROBE_FIELDS = {"scope_ref", "probe_id", "last_attempt_at", "last_ok_at", "last_error_code", "lease_until"}


def _fail(message: str, **details: Any) -> None:
    raise AsterunError(INVALID_REQUEST, message, details=details or None)


def _secret_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    if normalized in _SECRET_KEYS or normalized.endswith(_SECRET_SUFFIXES):
        return True
    return bool(set(normalized.split("_")) & _SECRET_PARTS)


def _reject_sensitive(value: Any) -> None:
    """白名单之外再挡 email/token/balance 一类键，以及任何含 @ 的字符串。"""
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or "@" in key or _secret_key(key):
                _fail("额度快照不能包含凭据、邮箱、令牌或余额字段")
            _reject_sensitive(child)
    elif isinstance(value, list):
        for child in value:
            _reject_sensitive(child)
    elif isinstance(value, str) and "@" in value:
        _fail("额度快照不能包含邮箱或含 @ 的字符串")


def canonical_utc(value: datetime) -> str:
    value = value.astimezone(timezone.utc).replace(tzinfo=None)
    spec = "microseconds" if value.microsecond else "seconds"
    return value.isoformat(timespec=spec) + "Z"


def window_epoch_from(resets_at: datetime | None) -> str | None:
    """resets_at 截到 UTC 分钟，作为窗口身份。"""
    if resets_at is None:
        return None
    truncated = resets_at.astimezone(timezone.utc).replace(second=0, microsecond=0)
    return truncated.strftime("%Y-%m-%dT%H:%M:%SZ")


def _optional_text(value: dict, key: str) -> str | None:
    if key not in value or value[key] is None:
        return None
    text(value[key], key)
    return value[key]


def _optional_time(value: dict, key: str) -> str | None:
    if key not in value or value[key] is None:
        return None
    return canonical_utc(timestamp(value[key], key))


def normalize_observation(value: Any) -> dict[str, Any]:
    """严格白名单。window_epoch 由 resets_at 推导；native_time 只保存，不参与计算。"""
    if not isinstance(value, dict):
        _fail("额度窗口必须是对象")
    value = json_copy(value)
    _reject_sensitive(value)
    unknown = set(value) - (_REQUIRED | _OPTIONAL)
    if unknown:
        _fail("额度窗口含有未知字段", unknown=sorted(unknown))
    missing = _REQUIRED - set(value)
    if missing:
        _fail("额度窗口缺少字段", missing=sorted(missing))
    for key in ("scope_ref", "bucket_id"):
        text(value[key], key)
    if value["window_kind"] not in WINDOW_KINDS:
        _fail("window_kind 无效")
    if not isinstance(value["source_kind"], str) or _SOURCE.fullmatch(value["source_kind"]) is None:
        _fail("source_kind 必须是 probe_*、passive_*、manual 或 latch")
    if value["confidence"] not in CONFIDENCE:
        _fail("confidence 无效")
    if type(value["paid_overage"]) is not bool:
        _fail("paid_overage 必须为布尔值")
    if value["source_kind"] == "manual":
        if value["confidence"] != "low":
            _fail("手动录入的 confidence 必须为 low")
        if value["paid_overage"] is not False:
            _fail("手动录入的 paid_overage 必须为 false")
        if value["window_kind"] == "latch":
            _fail("手动录入不能创建闩锁")
    if "used_bp" in value:
        integer(value["used_bp"], "used_bp", nullable=True)
        if value["used_bp"] is not None and value["used_bp"] > 10000:
            _fail("used_bp 必须在 0 到 10000 之间")
    used_bp = value.get("used_bp")
    if "window_duration_s" in value:
        integer(value["window_duration_s"], "window_duration_s", nullable=True)
    duration = value.get("window_duration_s")
    observed = timestamp(value["observed_at"], "observed_at")
    valid = timestamp(value["valid_until"], "valid_until")
    if valid <= observed:
        _fail("valid_until 必须晚于 observed_at")
    resets = timestamp(value["resets_at"], "resets_at", nullable=True) if "resets_at" in value else None
    if resets is not None and valid > resets:
        _fail("valid_until 不能晚于 resets_at")
    if value["source_kind"] == "manual" and valid - observed > MANUAL_MAX_TTL:
        _fail("手动录入的有效期不能超过 24 小时")
    epoch = window_epoch_from(resets)
    if "window_epoch" in value and value["window_epoch"] is not None:
        supplied = timestamp(value["window_epoch"], "window_epoch")
        if canonical_utc(supplied) != epoch:
            _fail("window_epoch 必须是 resets_at 截到分钟")
    if resets is None and value.get("window_epoch") is not None:
        _fail("没有 resets_at 时 window_epoch 必须为空")
    hint = _optional_text(value, "exhausted_hint")
    native_time = _optional_text(value, "native_time")
    adapter = _optional_text(value, "adapter_version")
    native_version = _optional_text(value, "native_version")
    grants = value.get("expiring_grants", [])
    if not isinstance(grants, list) or len(grants) > 64:
        _fail("expiring_grants 必须是最多 64 项的数组")
    normalized_grants = []
    for item in grants:
        if not isinstance(item, dict) or set(item) != {"count", "expires_at", "kind"}:
            _fail("expiring_grants 每项只能包含 count、expires_at、kind")
        integer(item["count"], "expiring_grants.count")
        text(item["kind"], "expiring_grants.kind")
        normalized_grants.append({
            "count": item["count"],
            "expires_at": canonical_utc(timestamp(item["expires_at"], "expires_at")),
            "kind": item["kind"],
        })
    if "schema_version" in value and type(value["schema_version"]) is not int:
        _fail("schema_version 必须为 1")
    if value.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        _fail("schema_version 必须为 1")
    raw_digest = value.get("raw_digest")
    if "raw_digest" in value and raw_digest is not None:
        if not isinstance(raw_digest, str) or _DIGEST.fullmatch(raw_digest) is None:
            _fail("raw_digest 必须是 64 位小写十六进制摘要或 null")
    else:
        raw_digest = None
    return {
        "scope_ref": value["scope_ref"],
        "bucket_id": value["bucket_id"],
        "window_kind": value["window_kind"],
        "window_duration_s": duration if "window_duration_s" in value else None,
        "used_bp": used_bp if "used_bp" in value else None,
        "resets_at": None if resets is None else canonical_utc(resets),
        "window_epoch": epoch,
        "observed_at": canonical_utc(observed),
        "native_time": native_time,
        "source_kind": value["source_kind"],
        "confidence": value["confidence"],
        "paid_overage": value["paid_overage"],
        "exhausted_hint": hint,
        "expiring_grants": normalized_grants,
        "valid_until": canonical_utc(valid),
        "adapter_version": adapter,
        "native_version": native_version,
        "schema_version": SCHEMA_VERSION,
        "raw_digest": raw_digest,
    }


def normalize_probe_state(value: Any) -> dict[str, Any]:
    """探针单飞状态。PR1 只校验并读写，不发起探针。"""
    if not isinstance(value, dict):
        _fail("探针状态必须是对象")
    value = json_copy(value)
    _reject_sensitive(value)
    unknown = set(value) - _PROBE_FIELDS
    if unknown or {"scope_ref", "probe_id"} - set(value):
        _fail("探针状态字段不完整或包含未知字段")
    text(value["scope_ref"], "scope_ref")
    text(value["probe_id"], "probe_id")
    result = {"scope_ref": value["scope_ref"], "probe_id": value["probe_id"]}
    for key in ("last_attempt_at", "last_ok_at", "lease_until"):
        result[key] = _optional_time(value, key)
    code = value.get("last_error_code")
    if code is not None and (not isinstance(code, str) or _ERROR_CODE.fullmatch(code) is None):
        _fail("last_error_code 必须是简短错误码或 null")
    result["last_error_code"] = code if code is not None else None
    return result


def used_bp_from_percent(value: Any) -> int:
    """0..100，最多两位小数，换算成万分比整数。不用二进制浮点。"""
    if not isinstance(value, str) or re.fullmatch(r"(?:100(?:\.0{1,2})?|[0-9]{1,2}(?:\.[0-9]{1,2})?)", value) is None:
        _fail("used_percent 必须是 0 到 100 之间、最多两位小数的数字字符串")
    whole, _, frac = value.partition(".")
    return int(whole) * 100 + int((frac + "00")[:2])


def _coerce_now(now: datetime | str) -> datetime:
    if isinstance(now, datetime):
        if now.tzinfo is None or now.utcoffset() is None:
            _fail("freshness 的 now 必须是 UTC 时间")
        return now.astimezone(timezone.utc)
    return timestamp(now, "now")


def freshness(obs: dict[str, Any] | None, now: datetime | str) -> dict[str, Any]:
    """fresh 当且仅当 now < valid_until 且（resets_at 为空或 now < resets_at），且不是闩锁或耗尽。

    过了 resets_at 视为 unknown，不假设重置为 0。时钟不一致时 unknown，issue 为 clock_inconsistent。
    闩锁在窗口翻转前保持 latched（无 resets_at 时一直保持，直到手动清除），不受 valid_until 短 TTL 影响。
    """
    now_dt = _coerce_now(now)
    if obs is None:
        return {"state": "absent", "issues": [], "age_s": None}
    if not isinstance(obs, dict):
        return {"state": "unknown", "issues": [], "age_s": None}
    try:
        observed = timestamp(obs.get("observed_at"), "observed_at")
    except AsterunError:
        return {"state": "unknown", "issues": [], "age_s": None}
    age = int((now_dt - observed).total_seconds())
    try:
        valid = timestamp(obs.get("valid_until"), "valid_until")
        resets = timestamp(obs.get("resets_at"), "resets_at", nullable=True)
    except AsterunError:
        return {"state": "unknown", "issues": [], "age_s": age}
    duration = obs.get("window_duration_s")
    clock = resets is not None and resets < observed
    if resets is not None and type(duration) is int and duration >= 0:
        clock = clock or (resets - observed).total_seconds() > duration + CLOCK_SLACK.total_seconds()
    if (observed - now_dt).total_seconds() > CLOCK_FUTURE.total_seconds():
        clock = True
    if clock:
        return {"state": "unknown", "issues": ["clock_inconsistent"], "age_s": age}
    if resets is not None and now_dt >= resets:
        return {"state": "unknown", "issues": ["window_flipped"], "age_s": age}
    # 闩锁持续到 resets_at；没有重置时间就持续到手动解除，不因 valid_until 过期而变成 stale。
    if obs.get("window_kind") == "latch" or obs.get("source_kind") == "latch":
        return {"state": "latched", "issues": [], "age_s": age}
    time_fresh = now_dt < valid
    hint = obs.get("exhausted_hint")
    used = obs.get("used_bp")
    if time_fresh and ((isinstance(hint, str) and hint) or (type(used) is int and used >= 10000)):
        return {"state": "exhausted", "issues": [], "age_s": age}
    if time_fresh:
        return {"state": "fresh", "issues": [], "age_s": age}
    return {"state": "stale", "issues": [], "age_s": age}


def merge_observations(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一 (scope_ref, bucket_id, window_kind) 取 observed_at 最新。

    只在同一窗口 epoch 内判来源冲突。距这条最新观测不超过 5 分钟、且窗口标记未表明属于另一
    epoch 的观测中，不同来源的 used_bp 相差超过 1000 时，改取较大的 used_bp，并标 source_conflict。
    """
    if not isinstance(observations, list):
        _fail("额度合并输入必须是数组")
    groups: dict[tuple, list] = {}
    for obs in observations:
        if not isinstance(obs, dict):
            _fail("额度合并输入必须是窗口对象")
        groups.setdefault((obs.get("scope_ref"), obs.get("bucket_id"), obs.get("window_kind")), []).append(obs)
    return [_merge_group(groups[key]) for key in sorted(groups, key=lambda item: tuple("" if part is None else str(part) for part in item))]


def _observed(obs: dict) -> datetime:
    return timestamp(obs.get("observed_at"), "observed_at")


def _reset_moment(obs: dict) -> datetime | None:
    raw = obs.get("resets_at")
    if raw is None:
        return None
    return timestamp(raw, "resets_at")


def _window_marker(obs: dict) -> str | None:
    """重置窗口身份。已有 window_epoch 时用它，否则把 resets_at 截到分钟。"""
    epoch = obs.get("window_epoch")
    if isinstance(epoch, str) and epoch:
        return epoch
    moment = _reset_moment(obs)
    if moment is None:
        return None
    return window_epoch_from(moment)


def _different_windows(left: dict, right: dict) -> bool:
    left_marker, right_marker = _window_marker(left), _window_marker(right)
    return left_marker is not None and right_marker is not None and left_marker != right_marker


def _merge_group(group: list[dict]) -> dict[str, Any]:
    ordered = sorted(group, key=lambda obs: (
        _observed(obs), str(obs.get("source_kind")), obs["used_bp"] if type(obs.get("used_bp")) is int else -1))
    latest = ordered[-1]
    latest_at = _observed(latest)
    recent = [
        obs for obs in ordered
        if latest_at - _observed(obs) <= CONFLICT_WINDOW and not _different_windows(obs, latest)
    ]
    conflict = False
    for index, left in enumerate(recent):
        for right in recent[index + 1:]:
            if left.get("source_kind") == right.get("source_kind") or _different_windows(left, right):
                continue
            left_bp, right_bp = left.get("used_bp"), right.get("used_bp")
            if type(left_bp) is int and type(right_bp) is int and abs(left_bp - right_bp) > CONFLICT_BP:
                conflict = True
                break
        if conflict:
            break
    if conflict:
        numeric = [obs for obs in recent if type(obs.get("used_bp")) is int]
        winner = max(numeric, key=lambda obs: (obs["used_bp"], _observed(obs), str(obs.get("source_kind"))))
        issues = ["source_conflict"]
    else:
        winner, issues = latest, []
    return {"observation": dict(winner), "issues": issues}
