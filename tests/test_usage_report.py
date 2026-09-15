from copy import deepcopy
from dataclasses import replace
import json

import pytest

from asterun.contracts import AcceptanceStatus, DeliveryStatus, Run, RunStatus, Task
from asterun.ids import AccountRef, BackendId, BillingPoolRef, ModelId, RunId, TaskId
from asterun.usage.report import build_usage_report, paginate_usage_report


def run(id="run-1", *, native=None, status=RunStatus.SUCCEEDED, backend="grok-code", role="implementation"):
    return Run(RunId(id), TaskId("task-1"), BackendId(backend), status, "2026-09-15T00:00:00Z",
               native=native or {}, role=role)


def grok(**changes):
    value = {"usage_source": "native_headless_report", "usage": {
        "input_tokens": 10, "cache_read_input_tokens": 80, "cache_creation_input_tokens": 5,
        "output_tokens": 20, "reasoning_tokens": 12},
        "modelUsage": {"grok-observed": {"inputTokens": 10, "cacheReadInputTokens": 80,
                                       "cacheCreationInputTokens": 5, "outputTokens": 20,
                                       "reasoningTokens": 12}}, "total_cost_usd": 0.1}
    value.update(changes)
    return value


def test_grok_inclusive_input_reasoning_subset_and_model_breakdown_are_not_double_counted():
    native = grok()
    original = deepcopy(native)
    report = build_usage_report([run(native=native), run("repair", native=grok(), role="repair")])
    totals = report["totals"]["tokens"]
    assert totals["input_tokens"]["known_sum"] == 190
    assert totals["non_cached_input_tokens"]["known_sum"] == 30
    assert totals["cached_input_tokens"]["known_sum"] == 160
    assert totals["output_tokens"]["known_sum"] == 40
    assert totals["reasoning_output_tokens"]["known_sum"] == 24
    assert totals["total_tokens"]["known_sum"] == 230
    assert report["totals"]["costs"][0]["known_sum"] == "0.2"
    assert report["runs"][0]["dimensions"]["model_id"] == "grok-observed"
    assert report["runs"][0]["native_usage"]["modelUsage"] == native["modelUsage"]
    report["runs"][0]["native_usage"]["modelUsage"].clear()
    assert native == original


def test_missing_cache_and_usage_are_unknown_not_zero():
    partial = grok(usage={"input_tokens": 10, "output_tokens": 4})
    report = build_usage_report([run(native=partial), run("unknown")])
    totals = report["totals"]["tokens"]
    assert totals["total_tokens"] == {"known_sum": None, "reported_runs": 0, "unknown_runs": 2, "partial": True}
    assert totals["output_tokens"] == {"known_sum": 4, "reported_runs": 1, "unknown_runs": 1, "partial": True}
    assert report["totals"]["costs"][0]["partial"] is True


def test_reported_total_derives_input_without_inventing_missing_cache_write():
    native = grok(usage={"input_tokens": 10, "cache_read_input_tokens": 80,
                         "output_tokens": 20, "total_tokens": 110})
    row = build_usage_report([run(native=native)])["runs"][0]
    assert row["tokens"]["input_tokens"] == 90
    assert row["tokens"]["non_cached_input_tokens"] == 10
    assert row["tokens"]["cache_write_input_tokens"] is None
    assert row["tokens"]["total_tokens"] == 110
    assert row["token_sources"]["input_tokens"] == "difference(usage.total_tokens,usage.output_tokens)"


def test_inconsistent_reported_total_keeps_native_evidence_and_reports_issue():
    native = grok(usage={"input_tokens": 10, "cache_read_input_tokens": 80,
                         "output_tokens": 20, "total_tokens": 50})
    row = build_usage_report([run(native=native)])["runs"][0]
    assert row["tokens"]["input_tokens"] is None
    assert row["tokens"]["total_tokens"] == 50
    assert "reported_total_less_than_known_components" in row["issues"]
    assert row["usage_is_partial"] is True


def test_duplicate_runs_are_deduplicated_but_conflicting_snapshots_fail():
    item = run(native=grok())
    report = build_usage_report([item, deepcopy(item)])
    assert report["duplicate_runs_ignored"] == 1
    assert report["totals"]["tokens"]["total_tokens"]["known_sum"] == 115
    different = deepcopy(item)
    different.native["usage"]["input_tokens"] += 1
    with pytest.raises(ValueError, match="run_id"):
        build_usage_report([item, different])


def test_codex_total_is_thread_cumulative_and_last_is_not_a_run_total():
    # Shape generated offline by codex app-server generate-json-schema:
    # v2/ThreadTokenUsageUpdatedNotification.json, local CLI 0.154.0-alpha.6.2.
    total = {"inputTokens": 100, "cachedInputTokens": 90, "outputTokens": 20,
             "reasoningOutputTokens": 8, "totalTokens": 120}
    native = {"model": "gpt-6-astra", "token_usage": {"threadId": "thread-1", "turnId": "turn-1",
              "tokenUsage": {"total": total, "last": {**total, "inputTokens": 1}}}}
    report = build_usage_report([run(native=native, backend="codex"), run("resume", native=native, backend="codex")])
    assert report["totals"]["tokens"]["total_tokens"]["known_sum"] is None
    row = report["runs"][0]
    assert row["observed_tokens"]["total_tokens"] == 120
    assert row["observed_tokens"]["non_cached_input_tokens"] == 10
    assert row["observed_tokens"]["reasoning_output_tokens"] == 8
    assert row["observed_tokens"]["cache_write_input_tokens"] is None
    assert row["scope"] == "thread_cumulative" and row["aggregation_eligible"] is False
    assert row["native_usage"]["token_usage"] == native["token_usage"]


@pytest.mark.parametrize("status", [RunStatus.RUNNING, RunStatus.PENDING_RECONCILE])
def test_unfinished_usage_is_partial_even_when_all_counters_exist(status):
    report = build_usage_report([run(native=grok(), status=status)])
    assert report["totals"]["tokens"]["total_tokens"]["known_sum"] == 115
    assert report["totals"]["tokens"]["total_tokens"]["partial"] is True
    assert report["totals"]["costs"][0]["partial"] is True


def test_partial_costs_keep_original_units_and_never_claim_billing():
    report = build_usage_report([run(native=grok(cost_is_partial=True, total_cost_usd_ticks=1234))])
    assert report["totals"]["costs"] == [
        {"unit": "USD", "known_sum": "0.1", "reported_runs": 1, "unknown_runs": 0,
         "partial": True, "source": "persisted_native_report", "billed_usage_verified": False},
        {"unit": "native_usd_ticks", "known_sum": "1234", "reported_runs": 1, "unknown_runs": 0,
         "partial": True, "source": "persisted_native_report", "billed_usage_verified": False}]


def test_task_binding_and_native_model_have_distinct_sources():
    task = Task(TaskId("task-1"), "workspace", BackendId("grok-code"), "prompt", "",
                AcceptanceStatus.PENDING, DeliveryStatus.NOT_CONFIGURED, "2026-09-15T00:00:00Z",
                account_ref=AccountRef("account:test"), model_id=ModelId("configured-model"),
                billing_pool_ref=BillingPoolRef("pool:test"))
    rows = build_usage_report([run(native=grok()), run("review", backend="other")], tasks=[task])["runs"]
    by_id = {row["run_id"]: row for row in rows}
    assert by_id["run-1"]["dimensions"]["model_id"] == "grok-observed"
    assert by_id["run-1"]["configured_model_id"] == "configured-model"
    assert by_id["run-1"]["dimensions"]["account_ref"] == "account:test"
    assert by_id["run-1"]["dimension_sources"]["account_ref"] == "task_binding"
    assert by_id["review"]["dimensions"]["account_ref"] is None
    assert by_id["run-1"]["dimensions"]["model_provider"] is None


@pytest.mark.parametrize("bad", [True, -1, 1.5, "100"])
def test_invalid_token_counter_is_not_added(bad):
    native = grok()
    native["usage"]["input_tokens"] = bad
    row = build_usage_report([run(native=native)])["runs"][0]
    assert row["tokens"]["input_tokens"] is None
    assert row["tokens"]["total_tokens"] is None


def test_unknown_adapter_and_cumulative_session_never_infer_run_total():
    rows = build_usage_report([run(native={"usage": {"total_tokens": 22}}),
                              run("session", native={"usage_scope": "session", "usage": {"total_tokens": 44}})])["runs"]
    assert {row["observed_tokens"]["total_tokens"] for row in rows} == {22, 44}
    assert all(row["tokens"]["total_tokens"] is None for row in rows)


def test_empty_report_has_no_fabricated_zero():
    report = build_usage_report([])
    assert report["totals"]["run_count"] == 0 and report["groups"] == []
    assert report["totals"]["tokens"]["total_tokens"]["known_sum"] is None


@pytest.mark.parametrize("status", [RunStatus.SUCCEEDED, RunStatus.FAILED])
def test_claude_single_invocation_cost_is_independent_of_token_convention(status):
    from asterun.backends.claude import ClaudeSession, apply_result, parse_result

    session = ClaudeSession("run-1", "/tmp", "t0")
    apply_result(session, parse_result(json.dumps({"total_cost_usd": 0.25, "session_id": "native-session"})))
    item = run(native=session.to_status_dict(), backend="custom-claude-name", status=status)
    report = build_usage_report([item, replace(item, id=RunId("repair"), native={
        **session.to_status_dict(), "session_id": "repair"}, role="repair")])
    assert report["totals"]["costs"][0]["known_sum"] == "0.50"
    assert report["totals"]["costs"][0]["reported_runs"] == 2
    assert report["totals"]["costs"][0]["partial"] is False
    assert report["totals"]["tokens"]["total_tokens"]["known_sum"] is None
    assert report["runs"][0]["costs"][0]["scope_source"] == "claude_one_shot_snapshot"


@pytest.mark.parametrize("raw,amount,reported", [({"total_cost_usd": 0}, "0.0", 1), ({}, None, 0),
                                                 ({"total_cost_usd": None}, None, 0)])
def test_claude_reported_zero_and_absent_cost_remain_distinct(raw, amount, reported):
    from asterun.backends.claude import ClaudeSession, apply_result, parse_result

    session = ClaudeSession("run-1", "/tmp", "t0")
    apply_result(session, parse_result(json.dumps(raw)))
    row = run(native=session.to_status_dict())
    report = build_usage_report([row])
    assert report["totals"]["costs"][0]["known_sum"] == amount
    assert report["totals"]["costs"][0]["reported_runs"] == reported
    assert report["totals"]["costs"][0]["unknown_runs"] == 1 - reported
    assert report["runs"][0]["native_usage"]["cost_reported"] is bool(reported)


@pytest.mark.parametrize("cost,expected", [(0.15, "0.15"), (0, None)])
def test_legacy_claude_cost_preserves_zero_ambiguity(cost, expected):
    native = {"mode": "claude", "session_id": "run-1", "resume_supported": False, "total_cost_usd": cost}
    report = build_usage_report([run(native=native, backend="renamed-backend")])
    assert report["totals"]["costs"][0]["known_sum"] == expected
    if cost == 0:
        assert report["runs"][0]["costs"][0]["issue"] == "claude_zero_may_be_adapter_default"
        assert report["runs"][0]["native_usage"]["total_cost_usd"] == 0


@pytest.mark.parametrize("marker", [{"usage_scope": "thread_cumulative"}, {"token_usage": {}}])
def test_cumulative_cost_takes_precedence_over_run_like_markers(marker):
    native = grok(**marker, mode="claude", session_id="run-1", resume_supported=False)
    report = build_usage_report([run(native=native)])
    assert report["totals"]["costs"][0]["known_sum"] is None
    assert report["runs"][0]["costs"][0]["aggregation_eligible"] is False


def test_backend_name_alone_does_not_prove_cost_scope():
    report = build_usage_report([run(native={"total_cost_usd": 3}, backend="claude")])
    assert report["totals"]["costs"][0]["known_sum"] is None


def test_detail_budget_and_cursor_preserve_all_rows_and_full_totals():
    # Each native model breakdown is large enough for the byte budget to cut a
    # page before its item limit, but not large enough to omit the individual row.
    data = build_usage_report([run(str(i), native=grok(modelUsage={"m": {"description": "中" * 2500}}))
                               for i in range(70)])
    original = deepcopy(data)
    cursor = None
    seen = []
    while True:
        page = paginate_usage_report(data, page_size=100, cursor=cursor)
        assert len(json.dumps(page, ensure_ascii=False).encode()) < 150 * 1024
        assert page["totals"] == data["totals"]
        assert page["pagination"]["totals_scope"] == "complete_query"
        assert not any("detail_omitted" in row for row in page["runs"])
        seen.extend(row["run_id"] for row in page["runs"])
        cursor = page["pagination"]["next_cursor"]
        if cursor is None:
            break
    assert seen == [row["run_id"] for row in data["runs"]]
    assert data == original


def test_single_oversized_native_detail_is_explicitly_omitted_and_cursor_advances():
    data = build_usage_report([run(native=grok(modelUsage={"model": {"raw": "大" * 600000}})), run("other")])
    page = paginate_usage_report(data, page_size=1)
    first = page["runs"][0]
    # run IDs are sorted, so select the large row on either page.
    if first["run_id"] == "other":
        page = paginate_usage_report(data, page_size=1, cursor=page["pagination"]["next_cursor"])
        first = page["runs"][0]
    assert first["detail_omitted"]["fields"] == ["native_usage"]
    assert first["detail_omitted"]["full_detail_bytes"] > 1024 * 1024
    assert first["tokens"]["total_tokens"] == 115
    assert len(json.dumps(page, ensure_ascii=False).encode()) < 150 * 1024
    assert page["totals"]["tokens"]["total_tokens"]["unknown_runs"] == 1


def test_oversized_group_dimensions_are_bounded_without_changing_totals():
    data = build_usage_report([run(native=grok(model="m" * 200000))])
    page = paginate_usage_report(data)
    assert page["groups"][0]["detail_omitted"]["fields"] == "all_except_returned_references"
    assert page["totals"] == data["totals"]
    assert len(json.dumps(page, ensure_ascii=False).encode()) < 150 * 1024


def test_pagination_rejects_invalid_or_stale_cursors():
    data = build_usage_report([run("1"), run("2")])
    page = paginate_usage_report(data, page_size=1)
    with pytest.raises(ValueError, match="cursor"):
        paginate_usage_report(data, cursor="not a cursor")
    changed = deepcopy(data)
    changed["scope"] = {"workspace": "other"}
    with pytest.raises(ValueError, match="快照或范围"):
        paginate_usage_report(changed, cursor=page["pagination"]["next_cursor"])
