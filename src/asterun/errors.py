from __future__ import annotations

from typing import Any


CONFIG_REQUIRED = "CONFIG_REQUIRED"
AUTH_REQUIRED = "AUTH_REQUIRED"
SCOPE_DENIED = "SCOPE_DENIED"
CAPABILITY_UNSUPPORTED = "CAPABILITY_UNSUPPORTED"
REVISION_CONFLICT = "REVISION_CONFLICT"
RATE_LIMITED = "RATE_LIMITED"
BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
REMOTE_STATE_UNKNOWN = "REMOTE_STATE_UNKNOWN"
UNKNOWN_FIELD = "UNKNOWN_FIELD"
UNKNOWN_PROJECT = "UNKNOWN_PROJECT"
PATH_OUT_OF_SCOPE = "PATH_OUT_OF_SCOPE"
PATH_INVALID = "PATH_INVALID"
INVALID_CONFIG = "INVALID_CONFIG"
INVALID_REQUEST = "INVALID_REQUEST"
NOT_FOUND = "NOT_FOUND"
RUN_ALREADY_TERMINAL = "RUN_ALREADY_TERMINAL"
IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
INSTANCE_LOCKED = "INSTANCE_LOCKED"
INTENT_PERSIST_FAILED = "INTENT_PERSIST_FAILED"
BINDING_MISMATCH = "BINDING_MISMATCH"
BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"

PUBLIC_ERROR_CODES = (
    CONFIG_REQUIRED,
    AUTH_REQUIRED,
    SCOPE_DENIED,
    CAPABILITY_UNSUPPORTED,
    REVISION_CONFLICT,
    RATE_LIMITED,
    BACKEND_UNAVAILABLE,
    REMOTE_STATE_UNKNOWN,
    UNKNOWN_FIELD,
    UNKNOWN_PROJECT,
    PATH_OUT_OF_SCOPE,
    PATH_INVALID,
    INVALID_CONFIG,
    INVALID_REQUEST,
    NOT_FOUND,
    RUN_ALREADY_TERMINAL,
    IDEMPOTENCY_CONFLICT,
    INSTANCE_LOCKED,
    INTENT_PERSIST_FAILED,
    BINDING_MISMATCH,
    BUDGET_EXHAUSTED,
)


class AsterunError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        next_action: str = "",
        details: dict[str, Any] | None = None,
        evidence_ref: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.next_action = next_action
        self.details = details or {}
        self.evidence_ref = evidence_ref

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "next_action": self.next_action,
            "details": self.details,
            "evidence_ref": self.evidence_ref,
        }
