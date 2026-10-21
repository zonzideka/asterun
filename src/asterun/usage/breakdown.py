"""把已保存的原生用量归一成四分量观测。不改写原记录，也不把缺失记成 0。"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any


SCHEMA = "asterun.usage_breakdown/v1"
_DISPATCH_KEY = "asterun.antigravity_dispatch_v1"
_CUMULATIVE_SCOPES = {"session", "session_cumulative", "thread_cumulative"}
_RESIDENCY_LIMIT = 500
_RESIDENCY_FIELDS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _component(raw: dict, key: str, issues: list[str]) -> tuple[int | None, bool]:
    """返回 (计数或 None, 键是否出现)。负数、bool、非整数视为缺失，不当作 0。"""
    if key not in raw:
        return None, False
    value = raw[key]
    if type(value) is int and value >= 0:
        return value, True
    issues.append("invalid_counter:" + key)
    return None, True


def _picture(*, convention: str, scope: str, attributable: bool, input_uncached: int | None,
             cache_read: int | None, cache_write: int | None, output: int | None,
             reasoning: int | None, inclusion: str, issues: list[str],
             reasoning_subset: bool) -> dict:
    if inclusion == "mismatch":
        # 包含关系对不上时不保留可能套错口径的分量，避免汇总把错数加进去。
        input_uncached = cache_read = cache_write = output = reasoning = None
        processed = new = None
        completeness = "unknown"
    else:
        if reasoning_subset and reasoning is not None and output is not None and reasoning > output:
            issues.append("reasoning_exceeds_output")
        if input_uncached is None or cache_read is None or output is None:
            processed = new = None
            completeness = "unknown"
        elif cache_write is None:
            processed = input_uncached + cache_read + output
            new = input_uncached + output
            completeness = "partial"
        else:
            processed = input_uncached + cache_read + cache_write + output
            new = input_uncached + cache_write + output
            completeness = "complete"
    return {"schema": SCHEMA, "convention": convention, "scope": scope, "attributable": attributable,
            "input_uncached": input_uncached, "cache_read": cache_read, "cache_write": cache_write,
            "output": output, "reasoning": reasoning, "processed": processed, "new": new,
            "inclusion_check": inclusion, "completeness": completeness, "issues": issues}


def _unknown() -> dict:
    return _picture(convention="unknown", scope="unknown", attributable=False, input_uncached=None,
                    cache_read=None, cache_write=None, output=None, reasoning=None,
                    inclusion="unverified", issues=["usage_convention_unknown"], reasoning_subset=False)


def _grok(native: dict) -> dict:
    raw = _mapping(native.get("usage"))
    issues: list[str] = []
    reported_input, _ = _component(raw, "input_tokens", issues)
    cache_read, read_present = _component(raw, "cache_read_input_tokens", issues)
    cache_write, write_present = _component(raw, "cache_creation_input_tokens", issues)
    output, _ = _component(raw, "output_tokens", issues)
    reasoning, _ = _component(raw, "reasoning_tokens", issues)
    total, _ = _component(raw, "total_tokens", issues)
    input_uncached = reported_input
    inclusion = "unverified"
    if total is not None:
        four = (reported_input, cache_read, cache_write, output)
        pair = reported_input is not None and output is not None
        # 先按未缓存 input 核对四项；只有总数等于 input+output 时才改判 input 含缓存。
        if all(value is not None for value in four) and total == sum(four):
            inclusion = "verified"
        elif pair and cache_read is not None and cache_write is not None and total == reported_input + output:
            split = reported_input - cache_read - cache_write
            if split < 0:
                issues.append("input_uncached_negative")
                inclusion = "mismatch"
            else:
                input_uncached = split
                inclusion = "verified"
        elif (pair and cache_read is not None and cache_write is None and not write_present
              and total == reported_input + cache_read + output):
            inclusion = "verified"
        elif (pair and cache_read is None and cache_write is None and not read_present and not write_present
              and total == reported_input + output):
            inclusion = "unverified"
        else:
            issues.append("inclusion_mismatch")
            inclusion = "mismatch"
    return _picture(convention="grok_headless", scope="run", attributable=True,
                    input_uncached=input_uncached, cache_read=cache_read, cache_write=cache_write,
                    output=output, reasoning=reasoning, inclusion=inclusion, issues=issues,
                    reasoning_subset=True)


def _codex(native: dict) -> dict:
    issues: list[str] = []
    notification = _mapping(native.get("token_usage"))
    token_usage = notification.get("tokenUsage")
    if not isinstance(token_usage, dict) or not token_usage:
        issues.append("unrecognized_codex_usage_shape")
        token_usage = {}
    raw = _mapping(token_usage.get("total"))
    reported_input, _ = _component(raw, "inputTokens", issues)
    cache_read, _ = _component(raw, "cachedInputTokens", issues)
    # total 只核对 input+output。cacheWriteInputTokens 不在等式里，计入会让
    # processed 偏离已核对总数，并把未覆盖分量标成 verified/complete。
    output, _ = _component(raw, "outputTokens", issues)
    reasoning, _ = _component(raw, "reasoningOutputTokens", issues)
    total, _ = _component(raw, "totalTokens", issues)
    input_uncached = None
    inclusion = "unverified"
    if reported_input is not None and cache_read is not None and reported_input < cache_read:
        issues.append("cached_input_exceeds_input")
        inclusion = "mismatch"
    elif reported_input is not None and cache_read is not None:
        input_uncached = reported_input - cache_read
    if inclusion != "mismatch":
        if total is None:
            inclusion = "unverified"
        elif reported_input is not None and output is not None and total == reported_input + output:
            inclusion = "verified"
        else:
            issues.append("inclusion_mismatch")
            inclusion = "mismatch"
    return _picture(convention="codex_app_server", scope="thread_cumulative", attributable=False,
                    input_uncached=input_uncached, cache_read=cache_read, cache_write=None,
                    output=output, reasoning=reasoning, inclusion=inclusion, issues=issues,
                    reasoning_subset=True)


def _antigravity(native: dict) -> dict:
    issues: list[str] = []
    raw = _mapping(native.get("usage"))
    reported_input, _ = _component(raw, "input_tokens", issues)
    output, _ = _component(raw, "output_tokens", issues)
    thinking, _ = _component(raw, "thinking_tokens", issues)
    cache_read, _ = _component(raw, "cache_read_tokens", issues)
    total, _ = _component(raw, "total_tokens", issues)
    inclusion = "unverified"
    if total is None:
        inclusion = "unverified"
    elif reported_input is not None and output is not None and total == reported_input + output:
        # input 不含缓存读；thinking 不并入 output。
        inclusion = "verified"
    elif (reported_input is not None and output is not None and thinking is not None
          and total == reported_input + output + thinking):
        output = output + thinking
        inclusion = "verified"
    else:
        issues.append("inclusion_mismatch")
        inclusion = "mismatch"
    return _picture(convention="antigravity", scope="run", attributable=True,
                    input_uncached=reported_input, cache_read=cache_read, cache_write=None,
                    output=output, reasoning=thinking, inclusion=inclusion, issues=issues,
                    reasoning_subset=False)


def normalize_breakdown(native: dict, *, backend_kind_hint: str | None = None) -> dict:
    """把一条原生用量归一成 asterun.usage_breakdown/v1。

    识别顺序与 usage.report 的约定判断一致：先看 Codex ``token_usage``，
    再看带会话范围的 Antigravity 回执，最后才是非累计的 Grok headless 报告。
    ``backend_kind_hint`` 不是计数约定，不能单独选口径。
    """
    del backend_kind_hint
    if not isinstance(native, dict):
        return _unknown()
    if "token_usage" in native:
        return _codex(native)
    if _DISPATCH_KEY in native and native.get("usage_scope") == "session":
        return _antigravity(native)
    if (native.get("usage_source") == "native_headless_report"
            and native.get("usage_scope") not in _CUMULATIVE_SCOPES):
        return _grok(native)
    return _unknown()


def _usage_event(event: Any) -> bool:
    if not isinstance(event, dict):
        return False
    kind, event_type = event.get("kind"), event.get("type")
    if kind == "usage":
        return event_type in (None, "message")
    return kind is None and event_type in (None, "message") and "usage" in event


def _residency(usage: dict) -> tuple[int, bool]:
    total, partial = 0, False
    for key in _RESIDENCY_FIELDS:
        value = usage.get(key) if key in usage else None
        if type(value) is int and value >= 0:
            total += value
        else:
            partial = True
    return total, partial


def grok_residency_curve(events: Iterable[dict]) -> dict:
    """按 Grok usage 事件画上下文驻留曲线。同 message_id 保留首次位置上的最后一次。"""
    points: list[dict] = []
    partials: list[bool] = []
    index: dict[str, int] = {}
    for event in events:
        if not _usage_event(event):
            continue
        usage = event.get("usage")
        residency, partial = _residency(usage if isinstance(usage, dict) else {})
        message_id = event.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            message_id = None
        point = {"message_id": message_id, "residency": residency}
        slot = index.get(message_id) if message_id is not None else None
        if slot is None:
            if message_id is not None:
                index[message_id] = len(points)
            points.append(point)
            partials.append(partial)
        else:
            points[slot] = point
            partials[slot] = partial
    count = len(points)
    if count == 0:
        return {"points": [], "peak": None, "count": 0, "partial": False}
    return {"points": points[-_RESIDENCY_LIMIT:], "peak": max(point["residency"] for point in points),
            "count": count, "partial": any(partials)}
