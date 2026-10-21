"""四分量用量归一化：只观察，不改变既有 token/费用口径。"""
from copy import deepcopy

import pytest

from asterun.contracts import Run, RunStatus
from asterun.ids import BackendId, RunId, TaskId
from asterun.usage.breakdown import grok_residency_curve, normalize_breakdown
from asterun.usage.report import build_usage_report, paginate_usage_report


_FIELDS = ("input_uncached", "cache_read", "cache_write", "output", "reasoning", "processed", "new")
_NOTES_PREFIX = [
    "known_sum 仅汇总可归属到所选运行的已知计数，未知不按零处理。",
    "仅已识别 Grok/Codex schema 按缓存/推理包含关系归一化；其它来源保持未知。",
    "modelUsage 是原生分解，不与运行总量重复相加。",
    "会话累计用量保留为观察值；没有运行起点基线时不计入运行总量。",
    "USD 与 native_usd_ticks 分开保留，不相加、不换算，不代表实际扣款。",
]


def run(id, native, backend):
    return Run(RunId(id), TaskId("task-1"), BackendId(backend), RunStatus.SUCCEEDED,
               "2026-09-15T00:00:00Z", native=native)


def grok(**usage):
    base = {"input_tokens": 10, "cache_read_input_tokens": 80, "cache_creation_input_tokens": 5,
            "output_tokens": 20, "reasoning_tokens": 12, "total_tokens": 115}
    base.update(usage)
    return {"usage_source": "native_headless_report", "usage": base}


def without(native, *keys):
    usage = dict(native["usage"])
    for key in keys:
        usage.pop(key, None)
    return {**native, "usage": usage}


def codex(**total):
    block = {"inputTokens": 100, "cachedInputTokens": 90, "outputTokens": 20,
             "reasoningOutputTokens": 8, "totalTokens": 120}
    block.update(total)
    return {"model": "gpt-6-astra", "token_usage": {"threadId": "thread-1", "turnId": "turn-1",
            "tokenUsage": {"total": block, "last": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}}}}


def agy(**usage):
    base = {"input_tokens": 7, "output_tokens": 3, "thinking_tokens": 1,
            "cache_read_tokens": 0, "total_tokens": 10}
    base.update(usage)
    return {"asterun.antigravity_dispatch_v1": {"run_id": "agy"}, "usage_scope": "session", "usage": base}


def test_grok_verified_uncached_total_matches_four_parts():
    native = grok()
    original = deepcopy(native)
    result = normalize_breakdown(native, backend_kind_hint="codex")
    assert native == original
    assert result["schema"] == "asterun.usage_breakdown/v1"
    assert result["convention"] == "grok_headless"
    assert result["scope"] == "run" and result["attributable"] is True
    assert result["input_uncached"] == 10
    assert result["cache_read"] == 80 and result["cache_write"] == 5
    assert result["output"] == 20 and result["reasoning"] == 12
    assert result["processed"] == 115 and result["new"] == 35
    assert result["inclusion_check"] == "verified" and result["completeness"] == "complete"
    assert result["issues"] == []
    assert normalize_breakdown(native) == result


def test_grok_total_equal_to_input_plus_output_reclassifies_inclusive_input():
    result = normalize_breakdown(grok(input_tokens=100, cache_read_input_tokens=70,
                                      cache_creation_input_tokens=10, output_tokens=20,
                                      total_tokens=120))
    assert result["input_uncached"] == 20
    assert result["cache_read"] == 70 and result["cache_write"] == 10
    assert result["output"] == 20
    assert result["processed"] == 120 and result["new"] == 50
    assert result["inclusion_check"] == "verified" and result["completeness"] == "complete"


def test_grok_negative_split_and_other_mismatch_stay_unknown():
    negative = normalize_breakdown(grok(input_tokens=30, cache_read_input_tokens=40,
                                        cache_creation_input_tokens=10, output_tokens=5,
                                        total_tokens=35))
    assert negative["inclusion_check"] == "mismatch" and negative["completeness"] == "unknown"
    assert negative["attributable"] is True
    assert all(negative[field] is None for field in _FIELDS)
    assert "input_uncached_negative" in negative["issues"]

    other = normalize_breakdown(grok(total_tokens=50))
    assert other["inclusion_check"] == "mismatch" and other["completeness"] == "unknown"
    assert other["processed"] is None and other["input_uncached"] is None
    assert "inclusion_mismatch" in other["issues"]


def test_grok_missing_cache_write_is_partial_and_absent_total_is_unverified():
    partial = normalize_breakdown(without(grok(total_tokens=110), "cache_creation_input_tokens"))
    assert partial["cache_write"] is None
    assert partial["input_uncached"] == 10 and partial["cache_read"] == 80 and partial["output"] == 20
    assert partial["processed"] == 110 and partial["new"] == 30
    assert partial["inclusion_check"] == "verified" and partial["completeness"] == "partial"
    assert partial["issues"] == []

    unverified = normalize_breakdown(without(grok(), "total_tokens", "cache_creation_input_tokens"))
    assert unverified["inclusion_check"] == "unverified" and unverified["completeness"] == "partial"
    assert unverified["processed"] == 110 and unverified["cache_write"] is None

    no_total = normalize_breakdown(without(grok(), "total_tokens"))
    assert no_total["inclusion_check"] == "unverified" and no_total["completeness"] == "complete"
    assert no_total["processed"] == 115 and no_total["new"] == 35


@pytest.mark.parametrize("bad", [True, False, -1, 1.5, "100"])
def test_non_integer_counters_are_missing_and_bool_is_not_one(bad):
    native = without(grok(), "total_tokens")
    native["usage"]["cache_read_input_tokens"] = bad
    result = normalize_breakdown(native)
    assert result["cache_read"] is None
    assert result["processed"] is None and result["new"] is None
    assert result["completeness"] == "unknown"
    assert "invalid_counter:cache_read_input_tokens" in result["issues"]
    assert result["input_uncached"] == 10 and result["output"] == 20

    # True must not be counted as 1, or this total would falsely verify.
    forged = grok(cache_read_input_tokens=True, total_tokens=36)
    forged_result = normalize_breakdown(forged)
    assert forged_result["inclusion_check"] == "mismatch"
    assert forged_result["cache_read"] is None and forged_result["processed"] is None


def test_codex_thread_total_is_not_attributable_and_cache_write_is_partial():
    native = codex()
    result = normalize_breakdown(native)
    assert result["convention"] == "codex_app_server"
    assert result["scope"] == "thread_cumulative" and result["attributable"] is False
    assert result["input_uncached"] == 10 and result["cache_read"] == 90
    assert result["cache_write"] is None and result["output"] == 20 and result["reasoning"] == 8
    assert result["processed"] == 120 and result["new"] == 30
    assert result["inclusion_check"] == "verified" and result["completeness"] == "partial"
    # last is not the run increment.
    assert result["input_uncached"] != 1

    written = normalize_breakdown(codex(cacheWriteInputTokens=7))
    assert written["cache_write"] == 7 and written["completeness"] == "complete"
    assert written["processed"] == 127 and written["new"] == 37
    assert written["attributable"] is False

    over = normalize_breakdown(codex(cachedInputTokens=140))
    assert over["inclusion_check"] == "mismatch" and over["completeness"] == "unknown"
    assert over["input_uncached"] is None and over["processed"] is None
    assert "cached_input_exceeds_input" in over["issues"]

    empty = normalize_breakdown({"token_usage": {}})
    assert empty["convention"] == "codex_app_server" and empty["attributable"] is False
    assert empty["completeness"] == "unknown" and empty["processed"] is None
    assert "unrecognized_codex_usage_shape" in empty["issues"]


def test_antigravity_session_is_one_run_and_thinking_has_two_shapes():
    separate = normalize_breakdown(agy())
    assert separate["convention"] == "antigravity"
    assert separate["scope"] == "run" and separate["attributable"] is True
    assert separate["input_uncached"] == 7 and separate["cache_read"] == 0
    assert separate["cache_write"] is None and separate["output"] == 3 and separate["reasoning"] == 1
    assert separate["processed"] == 10 and separate["new"] == 10
    assert separate["inclusion_check"] == "verified" and separate["completeness"] == "partial"

    folded = normalize_breakdown(agy(total_tokens=11))
    assert folded["output"] == 4 and folded["reasoning"] == 1
    assert folded["processed"] == 11 and folded["new"] == 11
    assert folded["inclusion_check"] == "verified" and folded["completeness"] == "partial"
    assert folded["cache_write"] is None

    mismatched = normalize_breakdown(agy(total_tokens=99))
    assert mismatched["inclusion_check"] == "mismatch" and mismatched["completeness"] == "unknown"
    assert mismatched["processed"] is None and mismatched["output"] is None
    assert mismatched["attributable"] is True

    missing_read = agy()
    missing_read["usage"]["cache_read_tokens"] = -3
    missing = normalize_breakdown(missing_read)
    assert missing["inclusion_check"] == "verified" and missing["cache_read"] is None
    assert missing["processed"] is None and missing["new"] is None
    assert missing["completeness"] == "unknown"
    assert "invalid_counter:cache_read_tokens" in missing["issues"]


def test_unknown_convention_is_all_null_and_markers_follow_report_order():
    unknown = normalize_breakdown({"usage": {"total_tokens": 22}})
    assert unknown["convention"] == "unknown" and unknown["scope"] == "unknown"
    assert unknown["attributable"] is False and unknown["completeness"] == "unknown"
    assert unknown["inclusion_check"] == "unverified"
    assert all(unknown[field] is None for field in _FIELDS)
    assert unknown["issues"] == ["usage_convention_unknown"]

    session_only = {"usage_scope": "session", "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
    assert normalize_breakdown(session_only)["convention"] == "unknown"
    assert normalize_breakdown({"asterun.antigravity_dispatch_v1": {}, "usage_scope": "run"})["convention"] == "unknown"

    cumulative = grok(usage_scope_placeholder=0)
    cumulative["usage_scope"] = "thread_cumulative"
    assert normalize_breakdown(cumulative)["convention"] == "unknown"
    assert normalize_breakdown(cumulative)["attributable"] is False

    both = grok()
    both["token_usage"] = codex()["token_usage"]
    assert normalize_breakdown(both)["convention"] == "codex_app_server"
    assert normalize_breakdown(both)["attributable"] is False

    assert normalize_breakdown(None)["convention"] == "unknown"


def test_residency_dedupes_message_id_keeps_anonymous_points_and_caps_the_tail():
    full = lambda value: {"input_tokens": value, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    events = [
        {"type": "message", "kind": "text", "text": "ignore", "usage": full(99)},
        {"type": "message", "kind": "usage", "message_id": "a", "usage": full(1)},
        {"type": "message", "kind": "usage", "usage": full(2)},
        {"type": "message", "kind": "usage", "message_id": "a", "usage": full(3)},
        {"type": "message", "kind": "usage", "usage": {"input_tokens": 4}},
        {"type": "message", "kind": "usage", "message_id": "b", "usage": full(5)},
    ]
    curve = grok_residency_curve(events)
    assert curve["points"] == [
        {"message_id": "a", "residency": 3},
        {"message_id": None, "residency": 2},
        {"message_id": None, "residency": 4},
        {"message_id": "b", "residency": 5},
    ]
    assert curve["peak"] == 5 and curve["count"] == 4 and curve["partial"] is True

    bool_point = grok_residency_curve([
        {"type": "message", "kind": "usage", "message_id": "c",
         "usage": {"input_tokens": True, "cache_read_input_tokens": 2, "cache_creation_input_tokens": -1}}])
    assert bool_point["points"] == [{"message_id": "c", "residency": 2}]
    assert bool_point["partial"] is True and bool_point["peak"] == 2

    many = []
    for index in range(600):
        usage = full(index)
        if index == 0:
            usage = {"input_tokens": 0}
        many.append({"type": "message", "kind": "usage", "message_id": f"m{index}", "usage": usage})
    capped = grok_residency_curve(many)
    assert capped["count"] == 600 and len(capped["points"]) == 500
    assert capped["points"][0] == {"message_id": "m100", "residency": 100}
    assert capped["points"][-1] == {"message_id": "m599", "residency": 599}
    assert capped["peak"] == 599 and capped["partial"] is True

    empty = grok_residency_curve([])
    assert empty == {"points": [], "peak": None, "count": 0, "partial": False}


def test_report_breakdown_sums_only_attributable_rows_and_keeps_old_totals():
    good = grok()
    bad = grok(total_tokens=50)
    report = build_usage_report([
        run("good", good, "grok-code"),
        run("bad", bad, "grok-code"),
        run("codex", codex(), "codex"),
        run("agy-separate", agy(), "antigravity"),
        run("agy-folded", agy(total_tokens=11), "antigravity"),
        run("unknown", {"usage": {"total_tokens": 22}}, "other"),
    ])
    assert report["schema_version"] == 1
    assert report["billed_usage_verified"] is False
    assert report["notes"][:5] == _NOTES_PREFIX
    assert "attributable" in report["notes"][5] and "processed" in report["notes"][5]
    rows = {row["run_id"]: row for row in report["runs"]}

    assert rows["good"]["tokens"]["input_tokens"] == 95
    assert rows["good"]["tokens"]["total_tokens"] == 115
    assert rows["good"]["breakdown"]["input_uncached"] == 10
    assert rows["good"]["breakdown"]["processed"] == 115
    assert rows["good"]["convention"] == "grok_headless"
    assert rows["good"]["aggregation_eligible"] is True

    assert rows["bad"]["tokens"]["input_tokens"] == 95
    assert rows["bad"]["tokens"]["total_tokens"] == 50
    assert "reported_total_differs_from_components" in rows["bad"]["issues"]
    assert rows["bad"]["breakdown"]["inclusion_check"] == "mismatch"
    assert rows["bad"]["breakdown"]["processed"] is None

    assert rows["codex"]["scope"] == "thread_cumulative"
    assert rows["codex"]["aggregation_eligible"] is False
    assert rows["codex"]["observed_tokens"]["total_tokens"] == 120
    assert rows["codex"]["tokens"]["total_tokens"] is None
    assert rows["codex"]["breakdown"]["processed"] == 120
    assert rows["codex"]["breakdown"]["attributable"] is False

    assert rows["agy-separate"]["scope"] == "session"
    assert rows["agy-separate"]["convention"] == "unknown"
    assert rows["agy-separate"]["aggregation_eligible"] is False
    assert rows["agy-separate"]["tokens"]["total_tokens"] is None
    assert rows["agy-separate"]["breakdown"]["scope"] == "run"
    assert rows["agy-separate"]["breakdown"]["convention"] == "antigravity"
    assert rows["agy-separate"]["breakdown"]["output"] == 3
    assert rows["agy-folded"]["breakdown"]["output"] == 4

    assert report["totals"]["tokens"]["total_tokens"]["known_sum"] == 165
    assert report["totals"]["tokens"]["total_tokens"]["reported_runs"] == 2
    breakdown = report["totals"]["breakdown"]
    assert set(breakdown) == {"input_uncached", "cache_read", "cache_write", "output", "processed", "new",
                              "completeness"}
    assert breakdown["input_uncached"] == {"known_sum": 24, "reported_runs": 3, "unknown_runs": 1}
    assert breakdown["cache_read"] == {"known_sum": 80, "reported_runs": 3, "unknown_runs": 1}
    assert breakdown["cache_write"] == {"known_sum": 5, "reported_runs": 1, "unknown_runs": 3}
    assert breakdown["output"] == {"known_sum": 27, "reported_runs": 3, "unknown_runs": 1}
    assert breakdown["processed"] == {"known_sum": 136, "reported_runs": 3, "unknown_runs": 1}
    assert breakdown["new"] == {"known_sum": 56, "reported_runs": 3, "unknown_runs": 1}
    assert breakdown["completeness"] == {"complete": 1, "partial": 2, "unknown": 1}

    grok_group = next(group for group in report["groups"] if group["dimensions"]["backend"] == "grok-code")
    assert grok_group["breakdown"]["processed"]["known_sum"] == 115
    assert grok_group["breakdown"]["completeness"] == {"complete": 1, "partial": 0, "unknown": 1}
    codex_group = next(group for group in report["groups"] if group["dimensions"]["backend"] == "codex")
    assert codex_group["breakdown"]["processed"]["known_sum"] is None
    assert codex_group["breakdown"]["completeness"] == {"complete": 0, "partial": 0, "unknown": 0}


def test_pagination_drops_breakdown_only_after_native_usage_and_keeps_small_rows():
    report = build_usage_report([run("good", grok(), "grok-code")])
    page = paginate_usage_report(report)
    assert "breakdown" in page["runs"][0] and "detail_omitted" not in page["runs"][0]
    assert page["totals"]["breakdown"]["processed"]["known_sum"] == 115

    def oversized(native_blob, breakdown_blob):
        return {"schema_version": 1, "totals": {"run_count": 1},
                "runs": [{"run_id": "r", "task_id": "t", "dimensions": {"backend": "grok-code"},
                          "native_usage": {"blob": native_blob}, "breakdown": breakdown_blob}]}

    native_only = paginate_usage_report(oversized("大" * 20000, {"ok": True}))
    reduced = native_only["runs"][0]
    assert reduced["detail_omitted"]["fields"] == ["native_usage"]
    assert reduced["breakdown"] == {"ok": True} and "native_usage" not in reduced

    both = paginate_usage_report(oversized("大" * 20000, {"blob": "大" * 20000}))
    assert both["runs"][0]["detail_omitted"]["fields"] == ["native_usage", "breakdown"]
    assert "breakdown" not in both["runs"][0] and "native_usage" not in both["runs"][0]
    assert len(__import__("json").dumps(both, ensure_ascii=False).encode()) < 150 * 1024
