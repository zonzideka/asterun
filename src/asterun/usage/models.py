"""明确单位、窗口与观察来源；未知值保持 null，金额不用浮点。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json

from asterun.errors import AsterunError
from asterun.plugin_api.protocol import json_copy

MAX_INTEGER = 9007199254740991
METERS = frozenset({"requests", "turns", "tokens", "micro_usd", "wall_ms", "tool_actions", "bytes"})
POOL_KEYS = {"billing_mode", "remaining", "meter", "local_limit", "max_concurrency", "window_id",
             "observed_at", "valid_until", "overage_verified", "allow_unknown_subscription",
             "risk_acceptance", "estimate_per_action", "paid_overage_allowed"}


def mapping(value, allowed=None, required=()):
    if not isinstance(value, dict) or (allowed is not None and set(value) - set(allowed)) or set(required) - set(value):
        raise AsterunError("INVALID_REQUEST", "插件持久记录的对象字段不完整或包含未知字段")


def text(value, where="标识", *, nullable=False):
    if nullable and value is None:
        return
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 1024 or any(ord(c) < 32 for c in value):
        raise AsterunError("INVALID_REQUEST", f"{where} 必须为非空规范字符串")


def integer(value, where="计量", *, minimum=0, nullable=False):
    if nullable and value is None:
        return
    if type(value) is not int or not minimum <= value <= MAX_INTEGER:
        raise AsterunError("INVALID_REQUEST", f"{where} 必须为指定范围内的安全整数")


def timestamp(value, where="时间", *, nullable=False):
    if nullable and value is None:
        return None
    try:
        if not isinstance(value, str) or not value.endswith(("Z", "+00:00")) or "T" not in value:
            raise ValueError("UTC required")
        result = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        if result.utcoffset().total_seconds() != 0:
            raise ValueError("UTC required")
        return result
    except (ValueError, TypeError, AttributeError, OverflowError) as exc:
        raise AsterunError("INVALID_REQUEST", f"{where} 必须为带 Z 的 UTC 时间") from exc


def reservation_amounts(value):
    if not isinstance(value, list) or not value:
        raise AsterunError("BUDGET_UNKNOWN", "执行必须预留至少一个有界本地计量池")
    result = {}
    for item in value:
        mapping(item, {"pool_ref", "meter", "amount", "window_id"}, {"pool_ref", "meter", "amount", "window_id"})
        for field in ("pool_ref", "window_id"):
            text(item[field], field)
        if not isinstance(item["meter"], str) or item["meter"] not in METERS:
            raise AsterunError("INVALID_REQUEST", "预算计量单位未注册")
        integer(item["amount"])
        key = (item["pool_ref"], item["meter"], item["window_id"])
        if key in result:
            raise AsterunError("INVALID_REQUEST", "计划计量不能重复")
        result[key] = item["amount"]
    return result


@dataclass(frozen=True, slots=True)
class BillingPool:
    _json: str

    @classmethod
    def from_dict(cls, ref, value):
        text(ref, "pool_ref")
        value = json_copy(value)
        mapping(value, POOL_KEYS, {"billing_mode", "paid_overage_allowed"})
        if not isinstance(value["billing_mode"], str) or value["billing_mode"] not in {"local", "subscription", "metered", "manual", "unknown"}:
            raise AsterunError("INVALID_REQUEST", "计费模式无效")
        for key in ("paid_overage_allowed", "overage_verified", "allow_unknown_subscription"):
            if key in value and type(value[key]) is not bool:
                raise AsterunError("INVALID_REQUEST", "计费政策布尔字段无效")
        if value["paid_overage_allowed"]:
            raise AsterunError("INVALID_REQUEST", "M1 不支持 overage")
        if "meter" in value and (not isinstance(value["meter"], str) or value["meter"] not in METERS):
            raise AsterunError("INVALID_REQUEST", "计费池计量单位未注册")
        for key in ("remaining", "estimate_per_action", "local_limit", "max_concurrency"):
            if key in value:
                integer(value[key], key, minimum=1 if key in {"local_limit", "max_concurrency"} else 0, nullable=key == "remaining")
        if "window_id" in value:
            text(value["window_id"], "window_id")
        for key in ("observed_at", "valid_until"):
            if key in value:
                timestamp(value[key], key, nullable=True)
        if value.get("observed_at") and value.get("valid_until") and timestamp(value["valid_until"]) <= timestamp(value["observed_at"]):
            raise AsterunError("INVALID_REQUEST", "观察有效期必须晚于观察时间")
        if value.get("risk_acceptance") is not None and value["risk_acceptance"] != "local_estimate":
            raise AsterunError("INVALID_REQUEST", "费用风险接受策略无效")
        return cls(json.dumps({"ref": ref, **value}, ensure_ascii=False, sort_keys=True, separators=(",", ":")))

    def to_dict(self):
        return json.loads(self._json)


@dataclass(frozen=True, slots=True)
class UsageObservation:
    """scope_ref 区分同一池不同 run/session 的累计计数器。"""

    _json: str

    @classmethod
    def from_dict(cls, value):
        value = json_copy(value)
        required = {"pool_ref", "meter", "scope", "window_id", "cursor", "source_kind", "observed_at", "used"}
        optional = {"scope_ref", "unit", "limit", "remaining", "valid_until", "sequence", "raw_evidence_ref", "confidence", "cumulative"}
        mapping(value, required | optional, required)
        for key in ("pool_ref", "window_id", "cursor"):
            text(value[key], key)
        if not isinstance(value["meter"], str) or value["meter"] not in METERS:
            raise AsterunError("INVALID_REQUEST", "观察计量单位未注册")
        if not isinstance(value["scope"], str) or value["scope"] not in {"request", "run", "session_cumulative", "billing_window"}:
            raise AsterunError("INVALID_REQUEST", "观察 scope 无效")
        source = value["source_kind"]
        if not isinstance(source, str):
            raise AsterunError("INVALID_REQUEST", "观察来源无效")
        value["source_kind"] = source.replace("-", "_")
        if value["source_kind"] not in {"provider_observed", "local_estimated", "billed", "user_claim", "unknown"}:
            raise AsterunError("INVALID_REQUEST", "观察来源无效")
        value.setdefault("scope_ref", "pool" if value["scope"] == "billing_window" else None)
        text(value["scope_ref"], "scope_ref")
        value.setdefault("unit", value["meter"])
        if value["unit"] != value["meter"]:
            raise AsterunError("INVALID_REQUEST", "M1 观察 unit 必须与明确计量 meter 相同")
        for key in ("used", "limit", "remaining"):
            if key in value:
                integer(value[key], key, nullable=True)
        if "sequence" in value:
            integer(value["sequence"], "sequence")
        for key in ("observed_at", "valid_until"):
            if key in value:
                timestamp(value[key], key, nullable=key == "valid_until")
        if value.get("valid_until") and timestamp(value["valid_until"]) <= timestamp(value["observed_at"]):
            raise AsterunError("INVALID_REQUEST", "观察有效期无效")
        if "raw_evidence_ref" in value:
            text(value["raw_evidence_ref"], "raw_evidence_ref", nullable=True)
        if "confidence" in value and value["confidence"] is not None and (not isinstance(value["confidence"], str) or value["confidence"] not in {"high", "medium", "low", "unknown"}):
            raise AsterunError("INVALID_REQUEST", "观察 confidence 无效")
        cumulative = value["scope"] in {"session_cumulative", "billing_window"}
        value.setdefault("cumulative", cumulative)
        if type(value["cumulative"]) is not bool or value["cumulative"] != cumulative:
            raise AsterunError("INVALID_REQUEST", "cumulative 必须与观察 scope 一致")
        return cls(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))

    def to_dict(self):
        return json.loads(self._json)
