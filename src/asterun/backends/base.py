from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from asterun.config import BackendConfig
from asterun.contracts import (
    Capability,
    ConnectionDisplay,
    SupportLevel,
    VerificationStatus,
)
from asterun.errors import BACKEND_UNAVAILABLE, CAPABILITY_UNSUPPORTED, AsterunError
from asterun.ids import RunId, TaskId


def require_dispatch_cwd(cwd: Path | None) -> Path:
    from asterun.errors import INVALID_REQUEST
    from asterun.paths import resolve_allowed_root

    if cwd is None:
        raise AsterunError(INVALID_REQUEST, "原生后端派发必须提供获授权的工作区路径")
    return resolve_allowed_root(cwd)


@dataclass(slots=True)
class BackendCall:
    method: str
    payload: dict[str, Any] = field(default_factory=dict)


class Backend(Protocol):
    config: BackendConfig
    calls: list[BackendCall]

    def can_dispatch(self) -> bool: ...
    def unavailable_error(self) -> AsterunError: ...
    def inspect(self) -> dict[str, Any]: ...
    def verify_connection(self) -> dict[str, Any]: ...
    def dispatch(self, task_id: TaskId, run_id: RunId, script: str, text: str, *, cwd: Path | None = None) -> dict[str, Any]: ...
    def request_cancel(self, run_id: RunId, script: str) -> dict[str, Any]: ...
    def respond_approval(self, run_id: RunId, decision: str) -> dict[str, Any]: ...


def capability_rows(kind: str) -> list[Capability]:
    # 旧 import 路径保留；第三方能力直接来自其注册 manifest。
    from asterun.plugins.builtin_capabilities import builtin_capability_rows

    return builtin_capability_rows(kind)


class DisabledBackend:
    """已声明但不可启用的真实后端占位。inspect 可用，派发不可用。"""

    def __init__(self, config: BackendConfig) -> None:
        self.config = config
        self.calls: list[BackendCall] = []

    @property
    def dispatch_count(self) -> int:
        return sum(1 for item in self.calls if item.method == "dispatch")

    def can_dispatch(self) -> bool:
        return False

    def unavailable_error(self) -> AsterunError:
        return AsterunError(
            BACKEND_UNAVAILABLE,
            f"真实执行入口不可启用：{self.config.kind}",
            next_action="使用 kind=fake 验证契约，或等待对应适配器迁移与现场授权",
            details={"backend": self.config.name, "kind": self.config.kind, "is_real_connection": False},
        )

    def inspect(self) -> dict[str, Any]:
        self.calls.append(BackendCall("inspect"))
        return {
            "backend": self.config.name,
            "kind": self.config.kind,
            "enabled_in_config": self.config.enabled,
            "execution_enabled": False,
            "is_real_connection": False,
            "connection_display": str(ConnectionDisplay.NONE),
            "authentication": "not_verified",
            "capabilities": [item.to_dict() for item in capability_rows(self.config.kind)],
            "message": "真实后端适配器尚未迁入，执行入口保持不可启用。",
        }

    def verify_connection(self) -> dict[str, Any]:
        self.calls.append(BackendCall("verify_connection"))
        raise AsterunError(
            BACKEND_UNAVAILABLE,
            f"后端 {self.config.name}（{self.config.kind}）的真实连接验证尚未启用",
            next_action="先完成 A0-02 源码交接和 A1-03 可靠派发；在此之前只能使用 fake",
            details={"backend": self.config.name, "kind": self.config.kind, "is_real_connection": False},
        )

    def dispatch(self, task_id: TaskId, run_id: RunId, script: str, text: str, *, cwd: Path | None = None) -> dict[str, Any]:
        self.calls.append(
            BackendCall(
                "dispatch",
                {"task_id": task_id.value, "run_id": run_id.value, "script": script, "text": text},
            )
        )
        raise AsterunError(
            BACKEND_UNAVAILABLE,
            f"真实执行入口不可启用：{self.config.kind}",
            next_action="使用 kind=fake 验证契约，或等待 A1-03/A1-04",
        )

    def request_cancel(self, run_id: RunId, script: str) -> dict[str, Any]:
        self.calls.append(BackendCall("request_cancel", {"run_id": run_id.value, "script": script}))
        raise AsterunError(BACKEND_UNAVAILABLE, "真实后端取消不可用")

    def respond_approval(self, run_id: RunId, decision: str) -> dict[str, Any]:
        self.calls.append(BackendCall("respond_approval", {"run_id": run_id.value, "decision": decision}))
        raise AsterunError(CAPABILITY_UNSUPPORTED, "真实后端审批桥接尚未启用")
