from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TypedId:
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value.strip():
            raise TypeError(f"{type(self).__name__} 需要非空字符串")

    def __str__(self) -> str:
        return self.value


class AccountRef(TypedId):
    """用户账户引用，不得与后端或模型 ID 混用。"""


class BackendId(TypedId):
    """执行后端标识。"""


class ModelId(TypedId):
    """模型标识，不等于执行后端。"""


class BillingPoolRef(TypedId):
    """计费池引用。"""


class RuntimeRef(TypedId):
    """运行主机引用。"""


class TaskId(TypedId):
    pass


class RunId(TypedId):
    pass


class ConversationId(TypedId):
    pass


class BackendSessionId(TypedId):
    pass


class ApprovalId(TypedId):
    pass


class OperationId(TypedId):
    pass


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def new_task_id() -> TaskId:
    return TaskId(new_id("tsk"))


def new_run_id() -> RunId:
    return RunId(new_id("run"))


def new_approval_id() -> ApprovalId:
    return ApprovalId(new_id("apr"))


def new_operation_id() -> OperationId:
    return OperationId(new_id("op"))


def new_conversation_id() -> ConversationId:
    return ConversationId(new_id("con"))


def require_type(value: object, expected: type) -> None:
    if not isinstance(value, expected):
        actual = type(value).__name__
        raise TypeError(f"需要 {expected.__name__}，实际是 {actual}，禁止混用账户/后端/模型标识")
