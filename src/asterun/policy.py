from __future__ import annotations

import getpass
from datetime import datetime
from dataclasses import dataclass
from typing import Any, Protocol

from asterun.errors import AUTH_REQUIRED, SCOPE_DENIED, AsterunError

FORBIDDEN_REQUEST_KEYS = {
    "principal",
    "granted",
    "set_ready",
    "set_granted",
    "authorization",
    "actor",
    "account_ref",
    "runtime_ref",
    "billing_pool_ref",
    "auth",
}


@dataclass(frozen=True, slots=True)
class Principal:
    subject_id: str
    source: str


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    code: str | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class Grant:
    expires_at: str | None = None
    resources: tuple[str, ...] | None = None
    revision: int = 1

    def is_expired(self, now: str) -> bool:
        if self.expires_at is None:
            return False
        try:
            expiry = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
            instant = datetime.fromisoformat(now.replace("Z", "+00:00"))
            return expiry.tzinfo is None or instant >= expiry
        except (TypeError, ValueError, AttributeError):
            return True

    def covers(self, resource: str) -> bool:
        if self.resources is None or "*" in self.resources:
            return True
        return resource in self.resources

    def to_dict(self) -> dict[str, Any]:
        return {
            "expires_at": self.expires_at,
            "resources": None if self.resources is None else list(self.resources),
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Grant:
        resources = data.get("resources")
        return cls(
            expires_at=data.get("expires_at"),
            resources=None if resources is None else tuple(str(item) for item in resources),
            revision=int(data.get("revision") or 1),
        )


def enforce_grant(grant: Grant | None, resource: str, now: str) -> None:
    if grant is None:
        return
    if grant.is_expired(now):
        raise AsterunError(
            AUTH_REQUIRED,
            "授权已过期",
            next_action="重新取得有效授权后再提交或续接；过期授权不能靠请求字段续期",
            details={"grant": "expired", "expires_at": grant.expires_at},
        )
    if not grant.covers(resource):
        raise AsterunError(
            SCOPE_DENIED,
            f"当前授权不覆盖资源 {resource}",
            next_action="使用授权范围内的工作区，不要在请求里自报扩权",
            details={"resource": resource},
        )


class Policy(Protocol):
    def authorize(self, principal: Principal, action: str, resource: str) -> Decision: ...


class AllowConfiguredWorkspaces:
    def __init__(self, workspace_aliases: set[str]) -> None:
        self.workspace_aliases = set(workspace_aliases)

    def authorize(self, principal: Principal, action: str, resource: str) -> Decision:
        if principal.source not in {"process", "test"}:
            return Decision(False, SCOPE_DENIED, "只接受进程或测试夹具提供的主体")
        if resource == "*":
            return Decision(True)
        if resource not in self.workspace_aliases:
            return Decision(False, SCOPE_DENIED, f"主体无权使用资源 {resource}")
        del action
        return Decision(True)


class DenyAll:
    def authorize(self, principal: Principal, action: str, resource: str) -> Decision:
        del principal, action, resource
        return Decision(False, SCOPE_DENIED, "当前策略拒绝该动作，调用不得到达后端")


def process_principal() -> Principal:
    return Principal(subject_id=f"os:{getpass.getuser()}", source="process")


def reject_self_reported_authority(payload: dict[str, Any] | None) -> None:
    if not payload:
        return
    hit = sorted(FORBIDDEN_REQUEST_KEYS.intersection(payload))
    if hit:
        raise AsterunError(
            SCOPE_DENIED,
            f"请求中的自报授权/主体字段不被采信：{', '.join(hit)}",
            next_action="删除 principal、granted、set_ready、set_granted 等字段；主体只由本机进程或测试夹具提供",
            details={"rejected_fields": hit},
        )


def enforce(decision: Decision) -> None:
    if decision.allowed:
        return
    raise AsterunError(
        decision.code or SCOPE_DENIED,
        decision.reason or "未获权",
        next_action="使用已配置工作区，并由进程身份调用；不要在 JSON 里自报授权通过",
    )
