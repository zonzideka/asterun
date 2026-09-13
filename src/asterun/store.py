from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from asterun.contracts import ApprovalRequest, Event, Operation, OperationStatus, Run, SessionBinding, Task
from asterun.errors import (
    IDEMPOTENCY_CONFLICT,
    INTENT_PERSIST_FAILED,
    NOT_FOUND,
    REVISION_CONFLICT,
    AsterunError,
)
from asterun.ids import ApprovalId, ConversationId, OperationId, RunId, TaskId


class Store(Protocol):
    def save_task(self, task: Task) -> None: ...
    def get_task(self, task_id: TaskId) -> Task: ...
    def save_run(self, run: Run) -> None: ...
    def get_run(self, run_id: RunId) -> Run: ...
    def save_approval(self, approval: ApprovalRequest) -> None: ...
    def get_approval(self, approval_id: ApprovalId) -> ApprovalRequest: ...
    def find_pending_approval(self, run_id: RunId) -> ApprovalRequest | None: ...
    def append_event(self, run_id: RunId, event: Event) -> Event: ...
    def list_events(self, run_id: RunId, cursor: int = 0, page_size: int = 100) -> list[Event]: ...
    def commit_intent(self, operation: Operation, task: Task, run: Run, *, session: SessionBinding | None = None) -> tuple[Operation, Task, Run, bool]: ...
    def save_operation(self, operation: Operation) -> None: ...
    def get_operation(self, operation_id: OperationId) -> Operation: ...
    def get_applied_revision(self) -> int | None: ...
    def bootstrap_applied_revision(self, revision: int) -> int: ...
    def cas_apply_revision(self, expected: int, new_revision: int) -> int: ...
    def save_session(self, session: SessionBinding) -> None: ...
    def get_session(self, conversation_id: ConversationId) -> SessionBinding: ...
    def find_session_by_native(self, backend: str, backend_session_id: str) -> SessionBinding | None: ...
    def list_task_ids(self) -> list[TaskId]: ...
    def find_operation_for_run(self, run_id: RunId) -> Operation | None: ...
    def find_idempotent_operation(self, operation: Operation) -> Operation | None: ...


class MemoryStore:
    def __init__(self) -> None:
        self.tasks: dict[str, Task] = {}
        self.runs: dict[str, Run] = {}
        self.approvals: dict[str, ApprovalRequest] = {}
        self.events: dict[str, list[Event]] = {}
        self.operations: dict[str, Operation] = {}
        self.sessions: dict[str, SessionBinding] = {}
        self._ops_by_key: dict[tuple[str, str, str, str], Operation] = {}
        self.applied_config_revision: int | None = None
        self.fail_next_intent = False

    def save_task(self, task: Task) -> None:
        self.tasks[task.id.value] = task

    def get_task(self, task_id: TaskId) -> Task:
        try:
            return self.tasks[task_id.value]
        except KeyError as exc:
            raise AsterunError(NOT_FOUND, f"找不到任务 {task_id}", details={"task_id": task_id.value}) from exc

    def save_run(self, run: Run) -> None:
        self.runs[run.id.value] = run

    def get_run(self, run_id: RunId) -> Run:
        try:
            return self.runs[run_id.value]
        except KeyError as exc:
            raise AsterunError(NOT_FOUND, f"找不到运行 {run_id}", details={"run_id": run_id.value}) from exc

    def save_approval(self, approval: ApprovalRequest) -> None:
        self.approvals[approval.id.value] = approval

    def get_approval(self, approval_id: ApprovalId) -> ApprovalRequest:
        try:
            return self.approvals[approval_id.value]
        except KeyError as exc:
            raise AsterunError(
                NOT_FOUND,
                f"找不到审批 {approval_id}",
                details={"approval_id": approval_id.value},
            ) from exc

    def find_pending_approval(self, run_id: RunId) -> ApprovalRequest | None:
        pending = [
            item
            for item in self.approvals.values()
            if item.run_id.value == run_id.value and item.state.value == "pending"
        ]
        return pending[0] if pending else None

    def append_event(self, run_id: RunId, event: Event) -> Event:
        bucket = self.events.setdefault(run_id.value, [])
        event.seq = len(bucket) + 1
        bucket.append(event)
        return event

    def list_events(self, run_id: RunId, cursor: int = 0, page_size: int = 100) -> list[Event]:
        bucket = self.events.get(run_id.value, [])
        selected = [item for item in bucket if item.seq > cursor]
        return selected[:page_size]

    def _idempotency_key(self, operation: Operation) -> tuple[str, str, str, str]:
        return (operation.principal, operation.action, operation.target, operation.idempotency_key)

    def commit_intent(self, operation: Operation, task: Task, run: Run, *, session: SessionBinding | None = None) -> tuple[Operation, Task, Run, bool]:
        if self.fail_next_intent:
            self.fail_next_intent = False
            raise AsterunError(
                INTENT_PERSIST_FAILED,
                "操作意图写盘失败，未派发后端",
                next_action="检查状态目录权限后重试；本次没有后端调用",
            )
        key = self._idempotency_key(operation)
        existing = self._ops_by_key.get(key)
        if existing is not None:
            if existing.input_hash != operation.input_hash:
                raise AsterunError(
                    IDEMPOTENCY_CONFLICT,
                    "同一幂等键对应不同输入",
                    next_action="更换幂等键，或使用与首次提交相同的输入",
                    details={"idempotency_key": operation.idempotency_key},
                )
            if existing.task_id is None or existing.run_id is None:
                raise AsterunError(NOT_FOUND, "已有意图缺少任务引用")
            return existing, self.get_task(existing.task_id), self.get_run(existing.run_id), True
        operation.task_id = task.id
        operation.run_id = run.id
        operation.status = OperationStatus.INTENDED
        self.operations[operation.id.value] = operation
        self._ops_by_key[key] = operation
        self.tasks[task.id.value] = task
        self.runs[run.id.value] = run
        if session is not None:
            self.sessions[session.conversation_id.value] = session
        return operation, task, run, False

    def save_operation(self, operation: Operation) -> None:
        self.operations[operation.id.value] = operation
        self._ops_by_key[self._idempotency_key(operation)] = operation

    def get_operation(self, operation_id: OperationId) -> Operation:
        try:
            return self.operations[operation_id.value]
        except KeyError as exc:
            raise AsterunError(
                NOT_FOUND,
                f"找不到操作 {operation_id}",
                details={"operation_id": operation_id.value},
            ) from exc

    def get_applied_revision(self) -> int | None:
        return self.applied_config_revision

    def bootstrap_applied_revision(self, revision: int) -> int:
        if self.applied_config_revision is None:
            self.applied_config_revision = revision
        return self.applied_config_revision

    def cas_apply_revision(self, expected: int, new_revision: int) -> int:
        current = self.applied_config_revision
        if current is None:
            raise AsterunError(REVISION_CONFLICT, "状态库还没有已应用的配置 revision")
        if current != expected:
            raise AsterunError(
                REVISION_CONFLICT,
                f"配置 revision 不匹配：期望已应用 {expected}，实际 {current}",
                next_action="重新读取 config.validate 后再 apply，不要覆盖别人刚写入的 revision",
                details={"expected": expected, "applied": current, "requested": new_revision},
            )
        self.applied_config_revision = new_revision
        return new_revision

    def save_session(self, session: SessionBinding) -> None:
        self.sessions[session.conversation_id.value] = session

    def get_session(self, conversation_id: ConversationId) -> SessionBinding:
        try:
            return self.sessions[conversation_id.value]
        except KeyError as exc:
            raise AsterunError(
                NOT_FOUND,
                f"找不到会话 {conversation_id}",
                details={"conversation_id": conversation_id.value},
            ) from exc

    def find_session_by_native(self, backend: str, backend_session_id: str) -> SessionBinding | None:
        for item in self.sessions.values():
            native = None if item.backend_session_id is None else item.backend_session_id.value
            if item.backend.value == backend and native == backend_session_id:
                return item
        return None

    def list_task_ids(self) -> list[TaskId]:
        return [TaskId(key) for key in self.tasks]

    def find_operation_for_run(self, run_id: RunId) -> Operation | None:
        for item in self.operations.values():
            if item.run_id is not None and item.run_id.value == run_id.value:
                return item
        return None

    def find_idempotent_operation(self, operation: Operation) -> Operation | None:
        return self._ops_by_key.get(self._idempotency_key(operation))


class JsonFileStore(MemoryStore):
    """A1-02 临时文件存储，不提供 A1-03 的事务、幂等或单实例保证。"""

    def __init__(self, directory: Path) -> None:
        super().__init__()
        self.directory = directory
        self.path = directory / "store.json"
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.tasks = {key: Task.from_dict(value) for key, value in raw.get("tasks", {}).items()}
        self.runs = {key: Run.from_dict(value) for key, value in raw.get("runs", {}).items()}
        self.approvals = {
            key: ApprovalRequest.from_dict(value) for key, value in raw.get("approvals", {}).items()
        }
        self.events = {
            key: [Event.from_dict(item) for item in items] for key, items in raw.get("events", {}).items()
        }
        self.operations = {
            key: Operation.from_dict(value) for key, value in raw.get("operations", {}).items()
        }
        self.sessions = {
            key: SessionBinding.from_dict(value) for key, value in raw.get("sessions", {}).items()
        }
        applied = raw.get("applied_config_revision")
        self.applied_config_revision = None if applied is None else int(applied)
        self._ops_by_key = {self._idempotency_key(item): item for item in self.operations.values()}

    def _dump(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "tasks": {key: value.to_dict() for key, value in self.tasks.items()},
            "runs": {key: value.to_dict() for key, value in self.runs.items()},
            "approvals": {key: value.to_dict() for key, value in self.approvals.items()},
            "events": {
                key: [item.to_dict() for item in items] for key, items in self.events.items()
            },
            "operations": {key: value.to_dict() for key, value in self.operations.items()},
            "sessions": {key: value.to_dict() for key, value in self.sessions.items()},
            "applied_config_revision": self.applied_config_revision,
        }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def save_task(self, task: Task) -> None:
        self._load()
        super().save_task(task)
        self._dump()

    def get_task(self, task_id: TaskId) -> Task:
        self._load()
        return super().get_task(task_id)

    def save_run(self, run: Run) -> None:
        self._load()
        super().save_run(run)
        self._dump()

    def get_run(self, run_id: RunId) -> Run:
        self._load()
        return super().get_run(run_id)

    def save_approval(self, approval: ApprovalRequest) -> None:
        self._load()
        super().save_approval(approval)
        self._dump()

    def get_approval(self, approval_id: ApprovalId) -> ApprovalRequest:
        self._load()
        return super().get_approval(approval_id)

    def find_pending_approval(self, run_id: RunId) -> ApprovalRequest | None:
        self._load()
        return super().find_pending_approval(run_id)

    def append_event(self, run_id: RunId, event: Event) -> Event:
        self._load()
        result = super().append_event(run_id, event)
        self._dump()
        return result

    def list_events(self, run_id: RunId, cursor: int = 0, page_size: int = 100) -> list[Event]:
        self._load()
        return super().list_events(run_id, cursor, page_size)

    def commit_intent(self, operation: Operation, task: Task, run: Run, *, session: SessionBinding | None = None) -> tuple[Operation, Task, Run, bool]:
        self._load()
        result = super().commit_intent(operation, task, run, session=session)
        self._dump()
        return result

    def save_operation(self, operation: Operation) -> None:
        self._load()
        super().save_operation(operation)
        self._dump()

    def get_operation(self, operation_id: OperationId) -> Operation:
        self._load()
        return super().get_operation(operation_id)

    def get_applied_revision(self) -> int | None:
        self._load()
        return super().get_applied_revision()

    def bootstrap_applied_revision(self, revision: int) -> int:
        self._load()
        result = super().bootstrap_applied_revision(revision)
        self._dump()
        return result

    def cas_apply_revision(self, expected: int, new_revision: int) -> int:
        self._load()
        result = super().cas_apply_revision(expected, new_revision)
        self._dump()
        return result

    def save_session(self, session: SessionBinding) -> None:
        self._load()
        super().save_session(session)
        self._dump()

    def get_session(self, conversation_id: ConversationId) -> SessionBinding:
        self._load()
        return super().get_session(conversation_id)

    def find_session_by_native(self, backend: str, backend_session_id: str) -> SessionBinding | None:
        self._load()
        return super().find_session_by_native(backend, backend_session_id)

    def list_task_ids(self) -> list[TaskId]:
        self._load()
        return super().list_task_ids()

    def find_operation_for_run(self, run_id: RunId) -> Operation | None:
        self._load()
        return super().find_operation_for_run(run_id)

    def find_idempotent_operation(self, operation: Operation) -> Operation | None:
        self._load()
        return super().find_idempotent_operation(operation)
