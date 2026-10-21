"""把一次派发计划成有限回合的段，并在回合上限处续段。

plan 未启用时返回 None，不访问数据库。续段只在段末分类成立时写意图。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone

from asterun.backends.grok import DEFAULT_CODE_MAX_TURNS
from asterun.grok_profile import CODE_PROFILE
from asterun.capsule.assemble import _events, verify_capsule
from asterun.capsule.model import render_prompt
from asterun.capsule.store import _NAMESPACE, _TABLE, latest_with_blob, load_capsule, record_capsule
from asterun.config_v2 import context_budget_settings
from asterun.context_budget.ledger import budget_view
from asterun.context_budget.observe import _matched_profile
from asterun.contracts import TERMINAL_RUN_STATUSES, EventType, Operation, OperationStatus, Run, RunStatus
from asterun.errors import AsterunError
from asterun.ids import RunId, new_operation_id, new_run_id
from asterun.quality import bounded_prompt, digest
from asterun.usage.breakdown import grok_residency_curve, normalize_breakdown

SEGMENT_KINDS = frozenset({"grok", "fake"})  # 本 PR 只实现 grok；fake 用于离线测试


def _segmentation_blocked(task, run) -> bool:
    """质量流程、修复目标绑定、read_scope、capability、dependencies 不分段。只看内存字段。"""
    if getattr(task, "quality", None):
        return True
    if getattr(run, "target_hash", ""):
        return True
    native = getattr(run, "native", None)
    if isinstance(native, dict) and native.get("external_repair_target"):
        return True
    if getattr(task, "read_scope", None) is not None:
        return True
    if getattr(task, "capability", None):
        return True
    if getattr(task, "dependencies", None):
        return True
    return False


def _control_owned(app, task, run) -> bool:
    """任务的任一运行意图由 runner-control 持有（rcp_ 键或质量控制），或无法确认时，返回 True。"""
    try:
        runs = list(app.store.list_runs(task.id))
        if run.id not in [r.id for r in runs]:
            runs.append(run)
        for item in runs:
            operation = app.store.find_operation_for_run(item.id)
            if operation is None:
                continue
            if operation.idempotency_key.startswith("rcp_"):
                return True
            capabilities = getattr(app, "capabilities", None)
            if capabilities is not None and capabilities.quality_control_for_operation(operation):
                return True
        return False
    except Exception:
        return True


def _grok_execution_profile(app, run, configured):
    """运行中的后端实例优先。app.backends 不可用时读配置对象上的 execution_profile。"""
    backends = getattr(app, "backends", None)
    if backends is not None:
        key = getattr(getattr(run, "backend", None), "value", None)
        try:
            live = backends[key]
        except Exception:
            live = None
        else:
            return getattr(live, "execution_profile", None)
    return getattr(configured, "execution_profile", None)


def plan(app, task, run) -> dict | None:
    """enforce 且后端在 segmentation.backends 白名单、角色在 segmentation.roles、kind 属于 SEGMENT_KINDS、
    grok 的 execution_profile 为 workspace-code-v1、匹配 profile 有 segment_turns 且 profile.get("segment") 不是 False、
    且任务不由 runner-control 持有时，返回段计划。
    mode 不是 enforce 或白名单为空时直接返回 None，不访问数据库。
    质量流程、修复目标绑定、read_scope、capability、dependencies、runner-control 持有的任务不分段。
    text-only 的 grok 不分段。无法确认控制层归属时也不分段。"""
    if _segmentation_blocked(task, run):
        return None
    settings = context_budget_settings(getattr(app, "config", None))
    if not isinstance(settings, dict) or settings.get("mode") != "enforce":
        return None
    segmentation = settings.get("segmentation") if isinstance(settings.get("segmentation"), dict) else {}
    backends = segmentation.get("backends")
    if not isinstance(backends, list) or not backends:
        return None
    role = getattr(run, "role", None) or "implementation"
    if role == "review":
        return None
    roles = segmentation.get("roles", ["implementation", "repair"])
    if not isinstance(roles, list) or role not in roles:
        return None
    backend_ref = getattr(run, "backend", None)
    backend_name = backend_ref.value if hasattr(backend_ref, "value") else backend_ref
    if not isinstance(backend_name, str) or backend_name not in backends:
        return None
    backend = app.config.get_backend(backend_name)
    kind = backend.kind
    if kind not in SEGMENT_KINDS:
        return None
    if kind == "grok" and _grok_execution_profile(app, run, backend) != CODE_PROFILE:
        return None
    profiles = settings.get("profiles") if isinstance(settings.get("profiles"), dict) else {}
    profile_key, _role = _matched_profile(app, backend_name, role, profiles)
    profile = profiles.get(profile_key)
    if not isinstance(profile, dict) or "segment_turns" not in profile or profile.get("segment") is False:
        return None
    if _control_owned(app, task, run):
        return None
    native = getattr(run, "native", None)
    if isinstance(native, dict) and native.get("segment") is not None:
        return native["segment"]
    segment_turns = profile["segment_turns"]
    if kind == "fake":
        max_turns = segment_turns
    else:
        max_turns = min(segment_turns, backend.max_turns or DEFAULT_CODE_MAX_TURNS)
    return {
        "index": 0,
        "max_turns": max_turns,
        "profile_key": profile_key,
        "max_segments": profile.get("max_segments"),
    }


def guard_continuation(app, task, run) -> None:
    """续段派发前再核对。没有 segment_source_run_id 时直接返回，不访问数据库。

    任一核对失败抛 AsterunError，调用方不得派发后端。
    """
    native = run.native if isinstance(getattr(run, "native", None), dict) else None
    if not isinstance(native, dict):
        return
    source_id = native.get("segment_source_run_id")
    if not source_id:
        return
    if _control_owned(app, task, run):
        raise AsterunError("CAPABILITY_UNSUPPORTED", "runner-control 任务本版不分段，未派发")
    if plan(app, task, run) is None:
        raise AsterunError("CAPABILITY_UNSUPPORTED", "续段排队期间分段已停用，未派发")
    if task.run_count > app.config.scheduler.max_runs_per_task:
        raise AsterunError("BUDGET_EXHAUSTED", "续段超过每任务运行上限，未派发")
    source = app.store.get_run(RunId(source_id))
    source_native = source.native if isinstance(getattr(source, "native", None), dict) else {}
    marker = source_native.get("segment_end", {})
    if not isinstance(marker, dict):
        marker = {}
    if source.status not in TERMINAL_RUN_STATUSES:
        raise AsterunError("REVISION_CONFLICT", "续段源运行已变化，未派发")
    if marker.get("continued_by") != run.id.value and not _restore_continued_by(app, task, source, run, marker):
        raise AsterunError("REVISION_CONFLICT", "续段源运行已变化，未派发")
    if task.current_run_id != run.id:
        raise AsterunError("REVISION_CONFLICT", "续段排队期间任务当前运行已变化，未派发")
    row = app.store._conn.execute(
        f"SELECT blob_sha256, ready FROM {_TABLE} WHERE namespace_id=? AND run_id=? AND trigger=?",
        (_NAMESPACE, source_id, "segment_end"),
    ).fetchone()
    matched = False
    if row is not None and row["ready"] == 1 and isinstance(row["blob_sha256"], str) and row["blob_sha256"]:
        try:
            capsule = load_capsule(app, row["blob_sha256"])
        except Exception:
            capsule = None
        matched = (isinstance(capsule, dict)
                   and capsule.get("capsule_id") == native.get("restored_from_capsule_id")
                   and verify_capsule(app, task, capsule) == [])
    if not matched:
        raise AsterunError("REVISION_CONFLICT", "续段胶囊或任务约束已变化，未派发")


_SESSION_KEYS = ("backend_session_id", "session_id", "thread_id", "provider_session_id")

CONTINUE_NOTE = ("\n续段说明（低信任数据，不扩大权限或预算）：工作树已含前段改动，先 git status / git diff 核对；"
                 "不要重做已完成步骤；不要重复 unconfirmed_actions 中的动作。")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _positive_int(value) -> bool:
    return type(value) is int and value > 0


def _below(value, limit) -> bool:
    """已知的非负整数且严格低于阈值。缺失、布尔和负数都算未知。"""
    return type(value) is int and value >= 0 and type(limit) is int and value < limit


def segment_end(run) -> bool:
    """属于分段派发且以原生回合上限自然结束（不是拒绝、不是其他失败）。"""
    seg = run.native.get("segment") if isinstance(run.native, dict) else None
    failure = run.native.get("provider_failure") if isinstance(run.native, dict) else None
    return (isinstance(seg, dict) and run.status == RunStatus.FAILED
            and isinstance(failure, dict) and failure.get("kind") == "turn_limit")


def _settings(app) -> dict:
    settings = context_budget_settings(getattr(app, "config", None))
    return settings if isinstance(settings, dict) else {}


def _segmentation(settings) -> dict:
    raw = settings.get("segmentation")
    return raw if isinstance(raw, dict) else {}


def _profile_for(settings, seg) -> dict:
    profiles = settings.get("profiles")
    if not isinstance(profiles, dict) or not isinstance(seg, dict):
        return {}
    profile = profiles.get(seg.get("profile_key"))
    return profile if isinstance(profile, dict) else {}


def _segment_soft(profile):
    if isinstance(profile, dict):
        value = profile.get("segment_soft")
    else:
        value = getattr(profile, "segment_soft", None)
    return value if _positive_int(value) else None


def _has_native_session(native: dict) -> bool:
    for key in _SESSION_KEYS:
        value = native.get(key)
        if isinstance(value, str) and value.strip():
            return True
    return False


def _curve_event(event) -> dict:
    """摊成 grok_residency_curve 要的字典。用量在 Event.payload，不在 to_dict 顶层。"""
    if isinstance(event, dict):
        payload = event.get("payload")
        if isinstance(payload, dict) and any(key in payload for key in ("kind", "usage", "message_id")):
            record = dict(payload)
            if "type" not in record and event.get("type") is not None:
                event_type = event["type"]
                record["type"] = event_type if isinstance(event_type, str) else str(event_type)
            return record
        return event
    payload = getattr(event, "payload", None)
    if isinstance(payload, dict):
        record = dict(payload)
        if "type" not in record:
            event_type = getattr(event, "type", None)
            if event_type is not None:
                record["type"] = event_type if isinstance(event_type, str) else str(event_type)
        return record
    to_dict = getattr(event, "to_dict", None)
    if callable(to_dict):
        data = to_dict()
        if isinstance(data, dict):
            return _curve_event(data)
    return {}


def resume_decision(app, task, run, profile) -> str:
    """处理量与驻留量都已知且低于阈值、且 run 有原生会话时返回 resume；非 grok 或未知一律新会话。"""
    del task, profile
    try:
        kind = app.config.get_backend(run.backend.value).kind
        if kind != "grok":
            return "new_session"
        segmentation = _segmentation(_settings(app))
        native = run.native if isinstance(run.native, dict) else {}
        processed = normalize_breakdown(native)["processed"]
        if not _below(processed, segmentation.get("min_long_task_processed", 600_000)):
            return "new_session"
        curve = grok_residency_curve(_curve_event(event) for event in _events(app.store, run.id))
        if curve["count"] == 0 or curve["partial"]:
            return "new_session"
        if not _below(curve["peak"], segmentation.get("resume_max_residency", 100_000)):
            return "new_session"
        if not _has_native_session(native):
            return "new_session"
        return "resume"
    except Exception:
        return "new_session"


def _mark_segment_end(app, run, reason: str, continued_by: str | None) -> None:
    marker = {"reason": reason, "continued_by": continued_by}
    if isinstance(getattr(run, "native", None), dict):
        run.native["segment_end"] = marker
    src = app.store.get_run(run.id)
    if src is not run:
        if not isinstance(src.native, dict):
            src.native = {}
        src.native["segment_end"] = {"reason": reason, "continued_by": continued_by}
    app.store.save_run(src)


def _restore_continued_by(app, task, source, run, marker) -> bool:
    """continued_by 缺失时，用指向本 run 的续段幂等键把标记补上。已有其他后继则不改。"""
    if marker.get("continued_by"):
        return False
    native = run.native if isinstance(getattr(run, "native", None), dict) else {}
    if native.get("segment_source_run_id") != source.id.value:
        return False
    segment = native.get("segment")
    if not isinstance(segment, dict) or "index" not in segment:
        return False
    operation = app.store.find_operation_for_run(run.id)
    expected = f"seg:{task.id.value}:{source.id.value}:{segment['index']}"
    if operation is None or operation.idempotency_key != expected:
        return False
    reason = marker.get("reason") if isinstance(marker.get("reason"), str) and marker.get("reason") else "turn_budget"
    if not isinstance(source.native, dict):
        source.native = {}
    source.native["segment_end"] = {"reason": reason, "continued_by": run.id.value}
    app.store.save_run(source)
    return True


def _backfill_continued_by(app, task, run) -> None:
    """幂等重放时，若源 run 还没有 continued_by，补成已存在后继的 id。"""
    src = app.store.get_run(run.id)
    native = src.native if isinstance(src.native, dict) else {}
    marker = native.get("segment_end") if isinstance(native.get("segment_end"), dict) else {}
    if marker.get("continued_by"):
        return
    current = app.store.get_task(task.id)
    successor_id = current.current_run_id
    if successor_id is None or successor_id == run.id:
        return
    successor = app.store.get_run(successor_id)
    succ_native = successor.native if isinstance(successor.native, dict) else {}
    if succ_native.get("segment_source_run_id") != run.id.value:
        return
    reason = marker.get("reason") if isinstance(marker.get("reason"), str) and marker.get("reason") else "turn_budget"
    if not isinstance(src.native, dict):
        src.native = {}
    src.native["segment_end"] = {"reason": reason, "continued_by": successor_id.value}
    app.store.save_run(src)


@contextmanager
def _connection_transaction(store):
    """连接空闲时开 BEGIN IMMEDIATE，让 commit_intent 变成 SAVEPOINT 而不是自行 COMMIT。

    SqliteStore 没有 transaction()，Application._state_transaction() 因此是空上下文。
    已经在事务里时不再 BEGIN，避免嵌套 BEGIN 失败。
    """
    conn = getattr(store, "_conn", None)
    if conn is None or conn.in_transaction:
        yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _stop(app, task, run, reason: str) -> None:
    _mark_segment_end(app, run, reason, None)
    if reason in {"max_segments", "budget"}:
        try:
            record_capsule(app, task, run, trigger="budget_soft")
        except Exception:
            pass


def _capsule_not_ready(app, task, run) -> dict:
    _stop(app, task, run, "capsule_not_ready")
    return {"stopped": "capsule_not_ready"}


def _segmentation_disabled(run) -> bool:
    """segment_end 已记下停用。重新 enforce 后不得再补后继。"""
    native = run.native if isinstance(getattr(run, "native", None), dict) else {}
    marker = native.get("segment_end")
    return isinstance(marker, dict) and marker.get("reason") == "segmentation_disabled"


def _continue_segment(app, task, run) -> dict | None:
    if not segment_end(run):
        return None
    # 停用判定一旦落盘就结束。调用方手里的 run 可能是旧对象，以库中的标记为准。
    if _segmentation_disabled(run) or _segmentation_disabled(app.store.get_run(run.id)):
        return {"stopped": "segmentation_disabled"}
    if task.paused or task.orchestration_owner == "human" or task.current_run_id != run.id:
        return None
    if _control_owned(app, task, run):
        _stop(app, task, run, "control_owned")
        return {"stopped": "control_owned"}
    if plan(app, task, run) is None:
        _stop(app, task, run, "segmentation_disabled")
        return {"stopped": "segmentation_disabled"}
    seg = run.native["segment"]
    next_index = seg["index"] + 1
    max_segments = seg.get("max_segments")
    if _positive_int(max_segments) and next_index >= max_segments:
        _stop(app, task, run, "max_segments")
        return {"stopped": "max_segments"}
    if task.run_count >= app.config.scheduler.max_runs_per_task:
        _stop(app, task, run, "max_runs")
        return {"stopped": "max_runs"}
    settings = _settings(app)
    profile = _profile_for(settings, seg)
    soft = _segment_soft(profile)
    view = budget_view(getattr(app.store, "_conn", None), task.id.value)
    if view and soft is not None:
        settled, held, task_hard = view.get("settled"), view.get("held"), view.get("task_hard")
        if type(settled) is int and type(held) is int and type(task_hard) is int and settled + held + soft > task_hard:
            _stop(app, task, run, "budget")
            return {"stopped": "budget"}
    try:
        row = record_capsule(app, task, run, trigger="segment_end")
    except Exception:
        return _capsule_not_ready(app, task, run)
    if not isinstance(row, dict) or row.get("ready") != 1:
        return _capsule_not_ready(app, task, run)
    try:
        latest = latest_with_blob(app.store._conn, task.id.value)
    except Exception:
        return _capsule_not_ready(app, task, run)
    blob_sha = latest.get("blob_sha256") if isinstance(latest, dict) else None
    if (not isinstance(latest, dict) or latest.get("run_id") != run.id.value
            or latest.get("trigger") != "segment_end"
            or not isinstance(blob_sha, str) or not blob_sha):
        return _capsule_not_ready(app, task, run)
    try:
        capsule = load_capsule(app, blob_sha)
    except Exception:
        return _capsule_not_ready(app, task, run)
    kind = app.config.get_backend(run.backend.value).kind
    try:
        prompt = bounded_prompt(render_prompt(capsule, backend_kind=kind) + CONTINUE_NOTE)
    except AsterunError:
        _stop(app, task, run, "capsule_too_large")
        return {"stopped": "capsule_too_large"}
    now = _utc_now()
    operation = Operation(
        id=new_operation_id(),
        idempotency_key=f"seg:{task.id.value}:{run.id.value}:{next_index}",
        principal=app.principal.subject_id,
        action="segment.continue",
        target=task.id.value,
        input_hash=digest({
            "version": 1,
            "source_run": run.id.value,
            "index": next_index,
            "capsule_id": row["capsule_id"],
        }),
        status=OperationStatus.INTENDED,
        created_at=now,
    )
    if app.store.find_idempotent_operation(operation) is not None:
        _backfill_continued_by(app, task, run)
        return {"reused": True}
    if not app.scheduler.can_enqueue():
        _stop(app, task, run, "queue_full")
        return {"stopped": "queue_full"}
    new_run = Run(
        id=new_run_id(),
        task_id=task.id,
        backend=task.backend,
        status=RunStatus.QUEUED,
        created_at=now,
        role=run.role,
        prompt=prompt,
        native={
            "segment": {**seg, "index": next_index},
            "restored_from_capsule_id": row["capsule_id"],
            "segment_source_run_id": run.id.value,
            "segment_resume": resume_decision(app, task, run, profile),
        },
    )
    task = replace(task, run_count=task.run_count + 1, current_run_id=new_run.id)
    # _state_transaction 在 SqliteStore 上是空上下文；_connection_transaction 补上真正的外层事务。
    with app._state_transaction():
        with _connection_transaction(app.store):
            operation, task, new_run, reused = app.store.commit_intent(operation, task, new_run)
            if not reused:
                src = app.store.get_run(run.id)
                src.native["segment_end"] = {"reason": "turn_budget", "continued_by": new_run.id.value}
                app.store.save_run(src)
            else:
                _backfill_continued_by(app, task, run)
    if not reused and isinstance(getattr(run, "native", None), dict):
        run.native["segment_end"] = {"reason": "turn_budget", "continued_by": new_run.id.value}
    if reused:
        return {"reused": True}
    workspace_root = str(app.config.get_workspace(task.workspace).root.resolve())
    app.scheduler.enqueue(task.backend.value, task.id, new_run.id, workspace_root)
    app._event(new_run.id, EventType.RUN_QUEUED, "core",
               {"segment_continue": True, "segment_index": next_index})
    return {"continued": new_run.id.value, "index": next_index}


def continue_segment(app, task, run) -> dict | None:
    """段末时幂等地提交后继段意图并排队。不满足条件返回 None 或 {"stopped": 原因}。"""
    try:
        return _continue_segment(app, task, run)
    except Exception as exc:
        try:
            app._event(run.id, EventType.MESSAGE, "core", {"segment_continue_error": type(exc).__name__})
        except Exception:
            pass
        return None
