"""只读读取上下文预算观测。不建表，不写库，不改变派发。"""

from __future__ import annotations

from asterun.config_v2 import context_budget_settings
from asterun.context_budget.observe import list_observations
from asterun.errors import CONFIG_REQUIRED, INVALID_REQUEST, AsterunError
from asterun.plugin_api.protocol import json_copy
from asterun.policy import enforce, reject_self_reported_authority

# 与 quota.inspect 相同的实例级读权限。
READ_ACTION = "quota.read"
_METHODS = {"context_budget.inspect"}


def _fail(message: str, code: str = INVALID_REQUEST) -> None:
    raise AsterunError(code, message)


def _authorize(app) -> None:
    enforce(app.policy.authorize(app.principal, READ_ACTION, "*"))
    app._require_grant("*")
    if not hasattr(app.store, "_conn"):
        raise AsterunError(CONFIG_REQUIRED, "上下文预算观测需要 SQLite 状态目录")
    app._require_applied_config()


def inspect_context_budget(app, payload: dict) -> dict:
    """返回观测与当前 mode。表不存在时观测为空列表，且不建表。"""
    limit = 20 if payload.get("limit") is None else payload["limit"]
    return {
        "observations": list_observations(app.store._conn, task_id=payload.get("task_id"), limit=limit),
        "mode": context_budget_settings(app.config)["mode"],
    }


def handle(app, method: str, payload: dict | None) -> dict:
    if method not in _METHODS:
        _fail(f"未知上下文预算方法：{method}")
    payload = json_copy({} if payload is None else payload)
    reject_self_reported_authority(payload)
    _authorize(app)
    return inspect_context_budget(app, payload)
