"""只读读取上下文胶囊。不建表，不写库，不派发。"""

from __future__ import annotations

from asterun.capsule.assemble import verify_capsule
from asterun.capsule.model import render_prompt
from asterun.capsule.store import latest_with_blob, list_capsules, load_capsule
from asterun.errors import CONFIG_REQUIRED, INVALID_REQUEST, AsterunError
from asterun.ids import RunId, TaskId
from asterun.plugin_api.protocol import json_copy
from asterun.policy import enforce, reject_self_reported_authority

# 与 context_budget.inspect / quota.inspect 相同的实例级读权限。
READ_ACTION = "quota.read"
_METHODS = {"capsule.inspect"}


def _fail(message: str, code: str = INVALID_REQUEST) -> None:
    raise AsterunError(code, message)


def _authorize(app) -> None:
    enforce(app.policy.authorize(app.principal, READ_ACTION, "*"))
    app._require_grant("*")
    if not hasattr(app.store, "_conn"):
        raise AsterunError(CONFIG_REQUIRED, "上下文胶囊需要 SQLite 状态目录")
    app._require_applied_config()


def inspect_capsules(app, payload: dict) -> dict:
    """返回胶囊行。render 时另附最新一条有正文胶囊的校验和提示词。"""
    task = app.store.get_task(TaskId(payload["task_id"]))
    enforce(app.policy.authorize(app.principal, "task.get", task.workspace))
    limit = 20 if payload.get("limit") is None else payload["limit"]
    capsules = list_capsules(app.store._conn, task_id=task.id.value, limit=limit)
    result = {"capsules": capsules}
    if payload.get("render") is not True:
        return result
    chosen = latest_with_blob(app.store._conn, task.id.value)
    if chosen is None:
        result["verify_issues"] = []
        result["rendered_prompt"] = None
        return result
    capsule = load_capsule(app, chosen["blob_sha256"])
    run = app.store.get_run(RunId(chosen["run_id"]))
    kind = app.config.get_backend(run.backend.value).kind
    result["verify_issues"] = verify_capsule(app, task, capsule)
    result["rendered_prompt"] = render_prompt(capsule, backend_kind=kind)
    return result


def handle(app, method: str, payload: dict | None) -> dict:
    if method not in _METHODS:
        _fail(f"未知上下文胶囊方法：{method}")
    payload = json_copy({} if payload is None else payload)
    reject_self_reported_authority(payload)
    _authorize(app)
    if not isinstance(payload.get("task_id"), str) or not payload["task_id"]:
        _fail("需要 task_id")
    return inspect_capsules(app, payload)
