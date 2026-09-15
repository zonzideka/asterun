"""只汇总已保存的原生用量，不调用供应商，也不把观察换算成扣款。"""
from __future__ import annotations

from collections.abc import Iterable
import base64
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
from typing import Any

from asterun.contracts import Run, Task, TERMINAL_RUN_STATUSES


TOKEN_FIELDS = (
    "input_tokens", "non_cached_input_tokens", "cached_input_tokens",
    "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens",
)
DIMENSIONS = ("backend", "model_provider", "model_id", "account_ref", "runtime_ref", "billing_pool_ref")
_NATIVE_FIELDS = (
    "usage", "modelUsage", "token_usage", "usage_source", "usage_scope",
    "usage_is_incomplete", "cost_is_partial", "total_cost_usd", "total_cost_usd_ticks",
    "num_turns", "model", "model_id", "model_provider",
    "mode", "session_id", "resume_supported", "cost_reported", "cost_source",
)


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _counter(value: Any) -> int | None:
    # bool is an int in Python; neither bool nor fractional tokens is a counter.
    return value if type(value) is int and value >= 0 else None


def _sum_known(*values: int | None) -> int | None:
    return sum(values) if all(value is not None for value in values) else None


def _empty_tokens() -> dict[str, int | None]:
    return dict.fromkeys(TOKEN_FIELDS)


def _tokens(raw: dict, *, convention: str, issues: list[str], sources: dict) -> dict:
    result = _empty_tokens()
    if convention == "grok_headless":
        ordinary = _counter(raw.get("input_tokens"))
        cached = _counter(raw.get("cache_read_input_tokens"))
        written = _counter(raw.get("cache_creation_input_tokens"))
        result.update(input_tokens=_sum_known(ordinary, cached, written),
                      non_cached_input_tokens=_sum_known(ordinary, written),
                      cached_input_tokens=cached, cache_write_input_tokens=written,
                      output_tokens=_counter(raw.get("output_tokens")),
                      reasoning_output_tokens=_counter(raw.get("reasoning_tokens")),
                      total_tokens=_counter(raw.get("total_tokens")))
        sources.update({"input_tokens": "sum(input_tokens,cache_read_input_tokens,cache_creation_input_tokens)",
                        "non_cached_input_tokens": "sum(input_tokens,cache_creation_input_tokens)",
                        "cached_input_tokens": "usage.cache_read_input_tokens",
                        "cache_write_input_tokens": "usage.cache_creation_input_tokens",
                        "output_tokens": "usage.output_tokens", "reasoning_output_tokens": "usage.reasoning_tokens",
                        "total_tokens": "usage.total_tokens"})
        reported, output = result["total_tokens"], result["output_tokens"]
        if result["input_tokens"] is None and reported is not None and output is not None:
            inferred = reported - output
            lower_bound = sum(value for value in (ordinary, cached, written) if value is not None)
            if inferred >= lower_bound:
                result["input_tokens"] = inferred
                sources["input_tokens"] = "difference(usage.total_tokens,usage.output_tokens)"
                if cached is not None:
                    result["non_cached_input_tokens"] = inferred - cached
                    sources["non_cached_input_tokens"] = "difference(input_tokens,cached_input_tokens)"
            else:
                issues.append("reported_total_less_than_known_components")
    elif convention == "codex_app_server":
        aliases = {"input_tokens": "inputTokens", "cached_input_tokens": "cachedInputTokens",
                   "cache_write_input_tokens": "cacheWriteInputTokens", "output_tokens": "outputTokens",
                   "reasoning_output_tokens": "reasoningOutputTokens", "total_tokens": "totalTokens"}
        result.update({key: _counter(raw.get(name)) for key, name in aliases.items()})
        sources.update({key: "tokenUsage.total." + name for key, name in aliases.items()})
        input_count, cached = result["input_tokens"], result["cached_input_tokens"]
        if input_count is not None and cached is not None:
            if input_count >= cached:
                result["non_cached_input_tokens"] = input_count - cached
                sources["non_cached_input_tokens"] = "difference(inputTokens,cachedInputTokens)"
            else:
                issues.append("cached_input_exceeds_input")
    else:
        # Other adapters do not establish the relationship between cache/thinking
        # counters and input/output. A reported total remains useful on its own.
        result["total_tokens"] = _counter(raw.get("total_tokens"))
        sources["total_tokens"] = "usage.total_tokens"
    calculated = _sum_known(result["input_tokens"], result["output_tokens"])
    if result["total_tokens"] is None:
        result["total_tokens"] = calculated
        sources["total_tokens"] = "sum(input_tokens,output_tokens)"
    elif calculated is not None and result["total_tokens"] != calculated:
        issues.append("reported_total_differs_from_components")
    reasoning, output = result["reasoning_output_tokens"], result["output_tokens"]
    if reasoning is not None and output is not None and reasoning > output:
        issues.append("reasoning_output_exceeds_output")
    sources.update({field: "unknown" for field, value in result.items() if value is None})
    return result


def _dimensions(run: Run, task: Task | None) -> tuple[dict, dict]:
    native = run.native
    values = {"backend": run.backend.value, **{key: _text(native.get(key)) for key in DIMENSIONS[1:]}}
    sources = {key: "run.backend" if key == "backend" else "run.native" if value else "unknown"
               for key, value in values.items()}
    models = _mapping(native.get("modelUsage"))
    model = _text(native.get("model")) or _text(_mapping(native.get("init_evidence")).get("model"))
    if not values["model_id"] and model:
        values["model_id"], sources["model_id"] = model, "run.native.model"
    if not values["model_id"] and len(models) == 1:
        values["model_id"] = _text(next(iter(models)))
        sources["model_id"] = "run.native.modelUsage"
    # A review can use a different backend/account from its implementation task.
    if task is not None and task.backend == run.backend:
        for key in ("account_ref", "runtime_ref", "billing_pool_ref"):
            reference = getattr(task, key)
            if values[key] is None and reference is not None:
                values[key], sources[key] = reference.value, "task_binding"
    return values, sources


def _costs(run: Run, *, eligible: bool, partial: bool, cumulative: bool) -> list[dict]:
    native = run.native
    # ClaudeBackend creates a new ClaudeSession(session_id=run_id) for each
    # invocation. Identify that persisted shape, not the configurable backend name.
    claude_one_shot = (not cumulative and native.get("mode") == "claude"
                       and native.get("session_id") == run.id.value
                       and native.get("resume_supported") is False)
    rows = []
    for field, unit in (("total_cost_usd", "USD"), ("total_cost_usd_ticks", "native_usd_ticks")):
        value = native.get(field)
        valid = type(value) in (int, float) and Decimal(str(value)).is_finite() and value >= 0
        cost_eligible = not cumulative and (eligible or (claude_one_shot and unit == "USD"))
        # The historical Claude adapter defaults absent/unparsed cost to 0.0.
        # Positive values prove a parsed result; a zero cannot prove a free run.
        reported = native.get("cost_reported")
        ambiguous_zero = (claude_one_shot and unit == "USD" and valid and value == 0
                          and reported is not True)
        unreported = claude_one_shot and unit == "USD" and reported is False
        known = valid and not ambiguous_zero and not unreported
        rows.append({"unit": unit, "value": value if known else None,
                     "source": "run.native." + field, "partial": partial or not known,
                     "scope": "run" if cost_eligible else "unknown",
                     "scope_source": "claude_one_shot_snapshot" if claude_one_shot and unit == "USD"
                     else "native_headless_report" if eligible and not cumulative else "unknown",
                     "aggregation_eligible": cost_eligible and not ambiguous_zero and not unreported,
                     "issue": "claude_cost_not_reported" if unreported else
                     "claude_zero_may_be_adapter_default" if ambiguous_zero else None,
                     "billed_usage_verified": False})
    return rows


def _row(run: Run, task: Task | None) -> dict:
    native = run.native
    dimensions, sources = _dimensions(run, task)
    issues: list[str] = []
    scope, convention, eligible = "unknown", "unknown", False
    raw = _mapping(native.get("usage"))
    # Only the adapter's explicit marker establishes the Grok counter convention.
    cumulative = "token_usage" in native or native.get("usage_scope") in {
        "session", "session_cumulative", "thread_cumulative"}
    if "token_usage" in native:
        notification = _mapping(native["token_usage"])
        cumulative_tokens = _mapping(notification.get("tokenUsage"))
        raw = _mapping(cumulative_tokens.get("total"))
        scope, convention = "thread_cumulative", "codex_app_server"
        issues.append("cumulative_usage_not_attributable_to_run")
        if not cumulative_tokens:
            issues.append("unrecognized_codex_usage_shape")
    elif native.get("usage_scope") in {"session", "session_cumulative", "thread_cumulative"}:
        scope = native["usage_scope"]
        issues.append("cumulative_usage_not_attributable_to_run")
    elif native.get("usage_source") == "native_headless_report":
        scope, convention, eligible = "run", "grok_headless", True
    else:
        issues.append("usage_scope_or_convention_unknown")
    token_sources: dict[str, str] = {}
    observed = _tokens(raw, convention=convention, issues=issues, sources=token_sources)
    incomplete = native.get("usage_is_incomplete") is True or run.status not in TERMINAL_RUN_STATUSES
    cost_partial = incomplete or native.get("cost_is_partial") is True
    if len(_mapping(native.get("modelUsage"))) > 1:
        issues.append("multiple_native_models_preserved_in_breakdown")
    if not any(value is not None for value in observed.values()):
        issues.append("token_usage_unknown")
    return {"run_id": run.id.value, "task_id": run.task_id.value, "role": run.role,
            "status": str(run.status), "created_at": run.created_at,
            "dimensions": dimensions, "dimension_sources": sources,
            "configured_model_id": None if task is None or task.backend != run.backend or task.model_id is None else task.model_id.value,
            "scope": scope, "convention": convention, "aggregation_eligible": eligible,
            "tokens": observed if eligible else _empty_tokens(), "observed_tokens": observed,
            "token_sources": token_sources,
            "usage_is_partial": incomplete or bool(issues),
            "costs": _costs(run, eligible=eligible, partial=cost_partial, cumulative=cumulative),
            "native_usage": deepcopy({key: native[key] for key in _NATIVE_FIELDS if key in native}),
            "issues": issues}


def _aggregate(rows: list[dict]) -> dict:
    tokens = {}
    for field in TOKEN_FIELDS:
        known = [row["tokens"][field] for row in rows if row["tokens"][field] is not None]
        tokens[field] = {"known_sum": sum(known) if known else None, "reported_runs": len(known),
                         "unknown_runs": len(rows) - len(known),
                         "partial": len(known) != len(rows) or any(row["usage_is_partial"] for row in rows)}
    costs = []
    for unit in ("USD", "native_usd_ticks"):
        selected = [cost for row in rows for cost in row["costs"] if cost["unit"] == unit]
        known = [cost for cost in selected if cost["aggregation_eligible"] and cost["value"] is not None]
        amount = sum((Decimal(str(cost["value"])) for cost in known), Decimal(0))
        costs.append({"unit": unit, "known_sum": str(amount) if known else None,
                      "reported_runs": len(known), "unknown_runs": len(rows) - len(known),
                      "partial": len(known) != len(rows) or any(cost["partial"] for cost in known),
                      "source": "persisted_native_report", "billed_usage_verified": False})
    return {"run_count": len(rows), "task_count": len({row["task_id"] for row in rows}),
            "tokens": tokens, "costs": costs}


def build_usage_report(runs: Iterable[Run], *, tasks: Iterable[Task] = ()) -> dict:
    """Aggregate unique persisted runs; cumulative observations stay out of run totals.

    Inputs include cache reads and writes in the normalized fields. Reasoning is
    an output subset, and modelUsage is a breakdown, never an extra charge.
    Missing components are null; known_sum is a partial sum, not a complete bill.
    """
    tasks_by_id = {task.id.value: task for task in tasks}
    unique: dict[str, Run] = {}
    duplicates = 0
    for run in runs:
        previous = unique.get(run.id.value)
        if previous is not None:
            if previous.to_dict() != run.to_dict():
                raise ValueError("同一 run_id 有不同快照，不能确定用量版本")
            duplicates += 1
        unique[run.id.value] = run
    rows = [_row(run, tasks_by_id.get(run.task_id.value))
            for run in sorted(unique.values(), key=lambda run: (run.created_at, run.id.value))]
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        key = tuple(row["dimensions"][field] for field in DIMENSIONS)
        groups.setdefault(key, []).append(row)
    return {"schema_version": 1, "source": "persisted_run_native", "billed_usage_verified": False,
            "duplicate_runs_ignored": duplicates, "runs": rows, "totals": _aggregate(rows),
            "groups": [{"dimensions": dict(zip(DIMENSIONS, key)), **_aggregate(group)}
                       for key, group in groups.items()],
            "notes": ["known_sum 仅汇总可归属到所选运行的已知计数，未知不按零处理。",
                      "input_tokens 包含缓存读取和写入；reasoning_output_tokens 已包含在 output_tokens。",
                      "modelUsage 是原生分解，不与运行总量重复相加。",
                      "会话累计用量保留为观察值；没有运行起点基线时不计入运行总量。",
                      "USD 与 native_usd_ticks 分开保留，不相加、不换算，不代表实际扣款。"]}


_DETAIL_COLLECTIONS = ("tasks", "groups", "runs")
DETAIL_BUDGET_BYTES = 128 * 1024
_ITEM_BUDGET_BYTES = 16 * 1024


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")


def _bounded_detail(row: dict) -> dict:
    encoded = _json_bytes(row)
    if len(encoded) <= _ITEM_BUDGET_BYTES:
        return row
    omitted = {"reason": "detail_byte_limit", "full_detail_bytes": len(encoded),
               "full_detail_sha256": hashlib.sha256(encoded).hexdigest()}
    if "native_usage" in row:
        reduced = {key: value for key, value in row.items() if key != "native_usage"}
        reduced["detail_omitted"] = {**omitted, "fields": ["native_usage"]}
        if len(_json_bytes(reduced)) <= _ITEM_BUDGET_BYTES:
            return reduced
    # A model/account identifier can itself be very large. A small explicit
    # placeholder advances the cursor; totals were computed before this step.
    reduced = {key: row[key] for key in ("task_id", "run_id", "dimensions")
               if key in row and len(_json_bytes(row[key])) <= 2048}
    reduced["detail_omitted"] = {**omitted, "fields": "all_except_returned_references"}
    return reduced


def paginate_usage_report(data: dict, *, page_size: int = 20, cursor: str | None = None) -> dict:
    """Bound detail output, retaining totals for the entire authorized query.

    Cursors name the saved report snapshot and all three detail offsets. They
    are not authorization grants; the application rechecks every task first.
    """
    if type(page_size) is not int or not 1 <= page_size <= 100:
        raise ValueError("page_size 必须在 1 到 100 之间")
    snapshot = hashlib.sha256(_json_bytes(data)).hexdigest()
    collections = [data.get(key, []) for key in _DETAIL_COLLECTIONS]
    counts = [len(rows) for rows in collections]
    offsets = [0, 0, 0]
    if cursor is not None:
        try:
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= 256:
                raise ValueError
            decoded = json.loads(base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True))
            offsets = decoded["offsets"]
            if (set(decoded) != {"snapshot", "offsets"} or not isinstance(offsets, list)
                    or len(offsets) != 3 or any(type(value) is not int or value < 0 or value > count
                                              for value, count in zip(offsets, counts))):
                raise ValueError
        except (ValueError, KeyError, TypeError, UnicodeError) as exc:
            raise ValueError("用量报告 cursor 无效，请从第一页重新查询") from exc
        if decoded["snapshot"] != snapshot:
            raise ValueError("用量报告快照或范围已变化，请省略 cursor 从第一页重新查询")
    starts = offsets.copy()
    pages: list[list[dict]] = [[], [], []]
    byte_count, budget_full = 0, False
    while not budget_full:
        advanced = False
        for index, rows in enumerate(collections):
            if offsets[index] >= len(rows) or len(pages[index]) >= page_size:
                continue
            row = _bounded_detail(rows[offsets[index]])
            size = len(_json_bytes(row)) + 2
            if byte_count + size > DETAIL_BUDGET_BYTES:
                budget_full = True
                break
            pages[index].append(row)
            byte_count += size
            offsets[index] += 1
            advanced = True
        if not advanced:
            break
    has_more = offsets != counts
    next_cursor = (base64.urlsafe_b64encode(_json_bytes({"snapshot": snapshot, "offsets": offsets})).decode("ascii")
                   if has_more else None)
    result = {key: value for key, value in data.items() if key not in _DETAIL_COLLECTIONS}
    result.update({key: rows for key, rows in zip(_DETAIL_COLLECTIONS, pages) if key in data})
    result["pagination"] = {
        "page_size": page_size, "snapshot": snapshot, "next_cursor": next_cursor,
        "has_more": has_more, "totals_scope": "complete_query",
        "detail_budget_bytes": DETAIL_BUDGET_BYTES,
        "collections": {key: {"total_items": count, "offset": offset, "returned_items": len(rows),
                              "omitted_items": sum("detail_omitted" in row for row in rows)}
                        for key, count, offset, rows in zip(_DETAIL_COLLECTIONS, counts, starts, pages)
                        if key in data}}
    return result
