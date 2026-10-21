"""把一次派发计划成有限回合的段，并在回合上限处续段。

plan 未启用时返回 None，不访问数据库。续段只在段末分类成立时写意图。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from asterun.backends.grok import DEFAULT_CODE_MAX_TURNS
from asterun.capsule.assemble import verify_capsule
from asterun.capsule.model import render_prompt
from asterun.capsule.store import _NAMESPACE, _TABLE, latest_with_blob, load_capsule, record_capsule
from asterun.config_v2 import context_budget_settings
from asterun.context_budget.ledger import budget_view
from asterun.context_budget.observe import _matched_profile
from asterun.contracts import TERMINAL_RUN_STATUSES, EventType, Operation, OperationStatus, Run, RunStatus
from asterun.errors import AsterunError
from asterun.ids import RunId, new_operation_id, new_run_id
from asterun.quality import bounded_prompt, digest
from asterun.usage.breakdown import normalize_breakdown

SEGMENT_KINDS = frozenset({"grok", "fake"})  # 本 PR 只实现 grok；fake 用于离线测试


def plan(app, task, run) -> dict | None:
    """enforce 且后端在 segmentation.backends 白名单、角色在 segmentation.roles、kind 属于 SEGMENT_KINDS、
    匹配 profile 有 segment_turns 且 profile.get("segment") 不是 False 时，返回段计划；否则 None（不访问数据库）。"""
    del task
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
    profiles = settings.get("profiles") if isinstance(settings.get("profiles"), dict) else {}
    profile_key, _role = _matched_profile(app, backend_name, role, profiles)
    profile = profiles.get(profile_key)
    if not isinstance(profile, dict) or "segment_turns" not in profile or profile.get("segment") is False:
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
    if plan(app, task, run) is None:
        raise AsterunError("CAPABILITY_UNSUPPORTED", "续段排队期间分段已停用，未派发")
    source = app.store.get_run(RunId(source_id))
    source_native = source.native if isinstance(getattr(source, "native", None), dict) else {}
    marker = source_native.get("segment_end", {})
    if not isinstance(marker, dict):
        marker = {}
    if source.status not in TERMINAL_RUN_STATUSES or marker.get("continued_by") != run.id.value:
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


def resume_decision(app, task, run, profile) -> str:
    """处理量与驻留量都已知且低于阈值、且 run 有原生会话时返回 resume；未知一律新会话。"""
    del task, profile
    try:
        segmentation = _segmentation(_settings(app))
        native = run.native if isinstance(run.native, dict) else {}
        processed = normalize_breakdown(native)["processed"]
        if not _below(processed, segmentation.get("min_long_task_processed", 600_000)):
            return "new_session"
        if not _below(native.get("peak_residency"), segmentation.get("resume_max_residency", 100_000)):
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


def _continue_segment(app, task, run) -> dict | None:
    if not segment_end(run):
        return None
    if task.paused or task.orchestration_owner == "human" or task.current_run_id != run.id:
        return None
    if plan(app, task, run) is None:
        return {"stopped": "segmentation_disabled"}
    seg = run.native["segment"]
    next_index = seg["index"] + 1
    max_segments = seg.get("max_segments")
    if _positive_int(max_segments) and next_index >= max_segments:
        _stop(app, task, run, "max_segments")
        return {"stopped": "max_segments"}
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
        return {"reused": True}
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
    operation, task, new_run, reused = app.store.commit_intent(operation, task, new_run)
    if reused:
        return {"reused": True}
    _mark_segment_end(app, run, "turn_budget", new_run.id.value)
    workspace_root = str(app.config.get_workspace(task.workspace).root.resolve())
    app.scheduler.enqueue(task.backend.value, task.id, new_run.id, workspace_root)
    app._event(new_run.id, EventType.RUN_QUEUED, "core",
               {"segment_continue": True, "segment_index": next_index})
    return {"continued": new_run.id.value, "index": next_index}


def continue_segment(app, task, run) -> dict | None:
    """段末时幂等地提交后继段意图并排队。不满足条件返回 None 或 {"stopped": 原因}。"""
    try:
        return _continue_segment(app, task, run)
    except Exception:
        return None
