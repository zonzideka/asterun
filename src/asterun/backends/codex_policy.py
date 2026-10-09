"""Codex 普通任务的原生审批模式。固定读取不使用这里的值。"""

from __future__ import annotations

from typing import Any

from asterun.errors import INVALID_CONFIG, AsterunError

# App Server 的 AskForApproval 里，这些模式仍会在越界时询问，且不关闭沙箱。
# 省略配置时运行时仍发送 untrusted，从而继续覆盖 ~/.codex/config.toml。
CODEX_APPROVAL_POLICIES = frozenset({"untrusted", "on-failure", "on-request"})
DEFAULT_CODEX_APPROVAL_POLICY = "untrusted"
CODEX_SANDBOX_MODE = "workspace-write"
_REJECTED = "不接受 never、granular，也不接受会关闭沙箱的 danger-full-access"


def validate_codex_approval_policy(value: Any, where: str) -> str:
    if not isinstance(value, str) or value not in CODEX_APPROVAL_POLICIES:
        raise AsterunError(
            INVALID_CONFIG,
            f"{where}.approval_policy 只接受 untrusted、on-failure 或 on-request；"
            f"省略时仍发送 untrusted。沙箱固定为 {CODEX_SANDBOX_MODE}，{_REJECTED}。",
        )
    return value


def effective_codex_approval_policy(value: Any) -> str:
    if value is None:
        return DEFAULT_CODEX_APPROVAL_POLICY
    if not isinstance(value, str) or value not in CODEX_APPROVAL_POLICIES:
        raise AsterunError(
            INVALID_CONFIG,
            f"Codex approval_policy 已不在允许范围内，拒绝派发。沙箱保持 {CODEX_SANDBOX_MODE}，{_REJECTED}。",
        )
    return value


def normal_thread_params(config: Any) -> dict[str, str]:
    return {
        "approvalPolicy": effective_codex_approval_policy(getattr(config, "approval_policy", None)),
        "sandbox": CODEX_SANDBOX_MODE,
    }
