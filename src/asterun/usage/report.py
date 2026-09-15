"""只汇总已保存的原生用量，不调用供应商，也不把观察换算成扣款。"""
from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from decimal import Decimal
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


def _costs(native: dict, *, eligible: bool, partial: bool) -> list[dict]:
    rows = []
    for field, unit in (("total_cost_usd", "USD"), ("total_cost_usd_ticks", "native_usd_ticks")):
        value = native.get(field)
        valid = type(value) in (int, float) and Decimal(str(value)).is_finite() and value >= 0
        rows.append({"unit": unit, "value": value if valid else None,
                     "source": "run.native." + field, "partial": partial or not valid,
                     "aggregation_eligible": eligible, "billed_usage_verified": False})
    return rows


def _row(run: Run, task: Task | None) -> dict:
    native = run.native
    dimensions, sources = _dimensions(run, task)
    issues: list[str] = []
    scope, convention, eligible = "unknown", "unknown", False
    raw = _mapping(native.get("usage"))
    # Only the adapter's explicit marker establishes the Grok counter convention.
    if native.get("usage_source") == "native_headless_report":
        scope, convention, eligible = "run", "grok_headless", True
    elif "token_usage" in native:
        notification = _mapping(native["token_usage"])
        cumulative = _mapping(notification.get("tokenUsage"))
        raw = _mapping(cumulative.get("total"))
        scope, convention = "thread_cumulative", "codex_app_server"
        issues.append("cumulative_usage_not_attributable_to_run")
        if not cumulative:
            issues.append("unrecognized_codex_usage_shape")
    elif native.get("usage_scope") in {"session", "session_cumulative", "thread_cumulative"}:
        scope = native["usage_scope"]
        issues.append("cumulative_usage_not_attributable_to_run")
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
            "costs": _costs(native, eligible=eligible, partial=cost_partial),
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
