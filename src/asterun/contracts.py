from __future__ import annotations

from dataclasses import asdict, dataclass, field
from copy import deepcopy
from enum import StrEnum
from typing import Any

from asterun.ids import (
    AccountRef,
    ApprovalId,
    BackendId,
    BackendSessionId,
    BillingPoolRef,
    ConversationId,
    ModelId,
    OperationId,
    RunId,
    RuntimeRef,
    TaskId,
    require_type,
)


class SupportLevel(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class VerificationStatus(StrEnum):
    NOT_TESTED = "not_tested"
    OFFLINE_VERIFIED = "offline_verified"
    LIVE_VERIFIED = "live_verified"
    EXPIRED = "expired"


class ConnectionDisplay(StrEnum):
    SIMULATED = "simulated"
    REAL = "real"
    NONE = "none"


class RunStatus(StrEnum):
    QUEUED = "queued"
    DISPATCHING = "dispatching"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    WAITING_INPUT = "waiting_input"
    WAITING_AUTH = "waiting_auth"
    PENDING_RECONCILE = "pending_reconcile"
    PAUSED = "paused"


TERMINAL_RUN_STATUSES = {
    RunStatus.SUCCEEDED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
}


class AcceptanceStatus(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    NOT_CONFIGURED = "not_configured"


class DeliveryStatus(StrEnum):
    NOT_CONFIGURED = "not_configured"
    PENDING = "pending"
    DELIVERED = "delivered"


class EventType(StrEnum):
    TASK_CREATED = "task_created"
    RUN_QUEUED = "run_queued"
    RUN_DISPATCHING = "run_dispatching"
    RUN_STARTED = "run_started"
    MESSAGE = "message"
    WAITING_INPUT = "waiting_input"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESPONDED = "approval_responded"
    APPROVAL_ASSESSED = "approval_assessed"
    APPROVAL_FORWARDING = "approval_forwarding"
    CANCEL_REQUESTED = "cancel_requested"
    RUN_TERMINATED = "run_terminated"
    RUN_SUCCEEDED = "run_succeeded"
    RUN_FAILED = "run_failed"
    CAPABILITY_REJECTED = "capability_rejected"
    RECONCILED = "reconciled"
    HANDOFF_PAUSED = "handoff_paused"
    HANDOFF_RESUMED = "handoff_resumed"
    EVIDENCE_INVALIDATED = "evidence_invalidated"
    ACCEPTANCE_EVALUATED = "acceptance_evaluated"
    REVIEW_UNAVAILABLE = "review_unavailable"
    REPAIR_LIMIT = "repair_limit"
    QUEUE_ACCEPTED = "queue_accepted"


class ApprovalState(StrEnum):
    PENDING = "pending"
    FORWARDING = "forwarding"
    INVALIDATED = "invalidated"
    APPROVED = "approved"
    DENIED = "denied"


class OperationStatus(StrEnum):
    INTENDED = "intended"
    DISPATCHED = "dispatched"
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class Capability:
    name: str
    declared_support: SupportLevel
    adapter_support: SupportLevel
    verification_status: VerificationStatus
    verification_environment: str | None = None
    verified_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Event:
    seq: int
    type: EventType
    source: str
    timestamp: str
    schema_version: int
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "type": str(self.type),
            "source": self.source,
            "timestamp": self.timestamp,
            "schema_version": self.schema_version,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Event:
        return cls(
            seq=int(data["seq"]),
            type=EventType(data["type"]),
            source=str(data["source"]),
            timestamp=str(data["timestamp"]),
            schema_version=int(data["schema_version"]),
            payload=dict(data.get("payload") or {}),
        )


@dataclass(slots=True)
class Task:
    id: TaskId
    workspace: str
    backend: BackendId
    text: str
    script: str
    acceptance: AcceptanceStatus
    delivery: DeliveryStatus
    created_at: str
    current_run_id: RunId | None = None
    conversation_id: ConversationId | None = None
    account_ref: AccountRef | None = None
    model_id: ModelId | None = None
    billing_pool_ref: BillingPoolRef | None = None
    runtime_ref: RuntimeRef | None = None
    config_revision: int = 1
    paused: bool = False
    orchestration_owner: str = "core"
    evidence_stale: bool = False
    input_hash: str = ""
    evidence_input_hash: str = ""
    findings: list[dict[str, Any]] = field(default_factory=list)
    repair_count: int = 0
    review_status: str = "not_configured"
    run_count: int = 1
    workspace_root: str = ""
    quality: dict[str, Any] = field(default_factory=dict)
    capability: str | None = None
    capability_input: dict[str, Any] = field(default_factory=dict)
    read_scope: dict[str, str] | None = None
    external_evaluation: dict[str, Any] = field(default_factory=dict)
    external_require_review: bool = False

    def __post_init__(self) -> None:
        require_type(self.id, TaskId)
        require_type(self.backend, BackendId)
        if self.current_run_id is not None:
            require_type(self.current_run_id, RunId)
        if self.conversation_id is not None:
            require_type(self.conversation_id, ConversationId)
        if self.account_ref is not None:
            require_type(self.account_ref, AccountRef)
        if self.model_id is not None:
            require_type(self.model_id, ModelId)
        if self.billing_pool_ref is not None:
            require_type(self.billing_pool_ref, BillingPoolRef)
        if self.runtime_ref is not None:
            require_type(self.runtime_ref, RuntimeRef)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id.value,
            "workspace": self.workspace,
            "backend": self.backend.value,
            "text": self.text,
            "script": self.script,
            "acceptance": str(self.acceptance),
            "delivery": str(self.delivery),
            "created_at": self.created_at,
            "current_run_id": None if self.current_run_id is None else self.current_run_id.value,
            "conversation_id": None if self.conversation_id is None else self.conversation_id.value,
            "account_ref": None if self.account_ref is None else self.account_ref.value,
            "model_id": None if self.model_id is None else self.model_id.value,
            "billing_pool_ref": None if self.billing_pool_ref is None else self.billing_pool_ref.value,
            "runtime_ref": None if self.runtime_ref is None else self.runtime_ref.value,
            "config_revision": self.config_revision,
            "paused": self.paused,
            "orchestration_owner": self.orchestration_owner,
            "evidence_stale": self.evidence_stale,
            "input_hash": self.input_hash,
            "evidence_input_hash": self.evidence_input_hash,
            "findings": list(self.findings),
            "repair_count": self.repair_count,
            "review_status": self.review_status,
            "run_count": self.run_count,
            "workspace_root": self.workspace_root,
            "quality": self.quality,
            **({"capability": self.capability, "capability_input": deepcopy(self.capability_input)} if self.capability else {}),
            **({"read_scope": dict(self.read_scope)} if self.read_scope is not None else {}),
            **({"external_evaluation": deepcopy(self.external_evaluation)} if self.external_evaluation else {}),
            **({"external_require_review": True} if self.external_require_review else {}),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        return cls(
            id=TaskId(data["id"]),
            workspace=str(data["workspace"]),
            backend=BackendId(data["backend"]),
            text=str(data.get("text") or ""),
            script=str(data.get("script") or "success"),
            acceptance=AcceptanceStatus(data["acceptance"]),
            delivery=DeliveryStatus(data["delivery"]),
            created_at=str(data["created_at"]),
            current_run_id=None if not data.get("current_run_id") else RunId(data["current_run_id"]),
            conversation_id=None if not data.get("conversation_id") else ConversationId(data["conversation_id"]),
            account_ref=None if not data.get("account_ref") else AccountRef(data["account_ref"]),
            model_id=None if not data.get("model_id") else ModelId(data["model_id"]),
            billing_pool_ref=None
            if not data.get("billing_pool_ref")
            else BillingPoolRef(data["billing_pool_ref"]),
            runtime_ref=None if not data.get("runtime_ref") else RuntimeRef(data["runtime_ref"]),
            config_revision=int(data.get("config_revision") or 1),
            paused=bool(data.get("paused")),
            orchestration_owner=str(data.get("orchestration_owner") or "core"),
            evidence_stale=bool(data.get("evidence_stale")),
            input_hash=str(data.get("input_hash") or ""),
            evidence_input_hash=str(data.get("evidence_input_hash") or ""),
            findings=list(data.get("findings") or []),
            repair_count=int(data.get("repair_count") or 0),
            review_status=str(data.get("review_status") or "not_configured"),
            run_count=int(data.get("run_count") or 1),
            workspace_root=str(data.get("workspace_root") or ""),
            quality=dict(data.get("quality") or {}),
            capability=data.get("capability"),
            capability_input=dict(data.get("capability_input") or {}),
            read_scope=None if data.get("read_scope") is None else dict(data["read_scope"]),
            external_evaluation=deepcopy(data.get("external_evaluation") or {}),
            external_require_review=bool(data.get("external_require_review", False)),
        )


@dataclass(slots=True)
class Run:
    id: RunId
    task_id: TaskId
    backend: BackendId
    status: RunStatus
    created_at: str
    cancel_requested: bool = False
    terminated: bool = False
    error_code: str | None = None
    summary: str = ""
    native: dict[str, Any] = field(default_factory=dict)
    role: str = "implementation"
    prompt: str = ""
    conversation_id: ConversationId | None = None
    target_hash: str = ""
    approval_attempts: list[str] = field(default_factory=list)
    output: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        require_type(self.id, RunId)
        require_type(self.task_id, TaskId)
        require_type(self.backend, BackendId)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id.value,
            "task_id": self.task_id.value,
            "backend": self.backend.value,
            "status": str(self.status),
            "created_at": self.created_at,
            "cancel_requested": self.cancel_requested,
            "terminated": self.terminated,
            "error_code": self.error_code,
            "summary": self.summary,
            "native": self.native,
            "role": self.role,
            "prompt": self.prompt,
            "conversation_id": None if self.conversation_id is None else self.conversation_id.value,
            "target_hash": self.target_hash,
            "approval_attempts": self.approval_attempts,
            **({"output": deepcopy(self.output)} if self.output is not None else {}),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Run:
        return cls(
            id=RunId(data["id"]),
            task_id=TaskId(data["task_id"]),
            backend=BackendId(data["backend"]),
            status=RunStatus(data["status"]),
            created_at=str(data["created_at"]),
            cancel_requested=bool(data.get("cancel_requested")),
            terminated=bool(data.get("terminated")),
            error_code=data.get("error_code"),
            summary=str(data.get("summary") or ""),
            native=dict(data.get("native") or {}),
            role=str(data.get("role") or "implementation"),
            prompt=str(data.get("prompt") or ""),
            conversation_id=ConversationId(data["conversation_id"]) if data.get("conversation_id") else None,
            target_hash=str(data.get("target_hash") or ""),
            approval_attempts=list(data.get("approval_attempts") or []),
            output=data.get("output"),
        )


@dataclass(slots=True)
class SessionBinding:
    conversation_id: ConversationId
    backend: BackendId
    workspace: str
    account_ref: AccountRef
    runtime_ref: RuntimeRef
    created_at: str
    updated_at: str
    backend_session_id: BackendSessionId | None = None
    latest_turn_id: str | None = None
    task_id: TaskId | None = None
    config_revision: int = 1

    def __post_init__(self) -> None:
        require_type(self.conversation_id, ConversationId)
        require_type(self.backend, BackendId)
        require_type(self.account_ref, AccountRef)
        require_type(self.runtime_ref, RuntimeRef)
        if self.backend_session_id is not None:
            require_type(self.backend_session_id, BackendSessionId)
        if self.task_id is not None:
            require_type(self.task_id, TaskId)

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id.value,
            "backend": self.backend.value,
            "workspace": self.workspace,
            "account_ref": self.account_ref.value,
            "runtime_ref": self.runtime_ref.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "backend_session_id": None if self.backend_session_id is None else self.backend_session_id.value,
            "latest_turn_id": self.latest_turn_id,
            "task_id": None if self.task_id is None else self.task_id.value,
            "config_revision": self.config_revision,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionBinding:
        return cls(
            conversation_id=ConversationId(data["conversation_id"]),
            backend=BackendId(data["backend"]),
            workspace=str(data["workspace"]),
            account_ref=AccountRef(data["account_ref"]),
            runtime_ref=RuntimeRef(data["runtime_ref"]),
            created_at=str(data["created_at"]),
            updated_at=str(data["updated_at"]),
            backend_session_id=None
            if not data.get("backend_session_id")
            else BackendSessionId(data["backend_session_id"]),
            latest_turn_id=data.get("latest_turn_id"),
            task_id=None if not data.get("task_id") else TaskId(data["task_id"]),
            config_revision=int(data.get("config_revision") or 1),
        )


@dataclass(slots=True)
class ApprovalRequest:
    id: ApprovalId
    task_id: TaskId
    run_id: RunId
    summary: str
    state: ApprovalState
    created_at: str
    expires_at: str | None = None
    target_hash: str = ""
    native_request: dict[str, Any] = field(default_factory=dict)
    decision_source: str | None = None
    assessment: dict[str, Any] = field(default_factory=dict)
    response_status: str = "not_sent"
    native_confirmed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id.value,
            "task_id": self.task_id.value,
            "run_id": self.run_id.value,
            "summary": self.summary,
            "state": str(self.state),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "target_hash": self.target_hash,
            "native_request": self.native_request,
            "decision_source": self.decision_source,
            "assessment": self.assessment,
            "response_status": self.response_status,
            "native_confirmed": self.native_confirmed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApprovalRequest:
        return cls(
            id=ApprovalId(data["id"]),
            task_id=TaskId(data["task_id"]),
            run_id=RunId(data["run_id"]),
            summary=str(data["summary"]),
            state=ApprovalState(data["state"]),
            created_at=str(data["created_at"]),
            expires_at=data.get("expires_at"),
            target_hash=str(data.get("target_hash") or ""),
            native_request=dict(data.get("native_request") or {}),
            decision_source=data.get("decision_source"),
            assessment=dict(data.get("assessment") or {}),
            response_status=str(data.get("response_status", "not_sent")),
            native_confirmed=data.get("native_confirmed") is True,
        )


@dataclass(slots=True)
class Operation:
    id: OperationId
    idempotency_key: str
    principal: str
    action: str
    target: str
    input_hash: str
    status: OperationStatus
    created_at: str
    task_id: TaskId | None = None
    run_id: RunId | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id.value,
            "idempotency_key": self.idempotency_key,
            "principal": self.principal,
            "action": self.action,
            "target": self.target,
            "input_hash": self.input_hash,
            "status": str(self.status),
            "created_at": self.created_at,
            "task_id": None if self.task_id is None else self.task_id.value,
            "run_id": None if self.run_id is None else self.run_id.value,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Operation:
        return cls(
            id=OperationId(data["id"]),
            idempotency_key=str(data["idempotency_key"]),
            principal=str(data["principal"]),
            action=str(data["action"]),
            target=str(data["target"]),
            input_hash=str(data["input_hash"]),
            status=OperationStatus(data["status"]),
            created_at=str(data["created_at"]),
            task_id=None if not data.get("task_id") else TaskId(data["task_id"]),
            run_id=None if not data.get("run_id") else RunId(data["run_id"]),
        )


@dataclass(slots=True)
class Envelope:
    ok: bool
    data: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    ids: dict[str, str] = field(default_factory=dict)
    evidence_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "data": self.data,
            "error": self.error,
            "ids": self.ids,
            "evidence_ref": self.evidence_ref,
        }
