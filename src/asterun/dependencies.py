"""复用任务队列的依赖门禁；只依赖已存在任务，不引入第二个执行账本。"""
from __future__ import annotations

from asterun.contracts import AcceptanceStatus, RunStatus
from asterun.errors import AsterunError
from asterun.ids import RunId
from asterun.policy import enforce


def validate(app, workspace, ids):
    if not isinstance(ids, list) or len(ids) > 16 or len(ids) != len(set(ids)):
        raise AsterunError("INVALID_REQUEST", "depends_on 最多 16 个不同的既有任务")
    root = app.config.get_workspace(workspace).root.resolve()
    visited = set()
    pending = list(ids)
    while pending:
        id = pending.pop()
        if id in visited:
            continue
        if len(visited) >= 128:
            raise AsterunError("INVALID_REQUEST", "依赖图超过 128 个任务")
        visited.add(id)
        task = app._require_task(id)
        enforce(app.policy.authorize(app.principal, "task.get", task.workspace))
        enforce(app.policy.authorize(app.principal, "workspace.read", task.workspace))
        app._require_grant(task.workspace)
        app._assert_task_binding(task)
        parent_root = app.config.get_workspace(task.workspace).root.resolve()
        if root == parent_root or root.is_relative_to(parent_root) or parent_root.is_relative_to(root):
            raise AsterunError("INVALID_REQUEST", "依赖任务须使用独立且不嵌套的工作树，避免写入上游验收目标")
        pending.extend(task.dependencies)
    return list(ids)


def inspect(app, task, run=None, *, seen=(), memo=None):
    memo = {} if memo is None else memo
    if task.id.value in seen or len(seen) >= 128:
        raise AsterunError("INVALID_REQUEST", "依赖图循环或超过深度上限")
    if task.id.value in memo:
        return memo[task.id.value]
    if len(memo) >= 128:
        raise AsterunError("INVALID_REQUEST", "依赖图超过 128 个任务")
    bindings = {}
    blocked = []
    for id in task.dependencies:
        parent = app._require_task(id)
        enforce(app.policy.authorize(app.principal, "task.get", parent.workspace))
        enforce(app.policy.authorize(app.principal, "workspace.read", parent.workspace))
        app._require_grant(parent.workspace)
        app._assert_task_binding(parent)
        parent = app.quality.validate_evidence(parent)
        current = app.store.get_run(parent.current_run_id) if parent.current_run_id else None
        parent = app._validate_external_evidence(parent, current)
        from asterun.stage_gates import revalidate
        parent = revalidate(app, parent, current)
        ancestors = inspect(app, parent, current, seen=(*seen, task.id.value), memo=memo) if parent.dependencies else {"ready": True}
        target = (parent.quality.get("target", {}).get("hash") if parent.quality else
                  parent.external_evaluation.get("target_hash"))
        ready = (parent.acceptance == AcceptanceStatus.PASSED and not parent.evidence_stale
                 and current and current.status == RunStatus.SUCCEEDED and not current.cancel_requested
                 and target and ancestors["ready"])
        if not ready:
            blocked.append(id)
            continue
        bindings[id] = {"run_id": current.id.value, "input_hash": parent.input_hash,
                        "target_hash": target, "revision": parent.config_revision}
    expected = run.native.get("dependency_bindings") if run else None
    stale = expected is not None and expected != bindings
    result = {"ready": not blocked and not stale, "bindings": bindings, "blocked": blocked, "stale": stale}
    memo[task.id.value] = result
    return result


def bind(app, task, run):
    if not task.dependencies:
        return
    original = (app.store.get_run(RunId(task.quality["implementation_run_id"]))
                if run.role == "review" else None)
    report = inspect(app, task, original)
    if not report["ready"]:
        raise AsterunError("REVISION_CONFLICT", "上游尚未完成固定版本验收，未派发依赖任务")
    run.native["dependency_bindings"] = report["bindings"]


def gate(app, task, run):
    if not task.dependencies:
        return task
    try:
        report = inspect(app, task, run)
        ready = report["ready"] and bool(run and run.native.get("dependency_bindings"))
        task.dependency_state = {"ready": ready, "stale": report["stale"], "blocked_count": len(report["blocked"])}
    except (AsterunError, OSError, UnicodeError):
        ready = False
        task.dependency_state = {"ready": False, "stale": True, "blocked_count": len(task.dependencies)}
    if not ready and task.acceptance == AcceptanceStatus.PASSED:
        task.acceptance = AcceptanceStatus.PENDING
        task.evidence_input_hash = ""
    return task
