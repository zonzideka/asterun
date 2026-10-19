"""候选组选路评分。纯函数，不读配置、不写库、不派发。

主分数是到期浪费量。付费超额和重置券只可展示，不进入 S。数据不够时由 select 退回 primary。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

PREFERENCE_DEFAULTS = {
    "enabled": False,
    "horizon_hours": 72,
    "waste_threshold_bp": 1000,
    "switch_margin_bp": 500,
    "require_confidence": "medium",
    "consider_short_windows": False,
    "floor_short_bp": 500,
    "floor_long_bp": 1000,
    "count_reset_grants": False,
}
_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}
_DEFAULT_DURATION_S = {"5h": 5 * 3600, "weekly": 7 * 24 * 3600, "monthly": 30 * 24 * 3600}
_FAR_RESET = "9999-12-31T23:59:59Z"


def resolve_prefs(raw: dict | None) -> dict[str, Any]:
    """缺省补齐。重置券永远不计入，调用方传入 true 也按 false 计算。"""
    prefs = dict(PREFERENCE_DEFAULTS)
    if isinstance(raw, dict):
        for key in PREFERENCE_DEFAULTS:
            if key in raw:
                prefs[key] = raw[key]
    prefs["count_reset_grants"] = False
    return prefs


def score_candidate(windows, *, now, prefs, inflight_runs, est_bp_per_run, weight=1) -> dict[str, Any]:
    """对一个候选的 inspect windows 打分。

    返回 tier、S、S_A、U、excluded、reason_codes、windows_used。
    tier 0 即将浪费，1 新鲜但未达阈值，2 未知，3 接近耗尽或已被剔除。
    """
    prefs = resolve_prefs(prefs)
    moment = _parse_time(now)
    rows = []
    paid_ignored = False
    latched = False
    exhausted = False
    for item in windows or []:
        if not isinstance(item, dict):
            continue
        observation = item.get("observation") if isinstance(item.get("observation"), dict) else {}
        freshness = item.get("freshness")
        if observation.get("paid_overage") is True:
            paid_ignored = True
            continue
        if _is_latch(observation, freshness, moment):
            latched = True
            continue
        used = observation.get("used_bp")
        if freshness == "exhausted" or (
            _is_fresh(freshness) and (bool(observation.get("exhausted_hint")) or (type(used) is int and used >= 10000))
        ):
            exhausted = True
            continue
        if not _is_fresh(freshness) or type(used) is not int:
            continue
        duration = _duration_s(observation)
        resets_at = observation.get("resets_at")
        reset_at = _parse_time(resets_at)
        t_seconds = None if moment is None or reset_at is None else int((reset_at - moment).total_seconds())
        est = _estimate(est_bp_per_run, observation.get("window_kind"))
        remaining = 10000 - used - int(inflight_runs) * est
        rows.append({
            "bucket": observation.get("bucket_id") or "",
            "kind": observation.get("window_kind") or "",
            "used": used,
            "remaining": remaining,
            "duration": duration,
            "t_seconds": t_seconds,
            "resets_at": resets_at if isinstance(resets_at, str) else None,
            "confidence": observation.get("confidence"),
            "est": est,
            "short": _is_short(observation, duration),
            "history": item.get("history"),
            "observation": observation,
        })

    codes: list[str] = []
    if paid_ignored:
        codes.append("paid_overage_ignored")
    windows_used = [f"{row['bucket']}/{row['kind']}" for row in rows]
    if latched:
        codes.append("latched")
    if exhausted:
        codes.append("exhausted")
    if not rows and not latched and not exhausted:
        codes.append("no_windows" if not windows else "unknown")
        return _result(2, 0, 0, None, False, codes, windows_used, None)

    u_value = min(row["remaining"] for row in rows) if rows else None
    below_floor = any(row["remaining"] < _floor_bp(row, prefs) + row["est"] for row in rows)
    ranked = _value_rows(rows, prefs)
    best = _best_waste(ranked)
    qualified = [row for row in ranked if _confidence_ok(row.get("confidence"), prefs["require_confidence"])]
    best_qualified = _best_waste(qualified)
    waste = 0 if best is None else best["waste"]
    score = int(weight) * waste
    score_a = 0 if best is None else best["s_a"]
    resets = None if best is None else best.get("resets_at")
    theta = prefs["waste_threshold_bp"]
    qualified_score = 0 if best_qualified is None else int(weight) * best_qualified["waste"]
    confidence_blocked = best is not None and qualified_score < theta <= score

    if latched or exhausted:
        if below_floor and "below_floor" not in codes:
            codes.append("below_floor")
        return _result(3, score, score_a, u_value, True, codes, windows_used, resets)
    if not rows:
        codes.append("unknown")
        return _result(2, 0, 0, None, False, codes, windows_used, None)
    if below_floor:
        codes.append("below_floor")
        return _result(3, score, score_a, u_value, False, codes, windows_used, resets)
    if qualified_score >= theta and best_qualified is not None:
        return _result(0, score, score_a, u_value, False, codes, windows_used, resets)
    if confidence_blocked:
        codes.append("confidence_below_required")
    return _result(1, score, score_a, u_value, False, codes, windows_used, resets)


def select(group, candidates: dict[str, dict], *, prefs, sticky=None) -> dict[str, Any]:
    """保守选择。未知退回 primary；只有新鲜证据才离开 primary。"""
    prefs = resolve_prefs(prefs)
    members = list(group["members"])
    primary = group["primary"]
    delta = int(prefs["switch_margin_bp"])
    scores = {
        alias: candidates.get(alias) or {
            "tier": 2, "S": 0, "S_A": 0, "excluded": False, "reason_codes": ["unknown"],
        }
        for alias in members
    }

    def finish(chosen: str, mode: str, reason: str) -> dict[str, Any]:
        return {
            "chosen": chosen,
            "primary": primary,
            "mode": mode,
            "reason_code": reason,
            "candidates": [_public_candidate(alias, scores[alias]) for alias in members],
        }

    if len(members) == 1:
        return finish(primary, "primary", "single_candidate")
    if members and all(_latched_score(scores[alias]) for alias in members):
        return finish(primary, "fallback_primary", "all_latched")
    if all(scores[alias].get("tier") == 2 and not scores[alias].get("excluded") for alias in members):
        return finish(primary, "fallback_primary", "all_unknown")

    chosen = primary
    mode = "primary"
    reason = "stay_primary"
    if _removed(scores[primary]):
        pool = [alias for alias in members if scores[alias].get("tier") in (0, 1) and not scores[alias].get("excluded")]
        if pool:
            chosen = _best(pool, scores, members, delta)
            mode = "switch"
            reason = "primary_excluded"
        else:
            mode = "fallback_primary"
            reason = "primary_excluded"
    elif scores[primary].get("tier") == 2:
        mode = "fallback_primary"
        reason = "primary_unknown"
    else:
        pool = [
            alias for alias in members
            if alias != primary and scores[alias].get("tier") == 0 and not scores[alias].get("excluded")
            and _beats(scores[alias], scores[primary], delta)
        ]
        if pool:
            chosen = _best(pool, scores, members, delta)
            mode = "switch"
            reason = "waste_margin"

    if sticky in members and sticky != chosen and reason not in {"all_unknown", "primary_unknown", "all_latched"}:
        held = scores[sticky]
        if held.get("tier") in (0, 1) and not _removed(held) and not _beats(scores[chosen], held, delta):
            chosen = sticky
            mode = "primary" if sticky == primary else "switch"
            reason = "sticky"
    return finish(chosen, mode, reason)


def _result(tier, score, score_a, u_value, excluded, codes, windows_used, resets_at) -> dict[str, Any]:
    return {
        "tier": tier,
        "S": score,
        "S_A": score_a,
        "U": u_value,
        "excluded": excluded,
        "reason_codes": codes,
        "windows_used": windows_used,
        "resets_at": resets_at,
    }


def _public_candidate(alias: str, score: dict) -> dict[str, Any]:
    return {
        "alias": alias,
        "tier": score.get("tier"),
        "S": score.get("S"),
        "S_A": score.get("S_A"),
        "excluded": bool(score.get("excluded")),
        "reason_codes": list(score.get("reason_codes") or []),
    }


def _value_rows(rows: list[dict], prefs: dict) -> list[dict]:
    """同一 window_kind 取剩余最少的桶，避免宽松桶把 S 抬高。"""
    horizon = int(prefs["horizon_hours"]) * 3600
    chosen: dict[str, dict] = {}
    for row in rows:
        if row["short"] and not prefs["consider_short_windows"]:
            continue
        if row["duration"] is None or row["t_seconds"] is None:
            continue
        if row["t_seconds"] <= 0 or row["t_seconds"] > horizon:
            continue
        if row["t_seconds"] > row["duration"]:
            continue
        scored = dict(row)
        scored["waste"] = _waste(row)
        scored["s_a"] = row["remaining"] * 3600 // row["t_seconds"]
        current = chosen.get(row["kind"])
        if current is None or row["remaining"] < current["remaining"]:
            chosen[row["kind"]] = scored
    return list(chosen.values())


def _best_waste(rows: list[dict]) -> dict | None:
    best = None
    for row in rows:
        if best is None or row["waste"] > best["waste"] or (
            row["waste"] == best["waste"] and (row.get("resets_at") or _FAR_RESET) < (best.get("resets_at") or _FAR_RESET)
        ):
            best = row
    return best


def _waste(row: dict) -> int:
    remaining = row["remaining"]
    duration = row["duration"]
    elapsed = duration - row["t_seconds"]
    minimum_elapsed = max(3600, duration // 10)
    ewma = _ewma_per_second(row["observation"], row.get("history"))
    if ewma is not None:
        numerator = remaining * ewma[1] - ewma[0] * row["t_seconds"]
        denominator = ewma[1]
    elif elapsed >= minimum_elapsed and elapsed > 0:
        numerator = remaining * elapsed - row["used"] * row["t_seconds"]
        denominator = elapsed
    else:
        numerator = remaining * duration - 10000 * row["t_seconds"]
        denominator = duration
    if numerator <= 0 or denominator <= 0:
        return 0
    return numerator // denominator


def _ewma_per_second(observation: dict, history):
    """同一 epoch、间隔不少于 30 分钟的用量差分。返回 (分子, 分母) 表示 bp/秒，没有历史则 None。"""
    epoch = observation.get("window_epoch")
    points = []
    for item in list(history or []) + [observation]:
        if not isinstance(item, dict):
            continue
        if epoch and item.get("window_epoch") not in (None, epoch):
            continue
        used = item.get("used_bp")
        stamp = _parse_time(item.get("observed_at"))
        if type(used) is not int or stamp is None:
            continue
        points.append((stamp, used))
    points.sort(key=lambda item: item[0])
    rates = []
    previous = None
    for stamp, used in points:
        if previous is not None:
            gap = int((stamp - previous[0]).total_seconds())
            delta = used - previous[1]
            if gap >= 1800 and delta >= 0:
                rates.append((delta, gap))
        previous = (stamp, used)
    if not rates:
        return None
    numerator, denominator = rates[0]
    for delta, gap in rates[1:]:
        numerator = delta * denominator + numerator * gap
        denominator = 2 * gap * denominator
    return numerator, denominator


def _floor_bp(row: dict, prefs: dict) -> int:
    if row["short"]:
        return int(prefs["floor_short_bp"])
    return int(prefs["floor_long_bp"])


def _is_short(observation: dict, duration: int | None) -> bool:
    if observation.get("window_kind") == "5h":
        return True
    return duration is not None and duration < 24 * 3600


def _duration_s(observation: dict) -> int | None:
    raw = observation.get("window_duration_s")
    if type(raw) is int and raw > 0:
        return raw
    return _DEFAULT_DURATION_S.get(observation.get("window_kind"))


def _is_fresh(freshness: str | None) -> bool:
    return freshness == "fresh"


def _is_latch(observation: dict, freshness: str | None, now) -> bool:
    """只有 freshness 仍为 latched、且未到 resets_at 才算闩锁。

    窗口翻转后 freshness 是 unknown。旧的 latch 种类或来源不能再把候选排除。
    """
    if freshness != "latched":
        return False
    reset_at = _parse_time(observation.get("resets_at"))
    moment = now if isinstance(now, datetime) else _parse_time(now)
    if reset_at is not None and moment is not None and moment >= reset_at:
        return False
    return True


def _confidence_ok(confidence, require: str) -> bool:
    return _CONFIDENCE_RANK.get(confidence, -1) >= _CONFIDENCE_RANK.get(require, 1)


def _estimate(est_bp_per_run, kind: str | None) -> int:
    if isinstance(est_bp_per_run, dict):
        if kind in est_bp_per_run:
            return int(est_bp_per_run[kind])
        return int(est_bp_per_run.get("default", 100))
    return int(est_bp_per_run)


def _removed(score: dict) -> bool:
    return bool(score.get("excluded")) or score.get("tier") == 3


def _latched_score(score: dict) -> bool:
    return "latched" in (score.get("reason_codes") or [])


def _rounded(value) -> int:
    number = int(value or 0)
    if number >= 0:
        return ((number + 50) // 100) * 100
    return -(((-number) + 50) // 100) * 100


def _beats(challenger: dict, incumbent: dict, delta: int) -> bool:
    return _rounded(challenger.get("S")) >= _rounded(incumbent.get("S")) + delta


def _better(left: str, right: str, scores: dict, members: list[str], delta: int) -> bool:
    left_score = _rounded(scores[left].get("S"))
    right_score = _rounded(scores[right].get("S"))
    if abs(left_score - right_score) >= delta and left_score != right_score:
        return left_score > right_score
    left_reset = scores[left].get("resets_at") or _FAR_RESET
    right_reset = scores[right].get("resets_at") or _FAR_RESET
    if left_reset != right_reset:
        return left_reset < right_reset
    if (scores[left].get("S_A") or 0) != (scores[right].get("S_A") or 0):
        return (scores[left].get("S_A") or 0) > (scores[right].get("S_A") or 0)
    left_start = scores[left].get("can_start")
    right_start = scores[right].get("can_start")
    if left_start is not None and right_start is not None and bool(left_start) != bool(right_start):
        return bool(left_start)
    return members.index(left) < members.index(right)


def _best(aliases: list[str], scores: dict, members: list[str], delta: int) -> str:
    winner = aliases[0]
    for alias in aliases[1:]:
        if _better(alias, winner, scores, members, delta):
            winner = alias
    return winner


def _parse_time(value):
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None
