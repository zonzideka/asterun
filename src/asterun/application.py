from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from typing import Any

from asterun.backends.base import Backend
from asterun.backends.fake import FAKE_SCRIPTS
from asterun.backup import backup_state
from asterun.binding import resolve_account_ref, resolve_runtime_ref
from asterun.config import AsterunConfig, load_config
from asterun.diagnose import build_diagnose
from asterun.contracts import (
    AcceptanceStatus,
    ApprovalRequest,
    ApprovalState,
    DeliveryStatus,
    Envelope,
    Event,
    EventType,
    Operation,
    OperationStatus,
    Run,
    RunStatus,
    SessionBinding,
    Task,
    TERMINAL_RUN_STATUSES,
)
from asterun.errors import (
    BINDING_MISMATCH,
    BUDGET_EXHAUSTED,
    CAPABILITY_UNSUPPORTED,
    CONFIG_REQUIRED,
    INVALID_REQUEST,
    NOT_FOUND,
    REMOTE_STATE_UNKNOWN,
    REVISION_CONFLICT,
    PATH_INVALID,
    RUN_ALREADY_TERMINAL,
    AsterunError,
)
from asterun.ids import (
    AccountRef,
    ApprovalId,
    BackendId,
    BackendSessionId,
    ConversationId,
    RunId,
    RuntimeRef,
    TaskId,
    new_approval_id,
    new_conversation_id,
    new_id,
    new_operation_id,
    new_run_id,
    new_task_id,
)
from asterun.instance import InstanceLock
from asterun.paths import resolve_within
from asterun.policy import (
    AllowConfiguredWorkspaces,
    Grant,
    Policy,
    Principal,
    enforce,
    enforce_grant,
    process_principal,
    reject_self_reported_authority,
)
from asterun.scheduler import Scheduler
from asterun.requests import validate_request
from asterun.store import MemoryStore, Store
from asterun.workflows import evaluate_quality
from asterun.executor import Executor
from asterun.approval_digest import request_hash
from asterun.quality import repair_prompt
from asterun.quality_runtime import QualityRuntime

ENTRY_BLOCKED_WHEN_DISABLED = {
    "capability.prepare", "capability.submit", "capability.invoke", "plugin.register", "plugin.enable", "plugin.disable",
    "control.command",
    "control.upload",
    "task.submit",
    "workflow.repair",
    "workflow.evaluate",
    "workflow.start",
    "approval.respond",
    "approval.batch",
    "approval.apply-policy",
    "task.cancel",
    "task.handoff",
    "config.apply",
    "session.resume",
    "task.reconcile",
    "task.present",
    "connection.verify",
    "state.restore",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ok(data: dict[str, Any], ids: dict[str, str] | None = None) -> Envelope:
    return Envelope(ok=True, data=data, ids=ids or {})


def fail(error: AsterunError) -> Envelope:
    return Envelope(ok=False, error=error.to_dict(), evidence_ref=error.evidence_ref, ids={})


class Application:
    def __init__(
        self,
        config: AsterunConfig,
        store: Store | None = None,
        backends: dict[str, Backend] | None = None,
        policy: Policy | None = None,
        principal: Principal | None = None,
        grant: Grant | None = None,
        account_ref: AccountRef | None = None,
        runtime_ref: RuntimeRef | None = None,
        entry: str = "core",
        state_dir: Path | None = None,
        background: bool = False,
    ) -> None:
        self.config = config
        self.store = store or MemoryStore()
        self.backends = backends or build_backends(config)
        self.policy = policy or AllowConfiguredWorkspaces(set(config.workspaces))
        self.principal = principal or process_principal()
        self.grant = grant
        self.account_ref = account_ref or resolve_account_ref()
        self.runtime_ref = runtime_ref or resolve_runtime_ref()
        self.instance_lock: InstanceLock | None = None
        self.scheduler = Scheduler(config.scheduler)
        self.entry = entry
        self.state_dir = state_dir
        self._closed = False
        self._draining = False
        self.executor = Executor() if background else None
        self._polling = False
        self.quality = QualityRuntime(self)
        self._control = None
        self.store.bootstrap_applied_revision(config.revision)
        self.admission = None
        if config.schema_version == 2:
            from asterun.connections.admission import AdmissionService
            self.admission = AdmissionService(self)
            from asterun.plugins.management import sync_runtime_latches
            sync_runtime_latches(self)
        self._restore_scheduler()
        if hasattr(self.store, "_conn"):
            self.control

    @property
    def control(self):
        if self._control is None:
            from asterun.control import ControlRuntime

            self._control = ControlRuntime(self)
        return self._control

    @classmethod
    def from_paths(
        cls,
        config_path: Path | None,
        state_dir: Path | None = None,
        *,
        policy: Policy | None = None,
        principal: Principal | None = None,
        grant: Grant | None = None,
        account_ref: AccountRef | None = None,
        runtime_ref: RuntimeRef | None = None,
        entry: str = "core",
        background: bool = False,
    ) -> Application:
        if config_path is None:
            raise AsterunError(
                CONFIG_REQUIRED,
                "没有配置文件",
                next_action="使用 --config 或 ASTERUN_CONFIG，不要依赖作者机器上的隐式路径",
            )
        from asterun.sqlite_store import SqliteStore

        config = load_config(config_path)
        store: Store
        lock = None
        store = None
        try:
            if state_dir is None:
                store = MemoryStore()
            else:
                lock = InstanceLock(state_dir / "asterun.lock")
                lock.acquire()
                store = SqliteStore(state_dir / "asterun.sqlite")
            app = cls(
                config, store=store, policy=policy, principal=principal, grant=grant,
                account_ref=account_ref, runtime_ref=runtime_ref, entry=entry, state_dir=state_dir,
                background=background,
            )
        except BaseException:
            try:
                if store is not None:
                    getattr(store, "close", lambda: None)()
            finally:
                if lock is not None:
                    lock.release()
            raise
        app.instance_lock = lock
        return app

    def close(self) -> None:
        if self._closed:
            return
        try:
            if self.executor is not None:
                self.executor.stopping.set()
                try:
                    for backend in self.backends.values():
                        getattr(backend, "close", lambda: None)()
                finally:
                    self.executor.close()
                self.executor.drain(self._record_result)
                for task_id in self.store.list_task_ids():
                    task = self.store.get_task(task_id)
                    run = self.store.get_run(task.current_run_id) if task.current_run_id else None
                    if run and run.status not in TERMINAL_RUN_STATUSES:
                        operation = self.store.find_operation_for_run(run.id)
                        if operation and operation.status != OperationStatus.INTENDED:
                            self._set_run(run, RunStatus.PENDING_RECONCILE, error_code=REMOTE_STATE_UNKNOWN)
                            operation.status = OperationStatus.UNKNOWN
                            self.store.save_operation(operation)
        finally:
            self._closed = True
            try:
                if self._control is not None:
                    self._control.close()
                getattr(self.store, "close", lambda: None)()
            finally:
                if self.instance_lock is not None:
                    self.instance_lock.release()
                    self.instance_lock = None

    def handle(self, method: str, payload: dict[str, Any] | None = None) -> Envelope:
        try:
            if self._closed:
                raise AsterunError(INVALID_REQUEST, "应用已关闭，请重新打开状态目录")
            payload = {} if payload is None else payload
            if not isinstance(payload, dict):
                raise AsterunError(INVALID_REQUEST, "请求必须是 JSON 对象")
            reject_self_reported_authority(payload)
            payload = validate_request(method, payload)
            self._require_entry(method)
            if method in {"review.status", "review.inbox", "review.ack"}:
                from asterun.review_follow import handle
                return ok(handle(self, method, payload))
            if method in {"capability.discovery", "capability.prepare", "capability.submit", "capability.get"}:
                # prepare/submit 不先 poll，准备和受理期间不启动供应商。
                return ok(self.capabilities.handle(method, payload))
            if method.startswith("control."):
                return self._handle_control(method, payload)
            if method.startswith("plugin.") or method in {"connection.setup", "usage.inspect"}:
                from asterun.plugins.management import handle
                return ok(handle(self, method, payload))
            if method == "task.submit" and str(payload.get("idempotency_key", "")).startswith("rcp_"):
                raise AsterunError(INVALID_REQUEST, "rcp_ 幂等键保留给标准控制执行器")
            if self._control is not None and method in {"task.submit", "session.resume"}:
                conversation = payload.get("conversation_id")
                native_session = payload.get("session_id")
                if conversation or native_session:
                    for known_id in self.store.list_task_ids():
                        known = self.store.get_task(known_id)
                        if not self.control.legacy_action(known_id.value):
                            continue
                        session = self._session_for_task(known)
                        if session and (conversation == session["conversation_id"] or
                                        native_session and native_session == session.get("backend_session_id")):
                            raise AsterunError(INVALID_REQUEST, "标准控制会话不能从旧入口派生未预算轮次")
            if self._control is not None and payload.get("task_id") and method in {
                "task.cancel", "task.handoff", "workflow.repair", "workflow.start", "workflow.evaluate"
            } and self.control.legacy_action(payload["task_id"]):
                raise AsterunError(INVALID_REQUEST, "该执行由 runner-control 管理，请使用标准控制命令")
            if self._control is not None and payload.get("task_id"):
                managed = self.control.legacy_action(payload["task_id"])
                if managed:
                    self.control.task(managed["task_id"], method)
            if method == "task.present":
                # 客户端展示重试不推进队列、模型轮次或质量流程。
                return self.task_present(payload)
            self.poll(advance_quality=method not in {"task.cancel", "task.handoff"})
            if method == "capability.invoke":
                return self.capability_invoke(payload)
            if method == "config.validate":
                return self.config_validate()
            if method == "config.apply":
                return self.config_apply(payload)
            if method == "backend.inspect":
                return self.backend_inspect(payload.get("backend"))
            if method == "connection.verify":
                return self.connection_verify(payload.get("backend"))
            if method == "task.submit":
                return self.task_submit(payload)
            if method == "task.get":
                return self.task_get(str(payload.get("task_id") or ""))
            if method == "task.events":
                return self.task_events(
                    str(payload.get("task_id") or ""),
                    cursor=payload.get("cursor", 0),
                    page_size=payload.get("page_size", 100),
                )
            if method == "task.cancel":
                return self.task_cancel(str(payload.get("task_id") or ""))
            if method == "approval.respond":
                return self.approval_respond(payload)
            if method == "approval.assess":
                return self.approval_assess(payload)
            if method == "approval.apply-policy":
                return self.approval_apply_policy(payload)
            if method == "approval.batch":
                return self.approval_batch(payload)
            if method == "session.resume":
                return self.session_resume(payload)
            if method == "task.handoff":
                return self.task_handoff(payload)
            if method == "task.reconcile":
                return self.task_reconcile(payload)
            if method == "workflow.evaluate":
                return self.workflow_evaluate(payload)
            if method == "workflow.start":
                if self._require_task(payload["task_id"]).capability:
                    raise AsterunError(CAPABILITY_UNSUPPORTED, "结构化工具任务尚不支持代码质量工作流")
                return self.quality.start(payload)
            if method == "workflow.repair":
                return self.workflow_repair(payload)
            if method == "scheduler.status":
                return self.scheduler_status()
            if method == "diagnose":
                return self.diagnose()
            if method == "state.backup":
                return self.state_backup(payload)
            if method == "state.restore":
                return self.state_restore(payload)
            raise AsterunError(INVALID_REQUEST, f"未知方法：{method}")
        except AsterunError as error:
            if method.startswith("control."):
                from asterun.control import api_error

                command = payload.get("command", {}) if isinstance(payload, dict) else {}
                request_id = command.get("request_id") if isinstance(command, dict) else None
                return Envelope(ok=False, error=api_error(error, request_id))
            return fail(error)
        except (OSError, UnicodeError) as error:
            return fail(AsterunError(PATH_INVALID, "无法读取或写入指定路径", next_action="检查路径、编码和访问权限"))

    def _handle_control(self, method, payload):
        try:
            if method == "control.command":
                result = self.control.command(payload["command"])
                # 响应只说明命令已受理/创建运行；业务完成由实体及事件确认。
                self.poll()
                return ok(result)
            if method == "control.discovery":
                return ok(self.control.discovery())
            if method == "control.resources":
                return ok(self.control.resources())
            self.poll()
            if method == "control.get":
                return ok(self.control.get(payload["kind"], payload["id"]))
            if method == "control.events":
                return ok(self.control.events(payload["task_id"], payload.get("after", 0), payload.get("limit", 100)))
            if method == "control.upload":
                return ok(self.control.upload(payload["task_id"], payload["text"]))
            if method == "control.content":
                return ok(self.control.artifact_content(payload["artifact_id"]))
        except AsterunError as error:
            from asterun.control import api_error

            return Envelope(ok=False, error=api_error(error, payload.get("command", {}).get("request_id")))
        raise AsterunError(INVALID_REQUEST, "未知标准控制入口")

    def config_validate(self) -> Envelope:
        enforce(self.policy.authorize(self.principal, "config.validate", "*"))
        applied = self.store.get_applied_revision()
        return ok(
            {
                "valid": True,
                "config": self.config.to_dict(),
                "applied_config_revision": applied,
                "loaded_config_revision": self.config.revision,
                "config_apply_required": applied != self.config.revision,
                "account_ref": self.account_ref.value,
                "runtime_ref": self.runtime_ref.value,
                "message": "配置通过校验。未知字段、未知项目和路径越界会返回独立错误码。",
            }
        )

    def config_apply(self, payload: dict[str, Any]) -> Envelope:
        enforce(self.policy.authorize(self.principal, "config.apply", "*"))
        self._require_grant("*")
        expected = payload.get("expected_revision")
        if expected is None:
            raise AsterunError(INVALID_REQUEST, "config.apply 需要 expected_revision")
        try:
            applied = self.store.cas_apply_revision(int(expected), self.config.revision)
        except AsterunError as error:
            if error.code != REVISION_CONFLICT:
                raise
            raise AsterunError(REVISION_CONFLICT,
                "配置 CAS 冲突：expected_revision 是当前已应用 revision，不是目标配置 revision",
                next_action="先 config.validate 查看 applied_config_revision；以该值传入 --expected-applied-revision（兼容 --revision）重试。核心使用启动时加载的配置，修改磁盘配置后须重新加载核心。",
                details={**error.details, "loaded_config_revision": self.config.revision,
                         "applied_config_revision": self.store.get_applied_revision()}) from error
        return ok(
            {
                "applied_config_revision": applied,
                "file_revision": self.config.revision,
                "loaded_config_revision": self.config.revision,
                "expected_applied_revision": int(expected),
                "message": "已把核心当前加载的配置 revision 应用到状态库；未重新读取或改写配置文件。",
            }
        )

    def backend_inspect(self, backend_name: str | None) -> Envelope:
        enforce(self.policy.authorize(self.principal, "backend.inspect", "*"))
        if backend_name:
            backend = self._backend(backend_name)
            return ok({"backends": [backend.inspect()]})
        rows = [self._backend(name).inspect() for name in self.config.backends]
        return ok({"backends": rows})

    def connection_verify(self, backend_name: str | None) -> Envelope:
        enforce(self.policy.authorize(self.principal, "connection.verify", "*"))
        self._require_grant("*")
        self._require_applied_config()
        backend = self._backend(backend_name)
        return ok(backend.verify_connection())

    @property
    def capabilities(self):
        from asterun.capabilities import CapabilityRuntime
        return CapabilityRuntime(self)

    def capability_invoke(self, payload: dict[str, Any]) -> Envelope:
        if self.admission is None:
            raise AsterunError(CAPABILITY_UNSUPPORTED, "结构化能力调用需要配置 v2")
        workspace = payload["workspace"]
        enforce(self.policy.authorize(self.principal, "capability.invoke", workspace))
        self._require_grant(workspace)
        self._require_applied_config()
        backend = self._backend(payload["backend"])
        if not hasattr(backend, "dispatch_capability"):
            raise AsterunError(CAPABILITY_UNSUPPORTED, "连接未实现结构化能力调用")
        validated_input = backend.registration.manifest.validate_input(payload["capability"], payload["input"])
        # 固定输入进入同一任务、意图、预留和执行器；不生成代理提示或第二个工作流。
        return self.task_submit({key: value for key, value in payload.items() if key not in {"capability", "input"}},
                                _capability=payload["capability"], _capability_input=validated_input)

    def task_submit(self, payload: dict[str, Any], *, _capability=None, _capability_input=None) -> Envelope:
        reject_self_reported_authority(payload)
        workspace_alias = str(payload.get("workspace") or "")
        if not workspace_alias:
            raise AsterunError(INVALID_REQUEST, "task.submit 需要 workspace")
        workspace = self.config.get_workspace(workspace_alias)
        enforce(self.policy.authorize(self.principal, "task.submit", workspace_alias))
        self._require_grant(workspace_alias)
        self._require_applied_config()
        expected_revision = payload.get("expected_revision")
        if expected_revision is not None and int(expected_revision) != self.config.revision:
            raise AsterunError(
                REVISION_CONFLICT,
                f"配置 revision 不匹配：请求 {expected_revision}，当前 {self.config.revision}",
                next_action="重新读取 config.validate 后再提交",
                details={"expected": expected_revision, "actual": self.config.revision},
            )
        backend_conf = self.config.get_backend(payload.get("backend"))
        self._require_grok_code_permissions(backend_conf, workspace_alias)
        input_path = payload.get("path")
        resolved_path = None
        if input_path:
            resolved_path = resolve_within(workspace.root, str(input_path))
            if not resolved_path.is_file():
                raise AsterunError(PATH_INVALID, "输入路径必须指向工作区内的可读文件")
        text = str(payload.get("text") or "")
        if resolved_path is not None and resolved_path.is_file():
            text = resolved_path.read_text(encoding="utf-8")
        backend = self._backend(backend_conf.name)
        if not backend.can_dispatch():
            raise backend.unavailable_error()
        script = str(payload.get("script") or "success")
        read_scope = payload.get("read_scope")
        if read_scope is not None:
            self._fixed_reader(workspace.root, read_scope, backend)
        if backend_conf.kind == "fake":
            if script not in FAKE_SCRIPTS:
                raise AsterunError(INVALID_REQUEST, "未知 fake 脚本")
            if script == "unsupported":
                raise AsterunError(CAPABILITY_UNSUPPORTED, "fake 脚本 unsupported：该能力被显式拒绝")
        now = utc_now()
        digest = _input_hash(
            workspace_alias,
            text,
            script,
            backend_conf.name,
            payload.get("path"),
            str(payload.get("conversation_id") or ""),
            read_scope=read_scope,
        )
        if _capability is not None:
            from asterun.control_protocol import digest as typed_digest
            digest = typed_digest({"submission_sha256": digest, "capability": _capability, "input": _capability_input})
        session = self._resolve_submit_session(payload, workspace_alias, backend_conf.name, now)
        if session.task_id is not None:
            previous = self.store.get_task(session.task_id)
            previous_scope = previous.read_scope if session.conversation_id == previous.conversation_id else None
            if previous_scope != read_scope:
                raise AsterunError(BINDING_MISMATCH, "原生会话的固定读取范围不能切换；请创建新会话")
        task = Task(
            id=new_task_id(),
            workspace=workspace_alias,
            backend=BackendId(backend_conf.name),
            text=text,
            script=script,
            acceptance=AcceptanceStatus.PENDING,
            delivery=DeliveryStatus.NOT_CONFIGURED,
            created_at=now,
            conversation_id=session.conversation_id,
            account_ref=self.account_ref,
            runtime_ref=self.runtime_ref,
            config_revision=self.config.revision,
            orchestration_owner="core",
            input_hash=digest,
            evidence_input_hash=digest,
            workspace_root=str(workspace.root.resolve()),
            capability=_capability,
            capability_input=_capability_input or {},
            read_scope=None if read_scope is None else dict(read_scope),
        )
        run = Run(
            id=new_run_id(),
            task_id=task.id,
            backend=task.backend,
            status=RunStatus.QUEUED,
            created_at=now,
        )
        task.current_run_id = run.id
        operation = Operation(
            id=new_operation_id(),
            idempotency_key=str(payload.get("idempotency_key") or new_id("idem")),
            principal=self.principal.subject_id,
            action="task.submit",
            target=workspace_alias,
            input_hash=digest,
            status=OperationStatus.INTENDED,
            created_at=now,
        )
        existing = self.store.find_idempotent_operation(operation)
        if existing is None and session.task_id is not None:
            previous = self.store.get_task(session.task_id)
            previous_run = self.store.get_run(previous.current_run_id) if previous.current_run_id else None
            if previous_run and previous_run.status not in TERMINAL_RUN_STATUSES:
                raise AsterunError(INVALID_REQUEST, "该会话还有未结束或未决运行，不能派发新轮次")
            if (session.backend_session_id and backend_conf.kind != "fake"
                    and not (hasattr(backend, "resume_native") or getattr(backend, "supports_native_resume", False))):
                raise AsterunError(CAPABILITY_UNSUPPORTED, "该适配器不支持原生续接，请使用新会话")
        decision = "start" if existing is not None else self.scheduler.admit(backend_conf.name, str(workspace.root.resolve()))
        session = replace(session, task_id=task.id, updated_at=now)
        operation, task, run, reused = self.store.commit_intent(operation, task, run, session=session)
        if reused:
            self._assert_task_binding(task)
            return ok(
                self._task_view(
                    task,
                    run,
                    extra={
                        "reused": True,
                        "operation": operation.to_dict(),
                        "session": self._session_for_task(task),
                    },
                ),
                ids={
                    "task_id": task.id.value,
                    "run_id": run.id.value,
                    "operation_id": operation.id.value,
                    **_conversation_ids(task),
                },
            )
        self._event(run.id, EventType.TASK_CREATED, "core", {"workspace": workspace_alias})
        self._event(run.id, EventType.RUN_QUEUED, "core", {})
        if decision == "queue":
            self.scheduler.enqueue(backend_conf.name, task.id, run.id, str(workspace.root.resolve()))
            self._event(run.id, EventType.QUEUE_ACCEPTED, "core", {"backend": backend_conf.name})
            return ok(
                self._task_view(
                    task,
                    run,
                    extra={
                        "queued": True,
                        "dispatched": False,
                        "operation": operation.to_dict(),
                        "session": session.to_dict(),
                        "scheduler": self.scheduler.snapshot(),
                    },
                ),
                ids={
                    "task_id": task.id.value,
                    "run_id": run.id.value,
                    "operation_id": operation.id.value,
                    "conversation_id": session.conversation_id.value,
                },
            )
        return self._dispatch_committed(task, run, operation, session, backend, backend_conf.name, now)

    def task_get(self, task_id: str) -> Envelope:
        task = self._require_task(task_id)
        enforce(self.policy.authorize(self.principal, "task.get", task.workspace))
        task = self.quality.validate_evidence(task)
        run = self.store.get_run(task.current_run_id) if task.current_run_id else None
        return ok(
            self._task_view(task, run, extra={"session": self._session_for_task(task)}),
            ids={"task_id": task.id.value, **_conversation_ids(task)},
        )

    def task_events(self, task_id: str, cursor: int = 0, page_size: int = 100) -> Envelope:
        if page_size < 1 or page_size > 500:
            raise AsterunError(INVALID_REQUEST, "page_size 必须在 1 到 500 之间")
        task = self._require_task(task_id)
        enforce(self.policy.authorize(self.principal, "task.events", task.workspace))
        if task.current_run_id is None:
            raise AsterunError(NOT_FOUND, "任务没有运行")
        events = self.store.list_events(task.current_run_id, cursor=cursor, page_size=page_size)
        next_cursor = events[-1].seq if events else cursor
        return ok(
            {
                "task_id": task.id.value,
                "run_id": task.current_run_id.value,
                "cursor": cursor,
                "next_cursor": next_cursor,
                "events": [item.to_dict() for item in events],
            },
            ids={"task_id": task.id.value, "run_id": task.current_run_id.value},
        )

    def task_cancel(self, task_id: str) -> Envelope:
        task = self._require_task(task_id)
        enforce(self.policy.authorize(self.principal, "task.cancel", task.workspace))
        self._require_grant(task.workspace)
        self._require_applied_config()
        self._assert_task_binding(task)
        if task.current_run_id is None:
            raise AsterunError(NOT_FOUND, "任务没有运行")
        run = self.store.get_run(task.current_run_id)
        if task.quality and task.quality["phase"] not in {"completed", "blocked", "stale"}:
            self.quality.pause(task, INVALID_REQUEST, "用户取消了质量流程，停止后续审查和修复", phase="blocked")
            if run.status in TERMINAL_RUN_STATUSES:
                return ok({"cancel_requested": True, "terminated": True, "workflow_stopped": True,
                           "run": run.to_dict(), "message": "质量流程已停止，保留已有运行的真实终态"},
                          ids={"task_id": task.id.value, "run_id": run.id.value})
        if run.status in TERMINAL_RUN_STATUSES:
            raise AsterunError(
                RUN_ALREADY_TERMINAL,
                f"运行已处于终态 {run.status}，取消不会改写为已取消",
                details={"status": str(run.status), "cancel_requested": run.cancel_requested},
            )
        run.cancel_requested = True
        if self.admission is not None:
            self.admission.guard_existing_run(run.id.value)
            from asterun.plugins.core_integration import bind_execution
            bind_execution(self, run, task, existing=True)
        self.store.save_run(run)
        self._event(
            run.id,
            EventType.CANCEL_REQUESTED,
            "core",
            {"accepted": True, "terminated": False},
        )
        operation = self.store.find_operation_for_run(run.id)
        backend = self._backend(run.backend.value)
        if operation is not None and operation.status == OperationStatus.INTENDED:
            self.scheduler.remove(run.id)
            result = {"status": str(RunStatus.CANCELLED), "terminated": True,
                      "summary": "排队运行已取消，未派发后端"}
        else:
            result = backend.request_cancel(run.id, task.script)
        if result.get("terminated") or result.get("raced_completion"):
            self._apply_backend_result(run, result, source=f"backend.{backend.config.kind}")
        run = self.store.get_run(run.id)
        if result.get("raced_completion") and run.status == RunStatus.SUCCEEDED:
            run.cancel_requested = True
            self.store.save_run(run)
        if run.status in TERMINAL_RUN_STATUSES:
            self._finish_operation(run)
            self.scheduler.finish(run.backend.value, run.id)
            self._drain_queue()
        return ok(
            {
                "cancel_requested": True,
                "terminated": run.terminated,
                "run": run.to_dict(),
                "message": "取消请求已受理"
                if not run.terminated
                else "取消请求已受理，并且运行已终止",
            },
            ids={"task_id": task.id.value, "run_id": run.id.value},
        )

    def _approval_context(self, payload: dict[str, Any], *, forwarding: bool = False):
        approval_id = str(payload.get("approval_id") or "")
        decision = str(payload.get("decision") or "approve")
        if not approval_id:
            raise AsterunError(INVALID_REQUEST, "approval.respond 需要 approval_id")
        if decision not in {"approve", "deny"}:
            raise AsterunError(INVALID_REQUEST, "decision 只能是 approve 或 deny")
        approval = self.store.get_approval(ApprovalId(approval_id))
        task = self._require_task(approval.task_id.value)
        enforce(self.policy.authorize(self.principal, "approval.respond", task.workspace))
        self._require_grant(task.workspace)
        self._require_applied_config()
        self._assert_task_binding(task)
        if approval.state is not ApprovalState.PENDING:
            raise AsterunError(INVALID_REQUEST, "该审批已结束，不会再次转发到后端")
        if Grant(expires_at=approval.expires_at).is_expired(utc_now()):
            raise AsterunError(
                INVALID_REQUEST,
                "审批已过期，不会转发到后端，也不会把授权改成已通过",
                details={"approval_id": approval_id, "expires_at": approval.expires_at},
            )
        expected_run = str(payload.get("expected_run_id") or "")
        expected_task = str(payload.get("expected_task_id") or "")
        expected_hash = str(payload.get("expected_target_hash") or "")
        if expected_run and expected_run != approval.run_id.value:
            raise AsterunError(INVALID_REQUEST, "审批与运行不匹配，未转发")
        if expected_task and expected_task != approval.task_id.value:
            raise AsterunError(INVALID_REQUEST, "审批与任务不匹配，未转发")
        if expected_hash and expected_hash != approval.target_hash:
            raise AsterunError(INVALID_REQUEST, "审批目标摘要不匹配，未转发")
        run = self.store.get_run(approval.run_id)
        if approval.target_hash in run.approval_attempts:
            raise AsterunError(INVALID_REQUEST, "该原生请求已有转发意图，禁止更换审批 id 重发")
        if (task.current_run_id != approval.run_id or run.task_id != task.id
                or run.status != RunStatus.WAITING_INPUT or run.cancel_requested
                or task.paused or task.orchestration_owner != "core"
                or approval.target_hash != (request_hash(task.input_hash, run.id.value, approval.native_request)
                                            if approval.native_request else task.input_hash)):
            raise AsterunError(INVALID_REQUEST, "审批已不属于当前等待输入的运行，未转发")
        if run.role == "review":
            raise AsterunError(CAPABILITY_UNSUPPORTED, "快照审查不允许通过审批扩展工具权限")
        if self._control is not None:
            managed = self.control.legacy_action(task.id.value)
            if managed:
                self.control.task(managed["task_id"], "approval.respond")
                controlled_run = self.control.store.get("Run", managed["run_id"], self.control.namespace)
                if decision == "approve":
                    if controlled_run["desired_state"] != "RUNNING" or controlled_run["control_owner"]["kind"] != "runner":
                        raise AsterunError(INVALID_REQUEST, "标准控制已暂停、接管或取消，审批不能继续副作用")
                    if forwarding:
                        with self.control.store.transaction():
                            self.control._own(managed, controlled_run)
        return approval, task, run

    def approval_assess(self, payload: dict[str, Any]) -> Envelope:
        from asterun.approval_policy import assess

        approval, task, run = self._approval_context(payload)
        assessment = assess(self.config.approval_policy, approval, task, run,
                            self.config.get_workspace(task.workspace).root, utc_now())
        self._event(run.id, EventType.APPROVAL_ASSESSED, "core", {
            "approval_id": approval.id.value, "target_hash": approval.target_hash,
            "config_revision": self.config.revision, **assessment,
        })
        return ok({"approval": approval.to_dict(), "assessment": assessment, "forwarded": False},
                  ids={"approval_id": approval.id.value, "task_id": task.id.value, "run_id": run.id.value})

    def approval_apply_policy(self, payload: dict[str, Any]) -> Envelope:
        assessment = self.approval_assess(payload).data["assessment"]
        if assessment["decision"] != "approve":
            return ok({"assessment": assessment, "forwarded": False, "manual_required": True},
                      ids={"approval_id": payload["approval_id"]})
        # 明确的 opt-in 方法；转发前再次核验全部绑定、授权和策略，不能复用陈旧 assess 输出。
        return self.approval_respond({**payload, "decision": "approve"}, policy_assessment=assessment)

    def approval_batch(self, payload: dict[str, Any]) -> Envelope:
        items = payload["items"]
        if not 1 <= len(items) <= 100:
            raise AsterunError(INVALID_REQUEST, "审批批次需要 1 至 100 项固定快照")
        results, seen = [], set()
        for item in items:
            try:
                if item["approval_id"] in seen:
                    raise AsterunError(INVALID_REQUEST, "同一批次不能重复审批 id，不会重复转发")
                seen.add(item["approval_id"])
                if item["decision"] == "policy":
                    result = self.approval_apply_policy({key: value for key, value in item.items() if key != "decision"})
                else:
                    result = self.approval_respond(item)
            except AsterunError as error:
                result = fail(error)
            results.append({"approval_id": item["approval_id"], **result.to_dict()})
        return ok({"results": results, "all_succeeded": all(result["ok"] and
                   not (result.get("data") or {}).get("manual_required") for result in results),
                   "snapshot_only": True})

    def approval_respond(self, payload: dict[str, Any], *, policy_assessment: dict | None = None) -> Envelope:
        approval, task, run = self._approval_context(payload, forwarding=True)
        decision = payload["decision"]
        if policy_assessment is not None:
            from asterun.approval_policy import assess

            latest = assess(self.config.approval_policy, approval, task, run,
                            self.config.get_workspace(task.workspace).root, utc_now())
            if latest["decision"] != "approve" or latest != policy_assessment:
                raise AsterunError(INVALID_REQUEST, "策略或审批目标已经变化，未转发")
            approval.assessment = latest
        approval.decision_source = "policy" if policy_assessment is not None else "manual"
        backend = self._backend(run.backend.value)
        # 意图和审计先落盘；进程或连接中断后 FORWARDING 永远不会自动重发。
        run.approval_attempts.append(approval.target_hash)
        self.store.save_run(run)
        approval.state = ApprovalState.FORWARDING
        approval.response_status = "forwarding_unconfirmed"
        self.store.save_approval(approval)
        self._event(run.id, EventType.APPROVAL_FORWARDING, "core", {
            "approval_id": approval.id.value, "target_hash": approval.target_hash,
            "decision": decision, "decision_source": approval.decision_source,
            "assessment": approval.assessment, "native_confirmed": False,
        })
        try:
            if approval.native_request:
                result = backend.respond_approval(run.id, decision, native_request=approval.native_request)
            else:
                result = backend.respond_approval(run.id, decision)
            if not isinstance(result, dict):
                raise ValueError("backend response must be an object")
        except Exception:
            # 只记录分类，异常消息可能包含后端秘密；保留 FORWARDING 并拒绝任何重发。
            approval.response_status = "send_result_unknown"
            self.store.save_approval(approval)
            self._event(run.id, EventType.APPROVAL_RESPONDED, "core", {
                "approval_id": approval.id.value, "decision": decision,
                "response_status": approval.response_status, "native_confirmed": False,
            })
            raise AsterunError(REMOTE_STATE_UNKNOWN, "审批发送结果未知，保留转发状态；请对账，不能重复发送") from None
        approval.native_confirmed = bool(approval.native_request) and result.get("native_confirmed") is True
        approval.response_status = ("native_confirmed" if approval.native_confirmed else
                                    "sent_unconfirmed" if approval.native_request else "local_completed")
        approval.state = ApprovalState.APPROVED if decision == "approve" else ApprovalState.DENIED
        self.store.save_approval(approval)
        self._event(run.id, EventType.APPROVAL_RESPONDED, "core", {
            "approval_id": approval.id.value, "decision": decision,
            "decision_source": approval.decision_source, "response_status": approval.response_status,
            "native_confirmed": approval.native_confirmed,
        })
        self._apply_backend_result(run, result, source=f"backend.{backend.config.kind}")
        run = self.store.get_run(run.id)
        self._finish_operation(run)
        if run.status in TERMINAL_RUN_STATUSES:
            self.scheduler.finish(run.backend.value, run.id)
            self._drain_queue()
        return ok(
            {"approval": approval.to_dict(), "run": run.to_dict(), "grant_unchanged": True, "forwarded": True},
            ids={"task_id": task.id.value, "run_id": run.id.value, "approval_id": approval.id.value},
        )

    def session_resume(self, payload: dict[str, Any]) -> Envelope:
        conversation_id = str(payload.get("conversation_id") or "")
        session_id = str(payload.get("session_id") or "")
        if not conversation_id and not session_id:
            raise AsterunError(
                INVALID_REQUEST,
                "session.resume 需要 conversation_id 或 session_id",
                next_action="先 task.submit 取得 conversation_id；不要把空续接当成原生线程可用",
            )
        if conversation_id:
            session = self.store.get_session(ConversationId(conversation_id))
        else:
            backend_name = payload.get("backend") or self.config.default_backend
            if not backend_name:
                raise AsterunError(INVALID_REQUEST, "用 session_id 续接时需要 backend")
            found = self.store.find_session_by_native(str(backend_name), session_id)
            if found is None:
                raise AsterunError(
                    NOT_FOUND,
                    "本地索引没有这条原生会话",
                    details={"backend": backend_name},
                )
            session = found
        enforce(self.policy.authorize(self.principal, "session.resume", session.workspace))
        self._require_grant(session.workspace)
        self._require_applied_config()
        workspace = str(payload.get("workspace") or "") or None
        self._assert_session_binding(session, workspace=workspace)
        expected_revision = payload.get("expected_revision")
        if expected_revision is not None and int(expected_revision) != self.config.revision:
            raise AsterunError(
                REVISION_CONFLICT,
                f"配置 revision 不匹配：请求 {expected_revision}，当前 {self.config.revision}",
                details={"expected": expected_revision, "actual": self.config.revision},
            )
        backend = self._backend(session.backend.value)
        if payload.get("native"):
            if self.admission is not None:
                self.admission.assert_session(session)
            if not (hasattr(backend, "resume_native") or getattr(backend, "supports_native_resume", False)) or not session.backend_session_id:
                raise AsterunError(CAPABILITY_UNSUPPORTED, "该会话没有可续接的原生引用或适配能力")
            if session.task_id:
                previous = self.store.get_task(session.task_id)
                previous_run = self.store.get_run(previous.current_run_id) if previous.current_run_id else None
                if previous_run and previous_run.status not in TERMINAL_RUN_STATUSES:
                    raise AsterunError(REMOTE_STATE_UNKNOWN, "原运行尚未结束，请先观察或对账")
            if hasattr(backend, "check_native_resume"):
                if not session.task_id or previous_run is None:
                    raise AsterunError(CAPABILITY_UNSUPPORTED, "插件原生续接缺少原运行绑定")
                from asterun.plugins.core_integration import bind_execution
                bind_execution(self, previous_run, previous, existing=True)
                report = backend.check_native_resume(previous_run.id,
                    {"backend_session_id": session.backend_session_id.value, "turn_id": session.latest_turn_id},
                    self.config.get_workspace(session.workspace).root)
                return ok({"session": session.to_dict(), "index_found": True, "binding_ok": True,
                           **report}, ids={"conversation_id": session.conversation_id.value})
            scope_kwargs = {}
            if (session.task_id and previous.read_scope is not None
                    and session.conversation_id == previous.conversation_id):
                scope_kwargs["read_scope"] = self._fixed_reader(
                    self.config.get_workspace(session.workspace).root, previous.read_scope, backend)
            report = backend.resume_native({"backend_session_id": session.backend_session_id.value,
                                            "turn_id": session.latest_turn_id,
                                            **({"read_scope_binding": previous.read_scope} if scope_kwargs else {})},
                                           self.config.get_workspace(session.workspace).root, **scope_kwargs)
            return ok({"session": session.to_dict(), "index_found": True, "binding_ok": True,
                       **report}, ids={"conversation_id": session.conversation_id.value})
        return ok(
            {
                "session": session.to_dict(),
                "index_found": True,
                "binding_ok": True,
                "native_checked": False,
                "native_resumed": False,
                "local_reattached": True,
                "task_id": None if session.task_id is None else session.task_id.value,
                "notes": {
                    "native_resume_is_not_index_resume": True,
                    "fake_is_not_real_connection": backend.config.kind == "fake",
                },
                "message": "已按本地索引续接对话；原生线程未核对，不能当成跨进程续接通过。",
            },
            ids={
                "conversation_id": session.conversation_id.value,
                **({"task_id": session.task_id.value} if session.task_id is not None else {}),
            },
        )

    def task_handoff(self, payload: dict[str, Any]) -> Envelope:
        task = self._require_task(str(payload.get("task_id") or ""))
        enforce(self.policy.authorize(self.principal, "task.handoff", task.workspace))
        self._require_grant(task.workspace)
        self._require_applied_config()
        action = payload.get("action")
        if action not in {"pause", "resume"}:
            raise AsterunError(INVALID_REQUEST, "task.handoff 的 action 必须是 pause 或 resume")
        run = self.store.get_run(task.current_run_id) if task.current_run_id else None
        if payload.get("external_change") is True:
            previous_acceptance = task.acceptance
            task.evidence_stale = True
            if task.acceptance is AcceptanceStatus.PASSED:
                task.acceptance = AcceptanceStatus.PENDING
            if run is not None:
                self._event(
                    run.id,
                    EventType.EVIDENCE_INVALIDATED,
                    "core",
                    {
                        "reason": "external_change",
                        "previous_acceptance": str(previous_acceptance),
                    },
                )
        if action == "pause":
            task.paused = True
            task.orchestration_owner = "human"
            if run is not None and run.status not in TERMINAL_RUN_STATUSES:
                if self.executor is None or run.id.value not in self.executor.workers:
                    self._set_run(run, RunStatus.PAUSED)
                self._event(run.id, EventType.HANDOFF_PAUSED, "core", {"orchestration_owner": "human"})
                run = self.store.get_run(run.id)
            self.store.save_task(task)
            return ok(
                {
                    "task": task.to_dict(),
                    "run": None if run is None else run.to_dict(),
                    "paused": True,
                    "orchestration_owner": "human",
                    "evidence_stale": task.evidence_stale,
                    "auto_advance_stopped": True,
                    "native_execution_stopped": run is None or run.status in TERMINAL_RUN_STATUSES,
                    "message": "已交给人工编排，自动推进已停止。",
                },
                ids={"task_id": task.id.value, **({"run_id": run.id.value} if run else {})},
            )
        task.paused = False
        task.orchestration_owner = "core"
        if run is not None and run.status == RunStatus.PAUSED:
            pending = self.store.find_pending_approval(run.id)
            operation = self.store.find_operation_for_run(run.id)
            if operation is not None and operation.status == OperationStatus.INTENDED:
                resumed_status = RunStatus.QUEUED
            elif operation is not None and operation.status == OperationStatus.UNKNOWN:
                resumed_status = RunStatus.PENDING_RECONCILE
            else:
                resumed_status = RunStatus.WAITING_INPUT if pending is not None else RunStatus.RUNNING
            self._set_run(run, resumed_status)
            self._event(run.id, EventType.HANDOFF_RESUMED, "core", {"orchestration_owner": "core"})
            run = self.store.get_run(run.id)
        self.store.save_task(task)
        self._drain_queue()
        run = self.store.get_run(run.id) if run is not None else None
        return ok(
            {
                "task": task.to_dict(),
                "run": None if run is None else run.to_dict(),
                "paused": False,
                "orchestration_owner": "core",
                "evidence_stale": task.evidence_stale,
                "auto_advance_stopped": False,
                "message": "核心已恢复编排。旧证据是否仍有效以 evidence_stale 为准。",
            },
            ids={"task_id": task.id.value, **({"run_id": run.id.value} if run else {})},
        )

    def task_present(self, payload: dict[str, Any]) -> Envelope:
        task = self._require_task(payload["task_id"])
        enforce(self.policy.authorize(self.principal, "task.present", task.workspace))
        self._require_grant(task.workspace)
        self._require_applied_config()
        self._assert_task_binding(task)
        run = self.store.get_run(task.current_run_id) if task.current_run_id else None
        if run is None or not run.native:
            raise AsterunError(REMOTE_STATE_UNKNOWN, "任务没有已持久化的原生引用，不能创建替代会话")
        worker = self.executor.workers.get(run.id.value) if self.executor else None
        if worker is not None and worker.is_alive():
            raise AsterunError(INVALID_REQUEST, "本机仍在观察该运行，请等待自动登记回执后再重试展示")
        if self.admission is not None:
            self.admission.guard_existing_run(run.id.value)
        backend = self._backend(run.backend.value)
        if not hasattr(backend, "present_native"):
            raise AsterunError(CAPABILITY_UNSUPPORTED, "该后端没有客户端项目登记适配器")
        report = backend.present_native(run.native, self.config.get_workspace(task.workspace).root)
        # 展示请求保存失败也不能通过内存存储的别名修改已保存的运行。
        run = replace(run, native={**run.native, "desktop_project": report})
        self.store.save_run(run)
        self._event(run.id, EventType.MESSAGE, "native.presentation", {"presentation": report})
        return ok({"task_id": task.id.value, "run_id": run.id.value,
                   "presentation": report, "re_dispatched": False},
                  ids={"task_id": task.id.value, "run_id": run.id.value})

    def task_reconcile(self, payload: dict[str, Any]) -> Envelope:
        requested = payload.get("task_id")
        if requested not in (None, ""):
            task = self._require_task(str(requested))
            enforce(self.policy.authorize(self.principal, "task.reconcile", task.workspace))
            self._require_grant(task.workspace)
            self._require_applied_config()
            task_ids = [task.id]
        else:
            enforce(self.policy.authorize(self.principal, "task.reconcile", "*"))
            self._require_grant("*")
            self._require_applied_config()
            task_ids = self.store.list_task_ids()
        reports: list[dict[str, Any]] = []
        unknown: list[dict[str, Any]] = []
        for task_id in task_ids:
            scoped_task = self._require_task(task_id.value)
            enforce(self.policy.authorize(self.principal, "task.reconcile", scoped_task.workspace))
            self._require_grant(scoped_task.workspace)
            self._assert_task_binding(scoped_task)
            run = self.store.get_run(scoped_task.current_run_id) if scoped_task.current_run_id else None
            if self.admission is not None and run and run.status not in TERMINAL_RUN_STATUSES:
                self.admission.guard_existing_run(run.id.value)
            backend = self._backend(run.backend.value if run else scoped_task.backend.value)
            if run and run.status not in TERMINAL_RUN_STATUSES and hasattr(backend, "reconcile_execution"):
                from asterun.plugins.core_integration import bind_execution
                bind_execution(self, run, scoped_task, existing=True)
                if not self.executor or run.id.value not in self.executor.workers:
                    self._record_result(run.id, backend.reconcile_execution(run.id, cwd=self.config.get_workspace(scoped_task.workspace).root))
            if run and run.status not in TERMINAL_RUN_STATUSES and run.native and hasattr(backend, "reconcile_native"):
                if not self.executor or run.id.value not in self.executor.workers:
                    result = backend.reconcile_native(run.native, self.config.get_workspace(scoped_task.workspace).root)
                    self._record_result(run.id, result)
            item = self._reconcile_one(task_id)
            reports.append(item)
            if item.get("unknown"):
                unknown.append(item)
        return ok(
            {
                "reports": reports,
                "unknown_states": unknown,
                "re_dispatched": False,
                "message": "按已保存的原生轮次核对结果；缺少引用或确定终态时保持未决，不重派。",
            }
        )

    def workflow_evaluate(self, payload: dict[str, Any]) -> Envelope:
        task = self._require_task(str(payload.get("task_id") or ""))
        enforce(self.policy.authorize(self.principal, "workflow.evaluate", task.workspace))
        self._require_grant(task.workspace)
        self._require_applied_config()
        self._assert_task_binding(task)
        if task.quality:
            # 已启动流程使用持久化检查和受信审查记录；调用方不能改写成更低的验收条件。
            if (payload.get("checks", task.quality["checks"]) != task.quality["checks"]
                    or payload.get("expected_revision", task.config_revision) != task.config_revision
                    or payload.get("expected_input_hash", task.input_hash) != task.input_hash):
                raise AsterunError(INVALID_REQUEST, "请求与已绑定的质量流程不一致")
            task = self.quality.validate_evidence(task)
            run = self.store.get_run(task.current_run_id)
            return ok({**self._task_view(task, run), "acceptance": str(task.acceptance),
                       "review_status": task.review_status, "quality": task.quality,
                       "run_success_is_not_acceptance": True}, ids={"task_id": task.id.value})
        run = self.store.get_run(task.current_run_id) if task.current_run_id else None
        workspace = self.config.get_workspace(task.workspace)
        review_available = None
        require_review = self.config.workflow.require_review or payload.get("require_review", False)
        if require_review:
            review_name = payload.get("review_backend") or self.config.workflow.review_backend
            if review_name:
                review_available = self._backend(str(review_name)).can_dispatch()
            else:
                review_available = False
        evaluation = evaluate_quality(
            task,
            run,
            config=self.config.workflow,
            workspace_root=workspace.root,
            payload=payload,
            review_available=review_available,
        )
        task.findings = evaluation.findings
        task.review_status = evaluation.review_status
        task.acceptance = evaluation.acceptance
        if evaluation.acceptance is AcceptanceStatus.PASSED:
            task.evidence_input_hash = evaluation.bound_input_hash
        self.store.save_task(task)
        if run is not None:
            self._event(
                run.id,
                EventType.ACCEPTANCE_EVALUATED,
                "core",
                {
                    "acceptance": str(evaluation.acceptance),
                    "review_status": evaluation.review_status,
                    "bound_revision": evaluation.bound_revision,
                    "bound_input_hash": evaluation.bound_input_hash,
                },
            )
            if evaluation.paused_for_review:
                self._event(run.id, EventType.REVIEW_UNAVAILABLE, "core", {"review_status": "unavailable"})
        if evaluation.paused_for_review:
            self.task_handoff({"task_id": task.id.value, "action": "pause"})
            task = self.store.get_task(task.id)
        return ok(
            {
                **evaluation.to_dict(),
                "task": task.to_dict(),
                "run": None if run is None else run.to_dict(),
            },
            ids={"task_id": task.id.value},
        )

    def workflow_repair(self, payload: dict[str, Any], *, _quality: bool = False) -> Envelope:
        task = self._require_task(str(payload.get("task_id") or ""))
        if task.capability:
            raise AsterunError(CAPABILITY_UNSUPPORTED, "结构化工具任务不接受代理修复提示")
        enforce(self.policy.authorize(self.principal, "workflow.repair", task.workspace))
        self._require_grant(task.workspace)
        self._require_applied_config()
        self._assert_task_binding(task)
        if task.quality and not _quality:
            raise AsterunError(CAPABILITY_UNSUPPORTED, "修复由质量流程持有，不能再启动第二个修复编排")
        if task.paused or task.orchestration_owner == "human":
            raise AsterunError(
                CAPABILITY_UNSUPPORTED,
                "任务已交给人工，不能自动修复",
                next_action="先 task.handoff resume，或由人决定是否继续",
            )
        limit = min(payload.get("max_repairs", self.config.workflow.max_repairs), self.config.workflow.max_repairs)
        backend = self._backend(task.backend.value)
        if not backend.can_dispatch():
            raise backend.unavailable_error()
        now = utc_now()
        operation = Operation(
            id=new_operation_id(),
            idempotency_key=payload.get("idempotency_key") or new_id("idem"),
            principal=self.principal.subject_id,
            action="workflow.repair",
            target=task.id.value,
            input_hash=task.input_hash,
            status=OperationStatus.INTENDED,
            created_at=now,
        )
        existing = self.store.find_idempotent_operation(operation)
        if existing is not None:
            old_run = self.store.get_run(existing.run_id)
            saved_op, saved_task, saved_run, _ = self.store.commit_intent(operation, task, old_run)
            return ok(self._task_view(saved_task, saved_run, extra={"reused": True, "operation": saved_op.to_dict()}),
                      ids={"task_id": task.id.value, "run_id": saved_run.id.value, "operation_id": saved_op.id.value})
        previous_run = self.store.get_run(task.current_run_id) if task.current_run_id else None
        if previous_run is not None and previous_run.status not in TERMINAL_RUN_STATUSES:
            raise AsterunError(INVALID_REQUEST, "当前运行尚未终止或结果未决，不能开始修复")
        if task.repair_count >= limit:
            if task.current_run_id:
                self._event(task.current_run_id, EventType.REPAIR_LIMIT, "core", {"repair_count": task.repair_count})
            raise AsterunError(
                BUDGET_EXHAUSTED,
                "已达到修复轮数上限，停止新调用",
                next_action="交接当前 finding 与继续条件，不要自行提高上限",
                details={"repair_count": task.repair_count, "max_repairs": limit},
            )
        if task.run_count >= self.config.scheduler.max_runs_per_task:
            raise AsterunError(
                BUDGET_EXHAUSTED,
                "已达到每任务运行上限，停止新调用",
                details={"run_count": task.run_count, "max_runs_per_task": self.config.scheduler.max_runs_per_task},
            )
        run = Run(
            id=new_run_id(),
            task_id=task.id,
            backend=task.backend,
            status=RunStatus.QUEUED,
            created_at=now,
            role="repair",
            prompt=repair_prompt(task) if task.findings else task.text,
            target_hash=task.quality.get("target", {}).get("hash", ""),
        )
        workspace_root = str(self.config.get_workspace(task.workspace).root.resolve())
        decision = self.scheduler.admit(task.backend.value, workspace_root)
        # 新 run、当前 run 指针及预算计数必须与意图一起提交。
        task = replace(task, repair_count=task.repair_count + 1, run_count=task.run_count + 1,
                       acceptance=AcceptanceStatus.PENDING, current_run_id=run.id,
                       review_status="not_configured")
        if task.quality:
            task = replace(task, quality={**task.quality, "phase": "repairing"})
        operation, task, run, reused = self.store.commit_intent(operation, task, run)
        if reused:
            return ok(self._task_view(task, run, extra={"reused": True, "operation": operation.to_dict()}))
        self._event(run.id, EventType.RUN_QUEUED, "core", {"repair": True, "repair_count": task.repair_count})
        backend = self._backend(task.backend.value)
        session = None
        if task.conversation_id is not None:
            try:
                session = self.store.get_session(task.conversation_id)
            except AsterunError:
                session = None
        if decision == "queue":
            self.scheduler.enqueue(task.backend.value, task.id, run.id, workspace_root)
            self._event(run.id, EventType.QUEUE_ACCEPTED, "core", {"repair": True, "backend": task.backend.value})
            return ok(
                self._task_view(
                    task,
                    run,
                    extra={
                        "queued": True,
                        "dispatched": False,
                        "repair": True,
                        "operation": operation.to_dict(),
                        "session": None if session is None else session.to_dict(),
                        "scheduler": self.scheduler.snapshot(),
                    },
                ),
                ids={
                    "task_id": task.id.value,
                    "run_id": run.id.value,
                    "operation_id": operation.id.value,
                    **_conversation_ids(task),
                },
            )
        return self._dispatch_committed(task, run, operation, session, backend, task.backend.value, now)

    def scheduler_status(self) -> Envelope:
        enforce(self.policy.authorize(self.principal, "scheduler.status", "*"))
        return ok(self.scheduler.snapshot())

    def diagnose(self) -> Envelope:
        enforce(self.policy.authorize(self.principal, "diagnose", "*"))
        return ok(build_diagnose(self))

    def state_backup(self, payload: dict[str, Any]) -> Envelope:
        enforce(self.policy.authorize(self.principal, "state.backup", "*"))
        self._require_grant("*")
        dest = payload.get("output") or payload.get("path")
        if not dest:
            raise AsterunError(INVALID_REQUEST, "state.backup 需要 output")
        if self.state_dir is None:
            raise AsterunError(INVALID_REQUEST, "state.backup 需要 --state-dir 指向 SQLite 状态目录")
        return ok(backup_state(self.state_dir, Path(str(dest))))

    def state_restore(self, payload: dict[str, Any]) -> Envelope:
        enforce(self.policy.authorize(self.principal, "state.restore", "*"))
        self._require_grant("*")
        source = payload.get("input") or payload.get("path")
        if not source:
            raise AsterunError(INVALID_REQUEST, "state.restore 需要 input")
        if self.state_dir is None:
            raise AsterunError(INVALID_REQUEST, "state.restore 需要 --state-dir 指向 SQLite 状态目录")
        if self.instance_lock is None:
            raise AsterunError(INVALID_REQUEST, "恢复必须持有状态目录的执行锁")
        for task_id in self.store.list_task_ids():
            task = self.store.get_task(task_id)
            run = self.store.get_run(task.current_run_id) if task.current_run_id else None
            if run is not None and run.status not in TERMINAL_RUN_STATUSES:
                raise AsterunError(INVALID_REQUEST, "当前应用仍有未结束的运行，不能覆盖其状态库")
        if self._control is not None and any(run["status"] not in {"SUCCEEDED", "FAILED", "CANCELED"}
                                             for run in self.control.store.list("Run", self.control.namespace)):
            raise AsterunError(INVALID_REQUEST, "标准控制仍有未完成运行，不能覆盖其状态库")
        from asterun.backup import _restore_unlocked
        from asterun.sqlite_store import SqliteStore

        result = _restore_unlocked(Path(str(source)), self.state_dir)
        if self._control is not None:
            self._control.close()
            self._control = None
        getattr(self.store, "close", lambda: None)()
        self.store = SqliteStore(self.state_dir / "asterun.sqlite")
        if self.config.schema_version == 2:
            from asterun.connections.admission import AdmissionService
            self.admission = AdmissionService(self)
            from asterun.plugins.management import sync_runtime_latches
            sync_runtime_latches(self)
        self.scheduler = Scheduler(self.config.scheduler)
        self._restore_scheduler()
        self.control
        return ok(result)

    def _require_entry(self, method: str) -> None:
        if self.entry in {None, "", "core"}:
            return
        if self.config.entries.enabled(self.entry):
            return
        if method not in ENTRY_BLOCKED_WHEN_DISABLED:
            return
        raise AsterunError(
            CAPABILITY_UNSUPPORTED,
            f"{self.entry} 入口已停用，未创建新任务",
            next_action="在 config.entries 中重新启用该入口，或改用仍启用的入口",
            details={"entry": self.entry, "method": method},
        )

    def _dispatch_committed(
        self,
        task: Task,
        run: Run,
        operation: Operation,
        session,
        backend,
        backend_name: str,
        now: str,
    ) -> Envelope:
        self._require_entry(operation.action)
        enforce(self.policy.authorize(self.principal, operation.action, task.workspace))
        self._require_grant(task.workspace)
        self._require_applied_config()
        self._assert_task_binding(task)
        self._require_grok_code_permissions(backend.config, task.workspace)
        reader = (self._fixed_reader(self.config.get_workspace(task.workspace).root, task.read_scope, backend)
                  if task.read_scope is not None and run.role != "review" else None)
        self.quality.before_dispatch(task, run)
        if self.admission is not None:
            plan = self.admission.guard_run(run.id.value)
            from asterun.connections.admission import input_digest
            if input_digest(run.prompt or task.text, task.script, run.role, capability=task.capability,
                            capability_input=task.capability_input) != plan["binding"]["input_sha256"]:
                raise AsterunError(BINDING_MISMATCH, "实际派发输入与持久计划不一致")
            from asterun.plugins.core_integration import bind_execution
            bind_execution(self, run, task)
        if self._control is not None:
            self.control.guard_legacy_dispatch(task, operation)
        if session is not None:
            self._assert_session_binding(session, workspace=task.workspace, backend=run.backend.value)
        if operation.principal != self.principal.subject_id:
            raise AsterunError("SCOPE_DENIED", "排队意图不属于当前调用主体")
        if operation.status != OperationStatus.INTENDED or run.status != RunStatus.QUEUED or run.cancel_requested:
            raise AsterunError(INVALID_REQUEST, "运行不再是可派发的排队意图")
        if not backend.can_dispatch():
            raise backend.unavailable_error()
        self.scheduler.start(backend_name, run.id, str(self.config.get_workspace(task.workspace).root.resolve()))
        self._set_run(run, RunStatus.DISPATCHING)
        self._event(run.id, EventType.RUN_DISPATCHING, "core", {"backend": backend_name})
        operation.status = OperationStatus.DISPATCHED
        self.store.save_operation(operation)
        if self.executor is not None and backend.config.kind != "fake":
            native = {"backend_session_id": session.backend_session_id.value if session and session.backend_session_id else None,
                      "turn_id": session.latest_turn_id if session else None}
            if reader is not None:
                native["read_scope_binding"] = task.read_scope
            cwd = self.config.get_workspace(task.workspace).root

            def execute(publish, stopping):
                if reader is not None:
                    return backend.dispatch_stream(task.id, run.id, task.script, run.prompt or task.text, cwd=cwd,
                                                   native=native, publish=publish, stopping=stopping, read_scope=reader)
                if task.capability:
                    return backend.dispatch_capability_stream(task.id, run.id, task.capability, task.capability_input,
                                                              cwd=cwd, publish=publish, stopping=stopping)
                if run.role == "review":
                    return backend.dispatch_review(task.id, run.id, task.script, run.prompt, cwd=cwd)
                if hasattr(backend, "dispatch_stream"):
                    return backend.dispatch_stream(task.id, run.id, task.script, run.prompt or task.text, cwd=cwd,
                                                   native=native, publish=publish, stopping=stopping)
                return backend.dispatch(task.id, run.id, task.script, run.prompt or task.text, cwd=cwd)

            self.executor.start(run.id, execute)
            return ok(self._task_view(task, run, extra={"dispatched": True, "background": True,
                      "operation": operation.to_dict(), "session": self._session_for_task(task)}),
                      ids={"task_id": task.id.value, "run_id": run.id.value,
                           "operation_id": operation.id.value, **_conversation_ids(task)})
        try:
            if task.capability:
                result = backend.dispatch_capability(task.id, run.id, task.capability, task.capability_input,
                                                      cwd=self.config.get_workspace(task.workspace).root)
            else:
                dispatch = backend.dispatch_review if run.role == "review" else backend.dispatch
                result = dispatch(task.id, run.id, task.script, run.prompt or task.text,
                                          cwd=self.config.get_workspace(task.workspace).root)
        except Exception as error:
            # 调用已经发生：即使是本地抛出的异常，也不能推断远端没有执行。
            self._set_run(run, RunStatus.PENDING_RECONCILE, error_code=REMOTE_STATE_UNKNOWN,
                          summary="后端调用未取得确定结果，需要对账")
            operation.status = OperationStatus.UNKNOWN
            self.store.save_operation(operation)
            self._event(run.id, EventType.RECONCILED, "core", {"error_code": REMOTE_STATE_UNKNOWN})
            raise AsterunError(REMOTE_STATE_UNKNOWN, "后端调用结果未确认，未自动重发",
                              details={"task_id": task.id.value, "run_id": run.id.value}) from error
        self._apply_backend_result(run, result, source=f"backend.{backend.config.kind}")
        if session is not None:
            session = self._record_session_after_dispatch(session, task, result, now)
        if result.get("needs_approval"):
            approval = ApprovalRequest(
                id=new_approval_id(),
                task_id=task.id,
                run_id=run.id,
                summary="fake 等待输入",
                state=ApprovalState.PENDING,
                created_at=utc_now(),
                target_hash=task.input_hash,
            )
            self.store.save_approval(approval)
            result = {**result, "approval_id": approval.id.value}
        task = self.store.get_task(task.id)
        run = self.store.get_run(run.id)
        if run.status == RunStatus.PENDING_RECONCILE:
            operation.status = OperationStatus.UNKNOWN
        elif run.status == RunStatus.FAILED:
            operation.status = OperationStatus.FAILED
        elif run.status in TERMINAL_RUN_STATUSES:
            operation.status = OperationStatus.COMPLETED
        else:
            operation.status = OperationStatus.DISPATCHED
        self.store.save_operation(operation)
        if (getattr(backend.config, "desktop_projects", None) is True
                and hasattr(backend, "present_native") and run.native):
            # 同步兼容入口也必须先保存原生引用与执行结果，再做客户端展示。
            try:
                report = backend.present_native(run.native, self.config.get_workspace(task.workspace).root)
            except Exception:
                report = {"registration": "unknown", "association": "unknown",
                          "client_visibility": "not_verified", "reason": "presentation_failed"}
            run, receipt = self._persist_native_presentation(run, report)
            result = {**result, "native": dict(run.native), "presentation": receipt}
        if run.status in TERMINAL_RUN_STATUSES:
            self.scheduler.finish(backend_name, run.id)
            self._drain_queue()
        return ok(
            self._task_view(
                task,
                run,
                extra={
                    "dispatch": {k: result[k] for k in result if k != "events"},
                    "operation": operation.to_dict(),
                    "reused": False,
                    "queued": False,
                    "dispatched": True,
                    "session": None if session is None else session.to_dict(),
                    "scheduler": self.scheduler.snapshot(),
                },
            ),
            ids={
                "task_id": task.id.value,
                "run_id": run.id.value,
                "operation_id": operation.id.value,
                **_conversation_ids(task),
            },
        )

    def _fixed_reader(self, root, binding, backend):
        # 这是调用者主动收窄读取范围，不增加 workspace grant，也不根据提示词推断权限。
        if backend.config.kind != "codex" or self.executor is None:
            raise AsterunError(CAPABILITY_UNSUPPORTED, "固定读取工具需要 Codex 后台执行入口")
        from asterun.read_scope import FixedReadScope
        return FixedReadScope(root, binding)

    def _drain_queue(self) -> None:
        if self.executor is not None and self.executor.stopping.is_set():
            return
        if self._draining:
            return
        self._draining = True
        try:
            for item in list(self.scheduler.queue):
                task = self.store.get_task(item.task_id)
                run = self.store.get_run(item.run_id)
                operation = self.store.find_operation_for_run(run.id)
                if run.status in TERMINAL_RUN_STATUSES or task.current_run_id != run.id:
                    self.scheduler.remove(run.id)
                    continue
                if task.paused or task.orchestration_owner == "human" or not self.scheduler.can_start(item.backend, item.workspace):
                    continue
                if self._control is not None and not self.control.legacy_may_advance(operation):
                    continue
                if operation is None or operation.status != OperationStatus.INTENDED:
                    continue
                self.scheduler.remove(run.id)
                try:
                    backend = self._backend(item.backend)
                    session_id = run.conversation_id or task.conversation_id
                    session = self.store.get_session(session_id) if session_id else None
                    self._dispatch_committed(task, run, operation, session, backend, item.backend, utc_now())
                except AsterunError as error:
                    if operation.status == OperationStatus.INTENDED:
                        self._set_run(run, RunStatus.FAILED, error_code=error.code, terminated=True,
                                      summary="派发前校验失败，未调用后端")
                        self._finish_operation(run)
        finally:
            self._draining = False

    def _finish_operation(self, run: Run) -> None:
        operation = self.store.find_operation_for_run(run.id)
        if operation is None:
            return
        if run.status == RunStatus.FAILED:
            operation.status = OperationStatus.FAILED
        elif run.status in TERMINAL_RUN_STATUSES:
            operation.status = OperationStatus.COMPLETED
        self.store.save_operation(operation)

    def poll(self, *, advance_quality: bool = True) -> None:
        """仅由拥有状态库的线程调用；终态落盘不依赖用户查询。"""
        if self._polling or self._closed:
            return
        self._polling = True
        try:
            if self.executor:
                self.executor.drain(self._record_result)
            if self._control is not None:
                self.control.advance()
            self._drain_queue()
            if advance_quality:
                self.quality.advance()
        finally:
            self._polling = False

    def _record_result(self, run_id, result):
        run = self.store.get_run(run_id)
        task = self.store.get_task(run.task_id)
        if run.status in TERMINAL_RUN_STATUSES or task.current_run_id != run.id:
            return
        backend_kind = self.config.get_backend(run.backend.value).kind
        native = result.get("native")
        has_presentation = (backend_kind == "codex" and isinstance(native, dict)
                            and "desktop_project" in native)
        presentation = native.get("desktop_project") if has_presentation else None
        if has_presentation:
            # 可选展示回执不能改变原生引用、执行结果和审批的持久化确认。
            result = {**result, "native": {key: value for key, value in native.items()
                                          if key != "desktop_project"}}
        self._apply_backend_result(run, result, f"backend.{backend_kind}")
        session_id = run.conversation_id or task.conversation_id
        if session_id and result.get("native"):
            session = self.store.get_session(session_id)
            self._record_session_after_dispatch(session, task, result, utc_now())
        requests = result.get("approvals") or ([{}] if result.get("needs_approval") else [])
        requests = [request for request in requests if
                    (request_hash(task.input_hash, run.id.value, request) if request else task.input_hash)
                    not in run.approval_attempts]
        pending = self.store.find_pending_approval(run.id)
        if pending and ((pending.native_request and "approvals" in result and pending.native_request not in requests)
                        or run.status in TERMINAL_RUN_STATUSES):
            pending.state = ApprovalState.INVALIDATED
            self.store.save_approval(pending)
        if requests and not self.store.find_pending_approval(run.id):
            request = requests[0]
            approval = ApprovalRequest(new_approval_id(), task.id, run.id,
                str((request.get("params") or {}).get("command") or request.get("method") or "fake 等待输入"),
                ApprovalState.PENDING, utc_now(),
                target_hash=request_hash(task.input_hash, run.id.value, request) if request else task.input_hash,
                native_request=request)
            self.store.save_approval(approval)
            self._event(run.id, EventType.APPROVAL_REQUESTED, "core", {"approval_id": approval.id.value,
                        "target_hash": approval.target_hash, "summary": approval.summary})
        operation = self.store.find_operation_for_run(run.id)
        if operation:
            operation.status = (OperationStatus.FAILED if run.status == RunStatus.FAILED else
                OperationStatus.COMPLETED if run.status in TERMINAL_RUN_STATUSES else
                OperationStatus.UNKNOWN if run.status == RunStatus.PENDING_RECONCILE else OperationStatus.DISPATCHED)
            self.store.save_operation(operation)
        if run.status in TERMINAL_RUN_STATUSES:
            self.scheduler.finish(run.backend.value, run.id)
            self._drain_queue()
        if has_presentation:
            self._persist_native_presentation(run, presentation)

    def _persist_native_presentation(self, run: Run, report: Any) -> tuple[Run, dict[str, Any]]:
        receipt = {"report": report, "receipt_persistence": "persisted"}
        if run.native.get("desktop_project") == report and "desktop_project" in run.native:
            return run, receipt
        presented = replace(run, native={**run.native, "desktop_project": report})
        try:
            self.store.save_run(presented)
        except Exception:
            # 写入可能已经生效，不能回滚，也不能让可选展示阻塞槽位释放或污染执行终态。
            receipt["receipt_persistence"] = "unknown"
            try:
                self._event(run.id, EventType.MESSAGE, "native.presentation",
                            {"receipt_persistence": "unknown", "reason": "presentation_receipt_unconfirmed"})
            except Exception:
                pass  # 附加诊断同样不能影响已经确认的执行记录。
            return run, receipt
        return presented, receipt

    def _restore_scheduler(self) -> None:
        for task_id in self.store.list_task_ids():
            task = self.store.get_task(task_id)
            run = self.store.get_run(task.current_run_id) if task.current_run_id else None
            if run is None or run.status in TERMINAL_RUN_STATUSES:
                continue
            operation = self.store.find_operation_for_run(run.id)
            if operation is not None and operation.status == OperationStatus.INTENDED:
                self.scheduler.enqueue(run.backend.value, task.id, run.id,
                                       self._workspace_key(task))
            else:
                self.scheduler.start(run.backend.value, run.id,
                                     self._workspace_key(task))
                self._reconcile_one(task.id)

    def _assert_task_binding(self, task: Task) -> None:
        if task.account_ref != self.account_ref or task.runtime_ref != self.runtime_ref:
            raise AsterunError(BINDING_MISMATCH, "任务绑定与当前账户/主机不一致")
        if task.config_revision != self.config.revision:
            raise AsterunError(REVISION_CONFLICT, "任务使用旧配置，请核对原工作区和后端后创建新任务")
        if task.workspace_root and task.workspace_root != str(self.config.get_workspace(task.workspace).root.resolve()):
            raise AsterunError(BINDING_MISMATCH, "工作区路径已改变，不能把旧运行绑定到新路径")
        if task.conversation_id is not None:
            session = self.store.get_session(task.conversation_id)
            self._assert_session_binding(session, workspace=task.workspace, backend=task.backend.value)

    def _workspace_key(self, task):
        if task.workspace_root:
            return task.workspace_root
        workspace = self.config.workspaces.get(task.workspace)
        return str(workspace.root.resolve()) if workspace else f"unmapped:{task.workspace}"

    def _reconcile_one(self, task_id: TaskId) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        run = self.store.get_run(task.current_run_id) if task.current_run_id else None
        operation = self.store.find_operation_for_run(run.id) if run is not None else None
        if run is None or operation is None:
            return {
                "task_id": task.id.value,
                "found": True,
                "operation_status": None if operation is None else operation.status.value,
                "run_status": None if run is None else run.status.value,
                "re_dispatched": False,
                "terminal": run is not None and run.status in TERMINAL_RUN_STATUSES,
                "unknown": False,
                "locatable": True,
            }
        terminal = run.status in TERMINAL_RUN_STATUSES
        if self.executor is not None and run.id.value in self.executor.workers:
            return {"task_id": task.id.value, "run_id": run.id.value, "run_status": str(run.status),
                    "re_dispatched": False, "terminal": terminal, "unknown": False, "observing": True}
        unknown = (not terminal) and (
            operation.status in {OperationStatus.DISPATCHED, OperationStatus.UNKNOWN}
            or run.status == RunStatus.PENDING_RECONCILE
        )
        if unknown and operation.status == OperationStatus.DISPATCHED and not terminal:
            operation.status = OperationStatus.UNKNOWN
            self.store.save_operation(operation)
            if run.status != RunStatus.PENDING_RECONCILE:
                self._set_run(run, RunStatus.PENDING_RECONCILE, error_code=REMOTE_STATE_UNKNOWN)
                run = self.store.get_run(run.id)
            self._event(
                run.id,
                EventType.RECONCILED,
                "core",
                {"re_dispatched": False, "error_code": REMOTE_STATE_UNKNOWN},
            )
        return {
            "task_id": task.id.value,
            "found": True,
            "operation_id": operation.id.value,
            "operation_status": operation.status.value,
            "run_id": run.id.value,
            "run_status": run.status.value,
            "error_code": run.error_code,
            "re_dispatched": False,
            "terminal": terminal,
            "unknown": unknown,
            "locatable": True,
        }

    def _require_grant(self, resource: str) -> None:
        enforce_grant(self.grant, resource, utc_now())

    def _require_grok_code_permissions(self, backend, workspace: str) -> None:
        if backend.kind != "grok" or backend.execution_profile != "workspace-code-v1":
            return
        # 显式同名外部插件使用自己的 manifest，不套用内置 Grok 的权限规则。
        if backend.plugin_id and "manifest_path" in self.config.plugins.get(backend.plugin_id, {}):
            return
        for action in ("workspace.read", "workspace.write", "process.execute"):
            enforce(self.policy.authorize(self.principal, action, workspace))

    def _require_applied_config(self) -> None:
        applied = self.store.get_applied_revision()
        if applied != self.config.revision:
            raise AsterunError(
                REVISION_CONFLICT,
                f"配置文件 revision {self.config.revision} 尚未应用到状态库（已应用 {applied}）",
                next_action="调用 config.apply，expected_revision 为状态库当前已应用值",
                details={"file": self.config.revision, "applied": applied},
            )

    def _resolve_submit_session(
        self,
        payload: dict[str, Any],
        workspace: str,
        backend_name: str,
        now: str,
    ) -> SessionBinding:
        raw_id = str(payload.get("conversation_id") or "")
        if raw_id:
            conversation_id = ConversationId(raw_id)
            try:
                session = self.store.get_session(conversation_id)
            except AsterunError as error:
                if error.code != NOT_FOUND:
                    raise
            else:
                self._assert_session_binding(session, workspace=workspace, backend=backend_name)
                if session.task_id is not None:
                    existing_task = self.store.get_task(session.task_id)
                    if existing_task.quality and existing_task.quality["phase"] not in {"completed", "blocked", "stale"}:
                        raise AsterunError(CAPABILITY_UNSUPPORTED, "会话仍由质量流程持有，不能并行提交新任务")
                    if existing_task.paused or existing_task.orchestration_owner == "human":
                        raise AsterunError(
                            CAPABILITY_UNSUPPORTED,
                            "会话已交给人工，自动推进已停止",
                            next_action="先 task.handoff resume，或开新 conversation",
                            details={"conversation_id": session.conversation_id.value},
                        )
                return session
        else:
            conversation_id = new_conversation_id()
        return SessionBinding(
            conversation_id=conversation_id,
            backend=BackendId(backend_name),
            workspace=workspace,
            account_ref=self.account_ref,
            runtime_ref=self.runtime_ref,
            created_at=now,
            updated_at=now,
            config_revision=self.config.revision,
        )

    def _assert_session_binding(
        self,
        session: SessionBinding,
        *,
        workspace: str | None = None,
        backend: str | None = None,
    ) -> None:
        if self.admission is not None and self.admission.store is not None:
            self.admission.store.get_session_lock(session.conversation_id.value, self.admission.namespace,
                                                  self.principal.subject_id)
        mismatches: dict[str, str] = {}
        if session.account_ref.value != self.account_ref.value:
            mismatches["account_ref"] = "mismatch"
        if session.runtime_ref.value != self.runtime_ref.value:
            mismatches["runtime_ref"] = "mismatch"
        if workspace and session.workspace != workspace:
            mismatches["workspace"] = "mismatch"
        if backend and session.backend.value != backend:
            mismatches["backend"] = "mismatch"
        if session.task_id:
            previous = self.store.get_task(session.task_id)
            root = self.config.workspaces.get(session.workspace)
            if previous.workspace_root and (root is None or previous.workspace_root != str(root.root.resolve())):
                mismatches["workspace_root"] = "mismatch"
        if mismatches:
            raise AsterunError(
                BINDING_MISMATCH,
                "会话绑定与当前账户/主机不一致，不会盲用旧线程",
                next_action="更换账户或主机后创建新会话，不要复用旧 conversation/native id",
                details={
                    "conversation_id": session.conversation_id.value,
                    "mismatches": mismatches,
                },
            )

    def _record_session_after_dispatch(
        self,
        session: SessionBinding,
        task: Task,
        result: dict[str, Any],
        now: str,
    ) -> SessionBinding:
        session.task_id = task.id
        session.updated_at = now
        native = result.get("native") if isinstance(result.get("native"), dict) else {}
        thread_id = (native.get("backend_session_id") if "backend_session_id" in native
                     else native.get("thread_id") or native.get("session_id") or native.get("provider_session_id"))
        if thread_id:
            session.backend_session_id = BackendSessionId(str(thread_id))
        turn_id = native.get("turn_id") or native.get("provider_turn_id")
        if turn_id:
            session.latest_turn_id = str(turn_id)
        self.store.save_session(session)
        return session

    def _session_for_task(self, task: Task) -> dict[str, Any] | None:
        if task.conversation_id is None:
            return None
        try:
            return self.store.get_session(task.conversation_id).to_dict()
        except AsterunError:
            return None

    def _backend(self, name: str | None) -> Backend:
        conf = self.config.get_backend(name)
        try:
            return self.backends[conf.name]
        except KeyError as exc:
            raise AsterunError(CONFIG_REQUIRED, f"后端 {conf.name} 未注册") from exc

    def _require_task(self, task_id: str) -> Task:
        if not task_id:
            raise AsterunError(INVALID_REQUEST, "需要 task_id")
        if self._control is not None:
            managed = self.control.legacy_action(task_id)
            if managed:
                self.control.task(managed["task_id"])
        task = self.store.get_task(TaskId(task_id))
        if self.admission is not None and self.admission.store is not None and task.current_run_id is not None:
            # 新 v2 执行已有主体绑定；读取也不能仅凭同一 workspace 获得他人的结果。
            # 没有插件绑定的旧 v1 记录继续按原有兼容读取规则处理。
            self.admission.store.binding_for_run(task.current_run_id.value, self.admission.namespace,
                                                 self.principal.subject_id)
        return task

    def _set_run(self, run: Run, status: RunStatus, **updates: Any) -> Run:
        run.status = status
        for key, value in updates.items():
            setattr(run, key, value)
        self.store.save_run(run)
        return run

    def _event(self, run_id: RunId, event_type: EventType, source: str, payload: dict[str, Any]) -> Event:
        event = Event(
            seq=0,
            type=event_type,
            source=source,
            timestamp=utc_now(),
            schema_version=1,
            payload=payload,
        )
        return self.store.append_event(run_id, event)

    def _apply_backend_result(self, run: Run, result: dict[str, Any], source: str) -> None:
        external = self.admission is not None and hasattr(self.backends[run.backend.value], "bind_execution")
        if external:
            try:
                with self.admission.store.transaction():
                    self._apply_backend_result_inner(run, result, source)
            except AsterunError:
                # 已收到但无法校验的 worker 结果不能作为终态，也不丢失在途意图。
                prior = self.store.get_run(run.id)
                references = {key: value for key, value in (result.get("native") or {}).items()
                              if key in {"provider_job_ref", "provider_session_id", "provider_turn_id"}
                              and isinstance(value, str) and 0 < len(value) <= 4096
                              and not any(ord(char) < 32 for char in value)}
                run.native, run.output = {**prior.native, **references}, prior.output
                self._set_run(run, RunStatus.PENDING_RECONCILE, terminated=False,
                              error_code=REMOTE_STATE_UNKNOWN, summary="插件结果未通过核心验证，保留预留等待对账")
            return
        self._apply_backend_result_inner(run, result, source)

    def _apply_backend_result_inner(self, run: Run, result: dict[str, Any], source: str) -> None:
        if self.admission is not None:
            from asterun.plugins.core_integration import ingest_result
            ingest_result(self, run, result)
        if result.get("native"):
            run.native.update(result["native"])
        for raw in result.get("events") or []:
            event_type = EventType(raw["type"]) if raw.get("type") in set(EventType) else EventType.MESSAGE
            self._event(run.id, event_type, source, {k: v for k, v in raw.items() if k != "type"})
        status = RunStatus(result["status"]) if result.get("status") else run.status
        terminated = bool(result.get("terminated"))
        if terminated and status == RunStatus.CANCELLED:
            self._event(run.id, EventType.RUN_TERMINATED, source, {"status": str(status)})
        self._set_run(
            run,
            status,
            terminated=terminated,
            error_code=result.get("error_code"),
            summary=str(result.get("summary") or run.summary),
        )

    def _task_view(self, task: Task, run: Run | None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        view = {
            "task": task.to_dict(),
            "run": None if run is None else run.to_dict(),
            "notes": {
                "run_success_is_not_acceptance": True,
                "fake_is_not_real_connection": task.backend.value
                and self.config.backends.get(task.backend.value)
                and self.config.backends[task.backend.value].kind == "fake",
            },
        }
        if extra:
            view.update(extra)
        if run:
            pending = self.store.find_pending_approval(run.id)
            view["approval"] = pending.to_dict() if pending else None
        return view


def _conversation_ids(task: Task) -> dict[str, str]:
    if task.conversation_id is None:
        return {}
    return {"conversation_id": task.conversation_id.value}


def _input_hash(
    workspace: str,
    text: str,
    script: str,
    backend: str,
    path: object,
    conversation_id: str,
    *, read_scope: dict[str, str] | None = None,
) -> str:
    payload = {
        "workspace": workspace,
        "text": text,
        "script": script,
        "backend": backend,
        "path": None if path is None else str(path),
        "conversation_id": conversation_id,
    }
    if read_scope is not None:
        payload["read_scope"] = read_scope
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_backends(config: AsterunConfig) -> dict[str, Backend]:
    from asterun.plugins.builtins import build_compatibility_backends

    return build_compatibility_backends(config)
