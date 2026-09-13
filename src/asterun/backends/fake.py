from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from asterun.backends.base import BackendCall, capability_rows
from asterun.config import BackendConfig
from asterun.contracts import ConnectionDisplay, RunStatus
from asterun.errors import BACKEND_UNAVAILABLE, CAPABILITY_UNSUPPORTED, INVALID_REQUEST, AsterunError
from asterun.ids import RunId, TaskId

FAKE_SCRIPTS = {
    "success",
    "failure",
    "events",
    "wait_input",
    "cancel_accept_only",
    "cancel_and_stop",
    "cancel_race_complete",
    "lost_reply",
    "timeout",
    "unsupported",
}


class FakeBackend:
    """可脚本化的离线模拟后端。不得显示为真实连接。"""

    def __init__(self, config: BackendConfig | None = None) -> None:
        self.config = config or BackendConfig(name="fake", kind="fake", enabled=True)
        self.calls: list[BackendCall] = []

    @property
    def dispatch_count(self) -> int:
        return sum(1 for item in self.calls if item.method == "dispatch")

    def can_dispatch(self) -> bool:
        return self.config.enabled

    def unavailable_error(self) -> AsterunError:
        return AsterunError(
            BACKEND_UNAVAILABLE,
            "fake 后端未启用",
            next_action="在配置中将 fake.enabled 设为 true",
            details={"backend": self.config.name, "kind": "fake"},
        )

    def inspect(self) -> dict[str, Any]:
        self.calls.append(BackendCall("inspect"))
        return {
            "backend": self.config.name,
            "kind": "fake",
            "enabled_in_config": self.config.enabled,
            "execution_enabled": self.config.enabled,
            "is_real_connection": False,
            "connection_display": str(ConnectionDisplay.SIMULATED),
            "authentication": "not_applicable",
            "scripts": sorted(FAKE_SCRIPTS),
            "capabilities": [item.to_dict() for item in capability_rows("fake")],
            "message": "fake 只模拟契约行为，不是真实模型或账户连接。",
        }

    def verify_connection(self) -> dict[str, Any]:
        self.calls.append(BackendCall("verify_connection"))
        return {
            "backend": self.config.name,
            "kind": "fake",
            "is_real_connection": False,
            "connection_display": str(ConnectionDisplay.SIMULATED),
            "authentication": "not_applicable",
            "verified": False,
            "simulated": True,
            "message": "connection.verify 对 fake 只返回模拟状态，不表示已登录或已接通真实后端。",
        }

    def dispatch(self, task_id: TaskId, run_id: RunId, script: str, text: str, *, cwd: Path | None = None) -> dict[str, Any]:
        self.calls.append(
            BackendCall(
                "dispatch",
                {"task_id": task_id.value, "run_id": run_id.value, "script": script, "text": text},
            )
        )
        if script not in FAKE_SCRIPTS:
            raise AsterunError(
                INVALID_REQUEST,
                f"未知 fake 脚本：{script}",
                details={"known": sorted(FAKE_SCRIPTS)},
            )
        if script == "unsupported":
            raise AsterunError(
                CAPABILITY_UNSUPPORTED,
                "fake 脚本 unsupported：该能力被显式拒绝",
                next_action="改用 success/failure/events/wait_input/cancel_* 脚本，或查看 backend inspect 的能力表",
                details={"script": script},
            )
        events = [{"type": "run_started", "text": "fake 开始模拟"}]
        if script == "events":
            events.extend(
                [
                    {"type": "message", "text": "第一段公开事件"},
                    {"type": "message", "text": "第二段公开事件"},
                ]
            )
        if script == "success":
            return {
                "status": str(RunStatus.SUCCEEDED),
                "terminated": True,
                "events": [*events, {"type": "run_succeeded", "text": text or "fake 成功"}],
                "summary": text or "fake 成功",
            }
        if script == "failure":
            return {
                "status": str(RunStatus.FAILED),
                "terminated": True,
                "error_code": "FAKE_SCRIPT_FAILURE",
                "events": [*events, {"type": "run_failed", "text": text or "fake 失败"}],
                "summary": "fake 脚本 failure",
            }
        if script == "events":
            return {
                "status": str(RunStatus.SUCCEEDED),
                "terminated": True,
                "events": [*events, {"type": "run_succeeded", "text": "事件序列结束"}],
                "summary": "fake 脚本 events",
            }
        if script == "wait_input":
            return {
                "status": str(RunStatus.WAITING_INPUT),
                "terminated": False,
                "needs_approval": True,
                "events": [
                    *events,
                    {"type": "waiting_input", "text": "等待输入或审批"},
                    {"type": "approval_requested", "text": "需要一次匹配的审批答复"},
                ],
                "summary": "fake 等待输入",
            }
        if script in {"lost_reply", "timeout"}:
            return {
                "status": str(RunStatus.PENDING_RECONCILE),
                "terminated": False,
                "error_code": "REMOTE_STATE_UNKNOWN",
                "events": [*events, {"type": "message", "text": f"脚本 {script}：结果未知，未重派"}],
                "summary": "已发出请求但回复丢失或超时，进入待对账",
            }
        return {
            "status": str(RunStatus.RUNNING),
            "terminated": False,
            "events": [*events, {"type": "message", "text": f"脚本 {script} 保持运行，等待取消"}],
            "summary": f"fake 脚本 {script} 保持运行",
        }

    def request_cancel(self, run_id: RunId, script: str) -> dict[str, Any]:
        self.calls.append(BackendCall("request_cancel", {"run_id": run_id.value, "script": script}))
        accepted = {
            "cancel_accepted": True,
            "run_id": run_id.value,
            "message": "取消请求已受理，还不等于运行已终止",
        }
        if script == "cancel_and_stop":
            return {
                **accepted,
                "terminated": True,
                "status": str(RunStatus.CANCELLED),
                "events": [{"type": "run_terminated", "text": "fake 已停止运行"}],
            }
        if script == "cancel_race_complete":
            return {
                **accepted,
                "terminated": True,
                "status": str(RunStatus.SUCCEEDED),
                "raced_completion": True,
                "events": [{"type": "run_succeeded", "text": "完成后取消未改写为已取消"}],
            }
        return {
            **accepted,
            "terminated": False,
            "status": str(RunStatus.RUNNING),
            "events": [],
        }

    def dispatch_review(self, task_id, run_id, script, text, *, cwd=None):
        """专用模拟审查；不把提示词回显当成 reviewer 的证据。"""
        self.calls.append(BackendCall("dispatch_review", {"run_id": run_id.value}))
        context = json.loads(text.split("\n", 1)[1])
        return {"status": "succeeded", "terminated": True,
                "summary": json.dumps({"target_hash": context["target"]["hash"], "findings": []}),
                "native": {"simulated": True}}

    def respond_approval(self, run_id: RunId, decision: str) -> dict[str, Any]:
        self.calls.append(BackendCall("respond_approval", {"run_id": run_id.value, "decision": decision}))
        if decision == "approve":
            return {
                "status": str(RunStatus.SUCCEEDED),
                "terminated": True,
                "events": [
                    {"type": "approval_responded", "text": "approve"},
                    {"type": "run_succeeded", "text": "审批通过后继续"},
                ],
                "summary": "审批通过",
            }
        return {
            "status": str(RunStatus.FAILED),
            "terminated": True,
            "error_code": "APPROVAL_DENIED",
            "events": [
                {"type": "approval_responded", "text": "deny"},
                {"type": "run_failed", "text": "审批被拒绝"},
            ],
            "summary": "审批拒绝",
        }
