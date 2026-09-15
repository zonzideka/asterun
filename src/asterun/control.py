"""runner-control/v1 本机 profile，复用已有执行器而不重解释旧 Run。

控制实体、事件、命令受理和 outbox 共用现有 SQLite 事务边界。
后端内置工具仍是原生权限边界；本层预写的是 submitTurn，不是模型内部工具。
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone, timedelta
from uuid import uuid4

from asterun import control_protocol as protocol
from asterun.control_store import ControlStore
from asterun.contracts import RunStatus, TERMINAL_RUN_STATUSES, OperationStatus
from asterun.errors import AsterunError
from asterun.ids import TaskId
from asterun.policy import enforce

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELED"}
COMMANDS = ["task.create", "task.amend", "run.start", "run.retry", "run.pause",
            "run.resume", "run.cancel", "run.takeover", "run.return_control",
            "state.put", "artifact.register", "grant.revoke"]
VALIDATORS = {"val_execution": "后端执行正常结束，仅验证执行层", "val_output": "后端执行正常结束且公开输出非空"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def identifier(prefix, value=None):
    return prefix + "_" + (uuid4().hex if value is None else hashlib.sha256(value.encode()).hexdigest()[:32])


def reject(code, message):
    raise AsterunError(code, message)


def api_error(error, request_id=None):
    code = {"INVALID_REQUEST": "INVALID_ARGUMENT", "NOT_FOUND": "FORBIDDEN", "SCOPE_DENIED": "FORBIDDEN",
            "CONFIG_REQUIRED": "SETUP_REQUIRED", "BINDING_MISMATCH": "FORBIDDEN",
            "REMOTE_STATE_UNKNOWN": "UNKNOWN_REMOTE", "LEASE_CONFLICT": "LEASE_LOST"}.get(error.code, error.code)
    known = {"INVALID_ARGUMENT", "VERSION_UNSUPPORTED", "CAPABILITY_UNSUPPORTED", "BACKEND_UNAVAILABLE",
             "SETUP_REQUIRED", "AUTH_REQUIRED", "FORBIDDEN", "APPROVAL_REQUIRED", "REVISION_CONFLICT",
             "IDEMPOTENCY_CONFLICT", "LEASE_LOST", "BUDGET_EXHAUSTED", "BUDGET_UNKNOWN", "UNKNOWN_REMOTE",
             "CURSOR_EXPIRED", "EVENT_GAP", "EVIDENCE_STALE", "INTERNAL_ERROR"}
    if code not in known:
        code = "INTERNAL_ERROR"
    try:
        protocol.validate(request_id, "Id")
    except AsterunError:
        request_id = identifier("req")
    retry = {"UNKNOWN_REMOTE": "after_reconcile", "LEASE_LOST": "after_reconcile",
             "AUTH_REQUIRED": "after_auth", "SETUP_REQUIRED": "after_auth",
             "REVISION_CONFLICT": "after_refresh", "CURSOR_EXPIRED": "after_refresh",
             "BACKEND_UNAVAILABLE": "after_backoff"}.get(code, "never")
    return protocol.validate({"code": code, "message": error.message[:4000], "request_id": request_id,
                              "retry_class": retry, "retry_after_ms": None, "operation_id": None}, "ApiError")


class ControlRuntime:
    def __init__(self, app):
        self.app = app
        self.namespace = "ns_local"
        self.own_connection = not hasattr(app.store, "_conn")
        connection = (sqlite3.connect(":memory:", isolation_level=None) if self.own_connection else app.store._conn)
        self.store = ControlStore(connection, initialize=not getattr(app.store, "readonly", False))
        self.busy = False
        self.worker = identifier("wrk")

    @property
    def principal(self):
        return identifier("prn", self.app.principal.source + ":" + self.app.principal.subject_id)

    @property
    def configuration_sha256(self):
        return protocol.digest({"config": self.app.config.to_dict(), "account": self.app.account_ref.value,
                                "runtime": self.app.runtime_ref.value,
                                "resolved_roots": {name: str(binding.root.resolve()) for name, binding in self.app.config.workspaces.items()}})

    def close(self):
        if self.own_connection:
            self.store._conn.close()

    def __del__(self):
        if getattr(self, "own_connection", False):
            self.store._conn.close()

    def entity(self, kind, prefix, **fields):
        timestamp = now()
        return dict(schema_version=protocol.SCHEMA_VERSION, kind=kind, id=identifier(prefix),
                    namespace_id=self.namespace, revision=1, created_at=timestamp, updated_at=timestamp,
                    extensions={}, **fields)

    def save(self, entity, *, update=False):
        if update:
            entity["revision"] += 1
            entity["updated_at"] = now()
        protocol.validate(entity, entity["kind"])
        self.store.put(entity)
        return entity

    def event(self, task_id, subject, event_type, *, run_id=None, operation_id=None,
              before=None, reason=None, details=None, evidence=(), artifacts=()):
        event = self.entity("Event", "evt", task_id=task_id, run_id=run_id,
                            sequence=1, event_type=event_type, actor_id=self.principal,
                            operation_id=operation_id, causation_id=operation_id,
                            correlation_id=run_id or task_id, observed_at=now(), committed_at=now(),
                            source_event_ref=None, data={
                                "subject_kind": subject["kind"], "subject_id": subject["id"],
                                "from_state": before, "to_state": subject.get("status"),
                                "reason_code": reason, "evidence_ids": list(evidence),
                                "artifact_ids": list(artifacts), "details": details or {},
                            })
        protocol.validate(event, "Event")
        return self.store.append_event(event)

    def transition(self, entity, status, *, reason=None, operation_id=None):
        if entity["status"] == status:
            return entity
        # 允许边来自原始草案，守卫由各命令及 settle 路径执行。
        from asterun.control_protocol import transitions
        previous = entity["status"]
        if status not in transitions()[entity["kind"]][previous]:
            reject("INVALID_ARGUMENT", f"不允许 {entity['kind']} 从 {previous} 到 {status}")
        entity["status"] = status
        if entity["kind"] == "Run":
            entity["blocked_reason"] = reason if status == "WAITING" else None
        self.save(entity, update=True)
        task_id = entity["id"] if entity["kind"] == "Task" else entity["task_id"]
        run_id = entity["id"] if entity["kind"] == "Run" else entity.get("run_id")
        event_type = {"Task": "task.state_changed", "Run": "run.state_changed",
                      "Assignment": "assignment.state_changed", "Action": "action.result_recorded",
                      "Session": "session.state_changed"}[entity["kind"]]
        self.event(task_id, entity, event_type, run_id=run_id, operation_id=operation_id,
                   before=previous, reason=reason)
        return entity

    def discovery(self):
        result = protocol.discovery()
        result["supported_commands"] = COMMANDS
        result["backend_ids"] = [identifier("bkd", name) for name in self.app.backends]
        return protocol.validate(result, "DiscoveryResponse")

    def resources(self):
        resources = []
        for name, binding in self.app.config.workspaces.items():
            if self.app.policy.authorize(self.app.principal, "control.read", name).allowed:
                resources.append({"resource_id": identifier("rsc", name), "workspace": name,
                                  "operations": ["agent.execute"], "selector": None,
                                  "enforcement": "advisory", "location": str(binding.root)})
        return {"namespace_id": self.namespace, "principal_id": self.principal,
                "configuration_sha256": self.configuration_sha256, "resources": resources,
                "workflows": [{"id": "wf_execute", "revision": 1,
                               "backend": self.app.config.default_backend}],
                "validators": VALIDATORS, "schema_sha256": protocol.SCHEMA_SHA256}

    def workspace(self, task):
        scopes = task["resource_scopes"]
        if len(scopes) != 1:
            reject("CAPABILITY_UNSUPPORTED", "本机 profile 每任务只接入一个已配置完整工作区")
        scope = scopes[0]
        if scope["selector"] is not None or scope["operations"] != ["agent.execute"]:
            reject("CAPABILITY_UNSUPPORTED", "本适配器不能保证路径子范围或任意操作；只支持完整工作区 agent.execute")
        for name in self.app.config.workspaces:
            if scope["resource_id"] == identifier("rsc", name):
                return name
        reject("FORBIDDEN", "资源不属于当前配置")

    def authorize(self, task, action="control.read"):
        if task["owner_id"] != self.principal:
            reject("FORBIDDEN", "任务不属于当前认证主体")
        workspace = self.workspace(task)
        enforce(self.app.policy.authorize(self.app.principal, action, workspace))
        self.app._require_grant(workspace)
        return workspace

    def task(self, task_id, action="control.read"):
        task = self.store.get("Task", task_id, self.namespace)
        self.authorize(task, action)
        return task

    def get(self, kind, entity_id):
        if kind == "BackendDescriptor":
            enforce(self.app.policy.authorize(self.app.principal, "backend.inspect", "*"))
            return self.backend_descriptor(entity_id)
        if kind not in {"Task", "Run", "Operation", "State", "Action", "Budget", "Reservation",
                        "Artifact", "Assignment", "Session", "Evidence", "ExecutionContext", "Grant"}:
            reject("INVALID_ARGUMENT", "不支持读取该实体类型")
        entity = self.store.get(kind, entity_id, self.namespace)
        task_id = entity_id if kind == "Task" else entity.get("task_id")
        if task_id:
            self.task(task_id)
        elif kind == "Operation" and entity["principal_id"] != self.principal:
            reject("FORBIDDEN", "Operation 不属于当前主体")
        else:
            if kind == "ExecutionContext":
                owner = entity["extensions"].get("asterun.owner_id")
                if owner != self.principal:
                    reject("FORBIDDEN", "执行上下文不属于当前主体")
        return entity

    def backend_descriptor(self, backend_id):
        from asterun import __version__
        from asterun.backends.base import capability_rows

        for name, backend in self.app.backends.items():
            if identifier("bkd", name) != backend_id:
                continue
            kind = backend.config.kind
            result = self.entity("BackendDescriptor", "bkd", backend_type={"codex": "codex_app_server", "grok": "grok_build_acp"}.get(kind, "other"),
                                 adapter_version=__version__, native_version="unknown", wire_protocol={"codex": "app-server", "grok": "ACP", "antigravity": "agy-stream-json"}.get(kind, kind),
                                 account_ref=identifier("acc", self.app.account_ref.value), environment_ref=identifier("env", self.app.runtime_ref.value),
                                 features={"resume": "native" if kind == "codex" else "none", "interrupt": "best_effort" if kind in {"codex", "claude", "fake", "antigravity"} else "unknown",
                                           "approval_bridge": "native" if kind == "codex" else "none", "effect_control": "opaque",
                                           "event_replay": "history_reconcile" if kind == "codex" else "unknown", "usage": "provider_reported" if kind == "antigravity" else "unknown",
                                           "requires_desktop": False, "external_activity_detection": "unknown"},
                                 verification=[{"name": row.name, "status": "unsupported" if row.adapter_support == "unsupported" else "documented" if row.declared_support == "supported" else "unknown",
                                                "version_ref": __version__, "verified_at": None, "evidence_ids": []} for row in capability_rows(kind)],
                                 allowed_model_ids=[], health="UNKNOWN" if backend.config.enabled else "UNAVAILABLE", observed_at=now())
            result["id"] = backend_id
            result["extensions"] = {"asterun.simulated": kind == "fake", "asterun.native_verification": "not_tested",
                                     "asterun.submit_control": "runner_mediated"}
            return protocol.validate(result, "BackendDescriptor")
        reject("FORBIDDEN", "后端引用不属于当前实例")

    def events(self, task_id, after=0, limit=100):
        self.task(task_id)
        records = self.store.events(self.namespace, task_id, after, limit)
        return {"events": records, "next_sequence": records[-1]["sequence"] if records else after}

    def command(self, command):
        command = protocol.validate(command, "Command")
        method, params = command["method"], command["params"]
        # 授权在幂等回读之前，过期及 CAS 在回读之后。
        task = None
        if method == "task.create":
            candidate = {"owner_id": self.principal, "resource_scopes": params["resource_scopes"]}
            self.authorize(candidate, method)
        elif "task_id" in params:
            task = self.task(params["task_id"], method)
        elif "run_id" in params:
            run = self.store.get("Run", params["run_id"], self.namespace)
            task = self.task(run["task_id"], method)
        elif method == "grant.revoke":
            grant = self.store.get("Grant", params["grant_id"], self.namespace)
            task = self.task(grant["task_id"], method)
        else:
            reject("CAPABILITY_UNSUPPORTED", "该命令尚未接入本机 profile")
        digest = protocol.command_digest(command)
        with self.store.transaction():
            old = self.store.find_operation(self.namespace, self.principal, method, command["idempotency_key"])
            if old:
                if old["payload_sha256"] != digest:
                    reject("IDEMPOTENCY_CONFLICT", "同一幂等键对应了不同命令")
                if old.get("task_id"):
                    self.task(old["task_id"], method)
                return self.response(old, command["request_id"])
            if instant(command["expires_at"]) <= instant(now()):
                reject("INVALID_ARGUMENT", "新命令已过期；已受理命令请使用原幂等键查询")
            if method not in COMMANDS:
                reject("CAPABILITY_UNSUPPORTED", "该命令尚未接入本机 profile")
            operation = self.entity("Operation", "opr", task_id=task["id"] if task else None,
                                    request_id=command["request_id"], method=method,
                                    principal_id=self.principal, idempotency_key=command["idempotency_key"],
                                    payload_sha256=digest, status="SUCCEEDED", result_ref=None, error_code=None)
            result = getattr(self, "_" + method.replace(".", "_"))(command, operation)
            operation["result_ref"] = result["id"]
            operation["task_id"] = result["id"] if result["kind"] == "Task" else result.get("task_id")
            protocol.validate(operation, "Operation")
            self.store.save_operation(operation, self.principal, command["idempotency_key"], digest)
        return self.response(operation, command["request_id"])

    @staticmethod
    def response(operation, request_id):
        return protocol.validate({"schema_version": protocol.SCHEMA_VERSION, "request_id": request_id,
                                  "operation_id": operation["id"], "status": "SUCCEEDED" if operation["status"] == "SUCCEEDED" else "ACCEPTED",
                                  "result_ref": operation["result_ref"]}, "CommandResponse")

    @staticmethod
    def cas(entity, command):
        if command["expected_revision"] != (entity["revision"] if entity else 0):
            reject("REVISION_CONFLICT", "目标 revision 已变化，请读取快照")

    def _criteria(self, acceptance):
        if not any(item["required"] for item in acceptance):
            reject("INVALID_ARGUMENT", "至少需要一个必需验收条件")
        if len({item["id"] for item in acceptance}) != len(acceptance):
            reject("INVALID_ARGUMENT", "验收条件 ID 重复")
        for item in acceptance:
            if item["validator_id"] not in VALIDATORS or item["input_artifact_ids"] or item["subject_revision_sha256"]:
                reject("CAPABILITY_UNSUPPORTED", "未注册该验收参数；本 profile 只提供执行终态和公开输出验证器")

    def _task_create(self, command, operation):
        self.cas(None, command)
        p = command["params"]
        if p["bot_instance_id"] is not None:
            reject("CAPABILITY_UNSUPPORTED", "Bot setup 属于可选入口，本机核心使用 null")
        self._criteria(p["acceptance"])
        limits = p["budget_limits"]
        if len(limits) != 1 or limits[0]["meter"] != "turns" or limits[0]["enforcement"] != "hard" or limits[0]["billing_pool_ref"] is not None:
            reject("CAPABILITY_UNSUPPORTED", "当前只保证 runner 派发次数的 hard turns 预算；token 和费用不能伪装硬上限")
        task = self.entity("Task", "tsk", owner_id=self.principal, bot_instance_id=None,
                           title=p["title"], objective=p["objective"], status="DRAFT", contract_revision=1,
                           resource_scopes=p["resource_scopes"], constraints=p["constraints"],
                           acceptance=p["acceptance"], budget_id=identifier("bud"), active_run_id=None,
                           result_artifact_ids=[])
        task["extensions"]["asterun.configuration_sha256"] = self.configuration_sha256
        budget = self.entity("Budget", "bud", task_id=task["id"], limits=limits,
                             unknown_usage_policy="admit_bounded_turns", paid_overage_allowed=False, observations=[])
        budget["id"] = task["budget_id"]
        self.save(budget)
        self.save(task)
        self.event(task["id"], task, "task.created", operation_id=operation["id"])
        self.transition(task, "READY", operation_id=operation["id"])
        return task

    def _task_amend(self, command, operation):
        task = self.task(command["params"]["task_id"])
        self.cas(task, command)
        if task["status"] != "READY":
            reject("CAPABILITY_UNSUPPORTED", "只允许修改尚未运行的约定；在途或终态任务应创建新任务")
        self._criteria(command["params"]["acceptance"])
        for key in ("objective", "constraints", "acceptance"):
            task[key] = command["params"][key]
        task["contract_revision"] += 1
        self.save(task, update=True)
        self.event(task["id"], task, "task.contract_updated", operation_id=operation["id"])
        return task

    def _run_start(self, command, operation):
        return self._start(command, operation, retry=False)

    def _run_retry(self, command, operation):
        return self._start(command, operation, retry=True)

    def _start(self, command, operation, *, retry, profile_plan=None):
        p = command["params"]
        task = self.task(p["task_id"], command["method"])
        self.cas(task, command)
        if task["status"] != ("FAILED" if retry else "READY"):
            reject("INVALID_ARGUMENT", "任务状态不允许此执行请求")
        if (p["workflow_id"] != "wf_execute" or p["workflow_revision"] != 1) and profile_plan is None:
            reject("CAPABILITY_UNSUPPORTED", "工作流未注册或版本不匹配")
        self.app._require_applied_config()
        if p["configuration_sha256"] != self.configuration_sha256:
            reject("REVISION_CONFLICT", "配置、账户或环境摘要已改变")
        if task["extensions"]["asterun.configuration_sha256"] != self.configuration_sha256:
            reject("REVISION_CONFLICT", "任务创建时的资源、账户或数据接收方已变化，请重新建立约定")
        backend = self.app._backend(profile_plan["backend"] if profile_plan else None)
        if not backend.can_dispatch():
            reject("SETUP_REQUIRED", "后端尚不可派发；安装、登录和 live 门禁需分别验证")
        previous = None
        if retry:
            previous = self.store.get("Run", p["previous_run_id"], self.namespace)
            if previous["task_id"] != task["id"] or previous["status"] != "FAILED" or previous["id"] != task["active_run_id"]:
                reject("INVALID_ARGUMENT", "重试只能引用本任务最近失败且已对账的运行")
        run = self.entity("Run", "run", task_id=task["id"], attempt=1 if previous is None else previous["attempt"] + 1,
                          retry_of_run_id=previous["id"] if previous else None, status="QUEUED", desired_state="RUNNING",
                          blocked_reason=None, control_owner={"kind": "runner", "principal_id": self.principal, "epoch": 1},
                          lease=None, workflow_id=p["workflow_id"], workflow_revision=p["workflow_revision"],
                          contract_revision=task["contract_revision"], configuration_sha256=p["configuration_sha256"],
                          budget_id=task["budget_id"], checkpoint_capsule_id=None, assignment_ids=[], completion_evidence_ids=[])
        run["extensions"] = {"asterun.backend": backend.config.name, "asterun.expires_at": command["expires_at"]}
        if profile_plan is not None:
            run["extensions"]["asterun.capability_plan"] = profile_plan
        self.save(run)
        self.event(task["id"], run, "run.created", run_id=run["id"], operation_id=operation["id"])
        task["active_run_id"] = run["id"]
        self.transition(task, "ACTIVE", operation_id=operation["id"])
        # 事务 A：执行参数、授权、预算和待交付记录必须一起存活或一起回滚。
        self._admit(task, run, operation, command)
        return self.store.get("Run", run["id"], self.namespace)

    def _artifact(self, task_id, content, *, run_id=None, assignment_id=None, artifact_type="other", media_type="application/json", sensitivity="internal"):
        sha = self.store.put_blob(self.namespace, content)
        artifact = self.entity("Artifact", "art", task_id=task_id, run_id=run_id, assignment_id=assignment_id,
                               artifact_type=artifact_type, media_type=media_type, storage_ref="sqlite:sha256:" + sha,
                               sha256=sha, size_bytes=len(content), producer_id=self.principal, verification="unverified",
                               evidence_ids=[], sensitivity=sensitivity, allowed_recipient_refs=[])
        self.save(artifact)
        self.event(task_id, artifact, "artifact.registered", run_id=run_id, artifacts=[artifact["id"]])
        return artifact

    def _evidence(self, task_id, subject_hash, summary, *, run_id=None, action_id=None,
                  validator="val_execution", verdict="PASS", artifacts=()):
        evidence = self.entity("Evidence", "evd", task_id=task_id, run_id=run_id, action_id=action_id,
                               validator_id=validator, validator_revision=1, subject_sha256=subject_hash,
                               verdict=verdict, observed_at=now(), valid_until=None, source_class="runner",
                               artifact_ids=list(artifacts), summary=summary)
        return self.save(evidence)

    def _admit(self, task, run, operation, command, *, payload_override=None, role="execution", prepared_plan=None):
        import json
        workspace = self.workspace(task)
        backend_name = run["extensions"]["asterun.backend"]
        payload = {"workspace": workspace, "text": task["objective"] + ("\n约束：\n" + "\n".join(task["constraints"]) if task["constraints"] else ""),
                   "backend": backend_name, "expected_revision": self.app.config.revision,
                   "idempotency_key": "rcp_" + run["id"]}
        profile_plan = run["extensions"].get("asterun.capability_plan")
        if profile_plan and payload_override is None:
            payload["text"] = profile_plan["input"].get("text", "")
            if profile_plan["typed"]:
                payload.update(capability=profile_plan["capability"], input=profile_plan["input"])
            prepared_plan = self.app.admission.store.get_plan(profile_plan["admission_plan_id"], self.app.principal.subject_id)
        if payload_override is not None:
            payload = payload_override
        backend_name = payload["backend"]
        capability_id = identifier("cap", payload["capability"]) if "capability" in payload else "cap_submit_turn"
        parameters = self._artifact(task["id"], json.dumps(payload, ensure_ascii=False).encode(), run_id=run["id"], artifact_type="input")
        context = self.entity("ExecutionContext", "ctx", environment_ref=identifier("env", self.app.runtime_ref.value),
                              resource_id=task["resource_scopes"][0]["resource_id"], context_type="directory",
                              location_ref=str(self.app.config.get_workspace(workspace).root.resolve()),
                              isolation_profile_ref=None, enforcement="advisory", baseline_sha256=None,
                              lease=None, cleanup_policy="retain")
        context["extensions"]["asterun.owner_id"] = self.principal
        self.save(context)
        assignment = self.entity("Assignment", "asg", task_id=task["id"], run_id=run["id"], role=role, attempt=1,
                                 retry_of_assignment_id=None, status="PENDING", executor_mode="agent",
                                 backend_instance_id=identifier("bkd", backend_name), handler_id=None,
                                 execution_context_id=context["id"], session_ids=[], depends_on=[],
                                 input_artifact_ids=[parameters["id"]], acceptance=task["acceptance"], result_artifact_ids=[], evidence_ids=[])
        self.save(assignment)
        self.event(task["id"], assignment, "assignment.created", run_id=run["id"])
        self.transition(assignment, "READY")
        run["assignment_ids"].append(assignment["id"])
        action_hash = protocol.digest({"capability": capability_id, "parameters": payload,
                                       "target": task["resource_scopes"][0], "configuration": run["configuration_sha256"],
                                       "contract_revision": task["contract_revision"], "principal_id": self.principal})
        confirmation = self._evidence(task["id"], action_hash, "本机进程身份通过已配置工作区策略；不表示原生工具隔离", run_id=run["id"], validator="val_policy")
        grant = self.entity("Grant", "grt", subject_id=self.principal, bot_instance_id=None,
                            template_manifest_sha256=None, task_id=task["id"], capability_ids=[capability_id],
                            resource_scopes=task["resource_scopes"], recipient_refs=[identifier("bkd", backend_name)],
                            expires_at=command["expires_at"], max_uses=1, used_count=0, bound_action_sha256=action_hash,
                            issued_by=self.principal, status="ACTIVE", confirmation_evidence_ids=[confirmation["id"]])
        self.save(grant)
        self.event(task["id"], grant, "grant.issued", run_id=run["id"])
        lease = (run["lease"] if role != "execution" else
                 self.store.claim_lease(self.namespace, run["id"], self.worker, self._lease_deadline()))
        run["lease"] = lease
        self.save(run, update=True)
        self.event(task["id"], run, "run.lease_acquired", run_id=run["id"])
        action = self.entity("Action", "act", task_id=task["id"], run_id=run["id"], assignment_id=assignment["id"],
                             operation_id=operation["id"], capability_id=capability_id, target=task["resource_scopes"][0],
                             parameters_artifact_id=parameters["id"], parameters_sha256=protocol.digest(payload),
                             action_sha256=action_hash, idempotency_key=payload["idempotency_key"], effect_class=profile_plan["effect_class"] if profile_plan and role == "execution" else "non_idempotent_write",
                             status="PLANNED", attempt=1, grant_ids=[grant["id"]], policy_revision=self.app.config.revision,
                             authorization_digest=protocol.digest({"grant_id": grant["id"], "action_sha256": action_hash}),
                             fencing_token=lease["fencing_token"], dispatched_at=None, receipt_evidence_ids=[], effect_control="runner_mediated")
        if role != "execution":
            action["extensions"]["asterun.quality_step"] = role
        self.save(action)
        self.event(task["id"], action, "action.planned", run_id=run["id"])
        reservation = self.entity("Reservation", "res", task_id=task["id"], budget_id=task["budget_id"],
                                  action_id=action["id"], status="HELD",
                                  reserved=[{"meter": "turns", "amount": 1, "billing_pool_ref": None}], actual=None, receipt_evidence_ids=[])
        self.store.reserve(reservation)
        self.event(task["id"], reservation, "budget.reserved", run_id=run["id"])
        action["extensions"]["asterun.reservation_id"] = reservation["id"]
        self.transition(action, "ADMITTED")
        self.event(task["id"], action, "action.admitted", run_id=run["id"])
        if self.app.admission is not None:
            binding = self.app.admission.admit_control(action, payload, prepared_plan=prepared_plan, role="implementation" if role == "execution" else role)
            action["extensions"]["asterun.plugin_plan_id"] = binding["plan_id"]
            self.save(action, update=True)
        self.store.enqueue(action["id"], self.namespace, payload)
        return action

    def _control_run(self, command):
        run = self.store.get("Run", command["params"]["run_id"], self.namespace)
        self.cas(run, command)
        if run["status"] in TERMINAL:
            reject("INVALID_ARGUMENT", "终态 Run 不允许重启或改写")
        return run

    def _run_pause(self, command, operation):
        run = self._control_run(command)
        if run["desired_state"] == "CANCELED":
            reject("INVALID_ARGUMENT", "取消意图不能撤销")
        run["desired_state"] = "PAUSED"
        self.save(run, update=True)
        if run["status"] not in {"PAUSING", "PAUSED"}:
            self.transition(run, "PAUSING", operation_id=operation["id"])
        return run

    def _run_cancel(self, command, operation):
        run = self._control_run(command)
        run["desired_state"] = "CANCELED"
        self.save(run, update=True)
        self.transition(run, "CANCELING", operation_id=operation["id"])
        return run

    def _run_resume(self, command, operation):
        if command["params"].get("capsule_id") is not None:
            reject("CAPABILITY_UNSUPPORTED", "Capsule 恢复尚未接入，不能当成已核验的恢复依据")
        run = self._control_run(command)
        if run["desired_state"] != "PAUSED" or run["status"] != "PAUSED" or run["control_owner"]["kind"] != "runner":
            reject("INVALID_ARGUMENT", "只能恢复已确认暂停且归 runner 的 Run")
        self._resume(run, operation)
        return run

    def _resume(self, run, operation):
        if run["configuration_sha256"] != self.configuration_sha256:
            reject("REVISION_CONFLICT", "配置或身份已改变，恢复需重新核对")
        # 对尚未派发的 outbox，恢复不会重做任何外部副作用。
        run["desired_state"] = "RUNNING"
        self.save(run, update=True)
        self.transition(run, "RECONCILING", operation_id=operation["id"])

    def _run_takeover(self, command, operation):
        if command["params"]["requested_owner"] != "human":
            reject("CAPABILITY_UNSUPPORTED", "外部编排者桥接尚未接入")
        run = self._run_pause(command, operation)
        run["extensions"]["asterun.takeover_requested"] = "human"
        self.save(run, update=True)
        return run

    def _run_return_control(self, command, operation):
        run = self._control_run(command)
        if run["control_owner"]["kind"] != "human" or run["status"] != "PAUSED":
            reject("INVALID_ARGUMENT", "尚未确认人工接管")
        run["control_owner"] = {"kind": "runner", "principal_id": self.principal, "epoch": run["control_owner"]["epoch"] + 1}
        run["extensions"].pop("asterun.takeover_requested", None)
        self._resume(run, operation)
        self.event(run["task_id"], run, "ownership.changed", run_id=run["id"], operation_id=operation["id"])
        return run

    def _state_put(self, command, operation):
        p = command["params"]
        for evidence_id in p["evidence_ids"]:
            evidence = self.store.get("Evidence", evidence_id, self.namespace)
            if evidence["task_id"] != p["task_id"]:
                reject("FORBIDDEN", "证据不属于该任务")
        state_id = identifier("stt", p["task_id"] + ":" + p["key"])
        try:
            state = self.store.get("State", state_id, self.namespace)
        except AsterunError as error:
            if error.code != "NOT_FOUND":
                raise
            state = None
        self.cas(state, command)
        if state and state["owner_id"] != self.principal:
            reject("FORBIDDEN", "领域 State 不属于当前主体")
        new = state or self.entity("State", "stt", task_id=p["task_id"], key=p["key"], owner_id=self.principal,
                                   value=None, value_sha256=protocol.digest(None), assertion_class=p["assertion_class"], evidence_ids=[], valid_until=None)
        new.update(id=state_id, value=p["value"], value_sha256=protocol.digest(p["value"]),
                   assertion_class=p["assertion_class"], evidence_ids=p["evidence_ids"])
        self.save(new, update=state is not None)
        self.event(p["task_id"], new, "state.updated", operation_id=operation["id"], details={"asterun.revision": new["revision"]})
        return new

    def _grant_revoke(self, command, operation):
        grant = self.store.get("Grant", command["params"]["grant_id"], self.namespace)
        self.cas(grant, command)
        grant["status"] = "REVOKED"
        self.save(grant, update=True)
        self.event(grant["task_id"], grant, "grant.revoked", operation_id=operation["id"])
        return grant

    def upload(self, task_id, text):
        self.task(task_id, "artifact.upload")
        content = text.encode("utf-8")
        if len(content) > protocol.MAX_COMMAND_BYTES // 2:
            reject("INVALID_ARGUMENT", "本机文本上传超过 512 KiB")
        with self.store.transaction():
            sha = self.store.put_blob(self.namespace, content)
            upload = {"kind": "Upload", "id": identifier("upl"), "namespace_id": self.namespace,
                      "task_id": task_id, "owner_id": self.principal, "sha256": sha, "size_bytes": len(content)}
            self.store.put(upload)
        return {key: upload[key] for key in ("id", "sha256", "size_bytes")}

    def _artifact_register(self, command, operation):
        self.cas(None, command)
        p = command["params"]
        upload = self.store.get("Upload", p["upload_ref"], self.namespace)
        if upload["task_id"] != p["task_id"] or upload["owner_id"] != self.principal:
            reject("FORBIDDEN", "上传引用不属于该任务和主体")
        if upload["sha256"] != p["sha256"] or upload["size_bytes"] != p["size_bytes"]:
            reject("INVALID_ARGUMENT", "内容摘要或字节数不匹配")
        if p["run_id"]:
            run = self.store.get("Run", p["run_id"], self.namespace)
            if run["task_id"] != p["task_id"]:
                reject("FORBIDDEN", "Run 不属于任务")
        if p["assignment_id"]:
            assignment = self.store.get("Assignment", p["assignment_id"], self.namespace)
            if assignment["task_id"] != p["task_id"] or assignment["run_id"] != p["run_id"]:
                reject("FORBIDDEN", "Assignment 不属于指定 Task/Run")
        return self._artifact(p["task_id"], self.store.get_blob(self.namespace, p["sha256"]),
                              run_id=p["run_id"], assignment_id=p["assignment_id"], artifact_type=p["artifact_type"],
                              media_type=p["media_type"], sensitivity=p["sensitivity"])

    def artifact_content(self, artifact_id):
        artifact = self.get("Artifact", artifact_id)
        content = self.store.get_blob(self.namespace, artifact["sha256"])
        if len(content) > protocol.MAX_COMMAND_BYTES // 2:
            reject("INVALID_ARGUMENT", "内容过大，请读取摘要")
        return {"artifact_id": artifact_id, "text": content.decode("utf-8"), "sha256": artifact["sha256"]}

    def legacy_action(self, legacy_task_id):
        for action in self.store.list("Action", self.namespace):
            if action["extensions"].get("asterun.legacy_task_id") == legacy_task_id:
                return action
            legacy = self._find_legacy(action)
            if legacy and legacy.id.value == legacy_task_id:
                return action
        return None

    @staticmethod
    def _lease_deadline():
        return (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat().replace("+00:00", "Z")

    def _own(self, action, run):
        """调用方持写事务。OS 单实例锁授权重启接管，token 防止旧写者迟到。"""
        lease = run["lease"]
        if lease["holder_id"] != self.worker:
            if self.app.instance_lock is None:
                reject("LEASE_LOST", "未持本机单实例锁，不能接管其他执行器")
            self.store.release_lease(self.namespace, run["id"], lease["holder_id"], lease["fencing_token"])
        else:
            if instant(lease["expires_at"]) > datetime.now(timezone.utc) + timedelta(seconds=30):
                self.store.check_lease(self.namespace, run["id"], self.worker, lease["fencing_token"])
                if action["fencing_token"] != lease["fencing_token"]:
                    action["fencing_token"] = lease["fencing_token"]
                    self.save(action, update=True)
                return
            self.store.release_lease(self.namespace, run["id"], self.worker, lease["fencing_token"])
        # claim 同 holder 续期会增加 fencing；Action/Run 必须在同一事务更新。
        lease = self.store.claim_lease(self.namespace, run["id"], self.worker, self._lease_deadline())
        run["lease"] = lease
        action["fencing_token"] = lease["fencing_token"]
        self.save(run, update=True)
        self.save(action, update=True)

    def guard_legacy_dispatch(self, task, operation):
        if self.app.capabilities.guard_quality_dispatch(task, operation):
            return
        if not operation.idempotency_key.startswith("rcp_"):
            return
        run_id = operation.idempotency_key[4:]
        run = self.store.get("Run", run_id, self.namespace)
        action = next(a for a in self.store.list("Action", self.namespace) if a["run_id"] == run_id)
        self._dispatch_guard(action, run, already_admitted=True)
        if run["workflow_id"] == "wf_code_change":
            self.app.capabilities.assert_plan(run["extensions"]["asterun.capability_plan"])

    def legacy_may_advance(self, operation):
        if not operation:
            return True
        controlled = self.app.capabilities.quality_control_for_operation(operation)
        if controlled:
            return controlled["desired_state"] == "RUNNING" and controlled["control_owner"]["kind"] == "runner"
        if not operation.idempotency_key.startswith("rcp_"):
            return True
        run = self.store.get("Run", operation.idempotency_key[4:], self.namespace)
        return run["desired_state"] == "RUNNING" and run["control_owner"]["kind"] == "runner"

    def _dispatch_guard(self, action, run, *, already_admitted=False):
        task = self.task(run["task_id"], "run.start")
        if self.app.admission is not None:
            self.app.admission.guard_control(action["id"])
        self.app._require_applied_config()
        if run["extensions"].get("asterun.capability_plan") and not action["dispatched_at"] and not action["extensions"].get("asterun.quality_step"):
            self.app.capabilities.assert_plan(run["extensions"]["asterun.capability_plan"])
        if run["desired_state"] != "RUNNING" or run["control_owner"]["kind"] != "runner":
            reject("FORBIDDEN", "持久控制意图禁止派发")
        if run["configuration_sha256"] != self.configuration_sha256:
            reject("REVISION_CONFLICT", "执行前配置或身份已改变")
        if task["contract_revision"] != run["contract_revision"]:
            reject("REVISION_CONFLICT", "任务约定已变化")
        lease = run["lease"]
        if lease["holder_id"] != self.worker or action["fencing_token"] != lease["fencing_token"]:
            reject("LEASE_LOST", "旧执行器不能借用新持有者租约")
        self.store.check_lease(self.namespace, run["id"], lease["holder_id"], lease["fencing_token"])
        grant = self.store.get("Grant", action["grant_ids"][0], self.namespace)
        if grant["status"] != "ACTIVE" or instant(grant["expires_at"]) <= instant(now()):
            reject("AUTH_REQUIRED", "Grant 已失效或撤销")
        if grant["subject_id"] != self.principal or grant["bound_action_sha256"] != action["action_sha256"]:
            reject("FORBIDDEN", "Action 与授权摘要不匹配")
        if grant["used_count"] >= grant["max_uses"] and not already_admitted:
            reject("AUTH_REQUIRED", "Grant 使用次数耗尽")
        parameters = self.store.get("Artifact", action["parameters_artifact_id"], self.namespace)
        payload = protocol.loads(self.store.get_blob(self.namespace, parameters["sha256"]))
        if protocol.digest(payload) != action["parameters_sha256"]:
            reject("FORBIDDEN", "执行参数摘要已变化")
        expected = protocol.digest({"capability": action["capability_id"], "parameters": payload,
                                    "target": task["resource_scopes"][0], "configuration": run["configuration_sha256"],
                                    "contract_revision": task["contract_revision"], "principal_id": self.principal})
        if expected != action["action_sha256"]:
            reject("FORBIDDEN", "执行目标或动作内容已改变")
        return payload

    def _find_legacy(self, action):
        legacy_id = action["extensions"].get("asterun.legacy_task_id")
        if legacy_id:
            return self.app.store.get_task(TaskId(legacy_id))
        for task_id in self.app.store.list_task_ids():
            task = self.app.store.get_task(task_id)
            if task.current_run_id:
                op = self.app.store.find_operation_for_run(task.current_run_id)
                if op and op.idempotency_key == action["idempotency_key"] and op.principal == self.app.principal.subject_id:
                    return task
        return None

    def advance(self):
        if self.busy:
            return
        self.busy = True
        try:
            # 所有外部调用都在事务外；交付标记后的未知窗口不会重新 submit。
            for action in self.store.list("Action", self.namespace):
                if action["extensions"].get("asterun.quality_step"):
                    continue  # QualityRuntime 是唯一派发者；同事务 Action/outbox 只跟踪其意图。
                if action["status"] in {"SUCCEEDED", "FAILED", "DENIED", "CANCELED"}:
                    continue
                task = self.store.get("Task", action["task_id"], self.namespace)
                if task["owner_id"] != self.principal:
                    continue
                run = self.store.get("Run", action["run_id"], self.namespace)
                if run["status"] in TERMINAL:
                    continue
                try:
                    with self.store.transaction():
                        self._own(action, run)
                except AsterunError as error:
                    if error.code in {"LEASE_LOST", "LEASE_CONFLICT"}:
                        continue
                    raise
                legacy = self._find_legacy(action)
                if legacy is not None:
                    self._observe(action, run, legacy)
                elif action["dispatched_at"]:
                    with self.store.transaction():
                        self._not_applied(action, run, "LOCAL_INTENT_ABSENT")
                elif run["desired_state"] != "RUNNING":
                    with self.store.transaction():
                        self._undelivered_control(action, run)
                else:
                    self._dispatch(action, run)
        finally:
            self.busy = False

    def _dispatch(self, action, run):
        backend = run["extensions"]["asterun.backend"]
        task = self.store.get("Task", run["task_id"], self.namespace)
        workspace = self.workspace(task)
        if not self.app.scheduler.can_start(backend, str(self.app.config.get_workspace(workspace).root.resolve())):
            return
        try:
            with self.store.transaction():
                payload = self._dispatch_guard(action, run)
                grant = self.store.get("Grant", action["grant_ids"][0], self.namespace)
                grant["used_count"] += 1
                self.save(grant, update=True)
                if run["status"] in {"QUEUED", "RECONCILING", "WAITING"}:
                    self.transition(run, "PREPARING")
                action["dispatched_at"] = now()
                self.transition(action, "EXECUTING")
                self.store.mark_delivery(action["id"], self.namespace)
                assignment = self.store.get("Assignment", action["assignment_id"], self.namespace)
                self.transition(assignment, "RUNNING")
                self.event(task["id"], action, "action.started", run_id=run["id"])
            if "capability" in payload:
                self.app.capability_invoke(payload)
            else:
                self.app.task_submit(payload)
        except AsterunError as error:
            legacy = self._find_legacy(action)
            if legacy is not None:
                self._observe(action, run, legacy)
            elif action["dispatched_at"]:
                with self.store.transaction():
                    self._not_applied(action, run, error.code)
            else:
                with self.store.transaction():
                    self._deny(action, run, error.code)
            return
        legacy = self._find_legacy(action)
        if legacy:
            self._observe(action, run, legacy)

    def _not_applied(self, action, run, reason, *, receipt_text=None):
        # 无本机 intent，或受信适配器已确认未提交轮次，才能生成否定证据。
        self._own(action, run)
        receipt = self._evidence(run["task_id"], action["action_sha256"],
                                 receipt_text or "本机执行器未建立派发意图，未调用供应商",
                                 run_id=run["id"], action_id=action["id"], validator="val_dispatch", verdict="FAIL")
        action["receipt_evidence_ids"] = [receipt["id"]]
        self.transition(action, "FAILED", reason=reason)
        self._settle_reservation(action, 0, [receipt["id"]])
        self.store.finish_delivery(action["id"], self.namespace)
        assignment = self.store.get("Assignment", action["assignment_id"], self.namespace)
        self.transition(assignment, "FAILED")
        if run["desired_state"] == "CANCELED":
            if run["status"] != "CANCELING":
                self.transition(run, "CANCELING")
            self.transition(run, "CANCELED")
        else:
            if run["status"] == "PAUSING":
                self.transition(run, "RECONCILING")
            self.transition(run, "FAILED", reason=reason)
        task = self.store.get("Task", run["task_id"], self.namespace)
        self.transition(task, run["status"])

    def _confirmed_prompt_not_sent(self, legacy_run):
        backend = self.app.backends.get(legacy_run.backend.value)
        kind = getattr(getattr(backend, "config", None), "kind", None)
        if kind == "grok":
            from asterun.backends.grok import DISPATCH_RECEIPT_KEY, PRE_PROMPT_STAGES, GrokBackend
            if not isinstance(backend, GrokBackend):
                return False
            receipt_key, stages = DISPATCH_RECEIPT_KEY, PRE_PROMPT_STAGES
        elif kind == "antigravity":
            from asterun.backends.antigravity import DISPATCH_RECEIPT_KEY, PRE_PROMPT_STAGES, AntigravityBackend
            if not isinstance(backend, AntigravityBackend):
                return False
            receipt_key, stages = DISPATCH_RECEIPT_KEY, PRE_PROMPT_STAGES
        elif kind == "codex":
            from asterun.backends.codex import CodexBackend
            if not isinstance(backend, CodexBackend):
                return False
            receipt_key, stages = "asterun.codex_dispatch_v1", {"preflight"}
        else:
            return False
        receipt = legacy_run.native.get(receipt_key)
        # Only a known adapter's persisted, run-bound terminal receipt is authoritative.
        # Client State/Artifact values and native response fields never enter this path.
        return (
            legacy_run.status == RunStatus.FAILED and legacy_run.terminated is True
            and isinstance(receipt, dict)
            and receipt.get("task_id") == legacy_run.task_id.value
            and receipt.get("run_id") == legacy_run.id.value
            and isinstance(receipt.get("stage"), str) and receipt["stage"] in stages
            and receipt.get("prompt_attempted") is False
            and receipt.get("delivery") == "not_sent"
            and isinstance(receipt.get("failure_type"), str) and bool(receipt["failure_type"])
        )

    def _confirmed_cancel_before_spawn(self, legacy_run):
        backend = self.app.backends.get(legacy_run.backend.value)
        if getattr(getattr(backend, "config", None), "kind", None) != "antigravity":
            return False
        from asterun.backends.antigravity import DISPATCH_RECEIPT_KEY, AntigravityBackend

        receipt = legacy_run.native.get(DISPATCH_RECEIPT_KEY)
        # A native cancellation after prompt delivery still consumes its turn.
        # Only this adapter's bound local pre-spawn receipt proves zero delivery.
        return (
            isinstance(backend, AntigravityBackend) and backend.config.kind == "antigravity"
            and legacy_run.status == RunStatus.CANCELLED and legacy_run.terminated is True
            and legacy_run.cancel_requested is True
            and "backend_session_id" not in legacy_run.native
            and isinstance(receipt, dict)
            and receipt.get("task_id") == legacy_run.task_id.value
            and receipt.get("run_id") == legacy_run.id.value
            and receipt.get("stage") == "preflight"
            and receipt.get("prompt_attempted") is False
            and receipt.get("delivery") == "not_sent"
            and receipt.get("process_started") is False
            and receipt.get("process_exit_code") is None
            and receipt.get("failure_type") == "cancelled_before_spawn"
        )

    def _unknown(self, action, run):
        if self.app.admission is not None:
            self.app.admission.control_uncertain(action["id"])
        if action["status"] != "UNKNOWN_REMOTE":
            self.transition(action, "UNKNOWN_REMOTE")
            self.store.mark_uncertain(self.namespace, action["extensions"]["asterun.reservation_id"])
            self.event(run["task_id"], action, "observation.gap", run_id=run["id"], reason="UNKNOWN_REMOTE")
        if run["status"] != "RECONCILING":
            self.transition(run, "RECONCILING")

    def _deny(self, action, run, reason):
        self.transition(action, "DENIED", reason=reason)
        self._settle_reservation(action, 0, [])
        self.store.finish_delivery(action["id"], self.namespace)
        assignment = self.store.get("Assignment", action["assignment_id"], self.namespace)
        self.transition(assignment, "CANCELED")
        if run["status"] == "QUEUED":
            self.transition(run, "PREPARING")
        self.transition(run, "FAILED", reason=reason)
        task = self.store.get("Task", run["task_id"], self.namespace)
        self.transition(task, "FAILED", reason=reason)

    def _undelivered_control(self, action, run):
        if run["desired_state"] == "PAUSED":
            if run["status"] != "PAUSED":
                self.transition(run, "PAUSED")
            self._transfer_if_requested(run)
        else:
            self.transition(action, "CANCELED")
            self._settle_reservation(action, 0, [])
            self.store.finish_delivery(action["id"], self.namespace)
            assignment = self.store.get("Assignment", action["assignment_id"], self.namespace)
            self.transition(assignment, "CANCELED")
            self.transition(run, "CANCELED")
            task = self.store.get("Task", run["task_id"], self.namespace)
            self.transition(task, "CANCELED")

    def _transfer_if_requested(self, run):
        if run["extensions"].get("asterun.takeover_requested") and run["control_owner"]["kind"] != "human":
            run["control_owner"] = {"kind": "human", "principal_id": self.principal, "epoch": run["control_owner"]["epoch"] + 1}
            self.save(run, update=True)
            self.event(run["task_id"], run, "ownership.changed", run_id=run["id"])

    def _observe(self, action, run, legacy):
        from asterun.ids import RunId

        pinned_run = action["extensions"].get("asterun.legacy_run_id")
        legacy_run = self.app.store.get_run(RunId(pinned_run) if pinned_run else legacy.current_run_id)
        with self.store.transaction():
            self._own(action, run)
            self._bind_session(action, run, legacy, legacy_run)
        if run["workflow_id"] == "wf_code_change":
            if not action["extensions"].get("asterun.legacy_task_id"):
                with self.store.transaction():
                    action["extensions"].update({"asterun.legacy_task_id": legacy.id.value, "asterun.legacy_run_id": legacy_run.id.value})
                    self.save(action, update=True)
            if self.app.capabilities.quality_progress(action, run, legacy, legacy_run):
                return
        legacy_op = self.app.store.find_operation_for_run(legacy_run.id)
        undelivered = legacy_op and legacy_op.status == OperationStatus.INTENDED and legacy_run.status == RunStatus.QUEUED
        if undelivered and run["desired_state"] == "PAUSED":
            with self.store.transaction():
                self._own(action, run)
                if run["status"] != "PAUSED":
                    self.transition(run, "PAUSED")
                self._transfer_if_requested(run)
            return
        if undelivered and run["desired_state"] == "CANCELED":
            with self.store.transaction():
                action["extensions"]["asterun.not_delivered"] = True
                self.save(action, update=True)
        if not action["extensions"].get("asterun.legacy_task_id"):
            with self.store.transaction():
                action["extensions"].update({"asterun.legacy_task_id": legacy.id.value, "asterun.legacy_run_id": legacy_run.id.value})
                self.save(action, update=True)
        if run["desired_state"] == "CANCELED" and legacy_run.status not in TERMINAL_RUN_STATUSES and not legacy_run.cancel_requested:
            try:
                self.app.task_cancel(legacy.id.value)
            except AsterunError:
                pass  # 取消不明仍保持控制意图，后续观察/对账。
            legacy_run = self.app.store.get_run(legacy.current_run_id)
        if legacy_run.status == RunStatus.PENDING_RECONCILE:
            # 使用现有精确原生引用对账；从不重发轮次。
            try:
                self.app.task_reconcile({"task_id": legacy.id.value})
            except AsterunError:
                pass
            legacy_run = self.app.store.get_run(legacy.current_run_id)
        with self.store.transaction():
            self._own(action, run)
            if legacy_run.status in TERMINAL_RUN_STATUSES and legacy_run.terminated:
                if self._confirmed_cancel_before_spawn(legacy_run):
                    self._cancel_not_applied(action, run,
                        receipt_text="本机 Antigravity 适配器确认取消发生在创建原生进程前，未提交模型轮次")
                    return
                if self._confirmed_prompt_not_sent(legacy_run):
                    self._not_applied(action, run, legacy_run.error_code or "BACKEND_UNAVAILABLE",
                                      receipt_text="本机适配器已确认在提交 prompt 前失败，未提交模型轮次")
                    return
                if action["extensions"].get("asterun.not_delivered"):
                    self._cancel_not_applied(action, run)
                    return
                if run["desired_state"] == "PAUSED":
                    if run["status"] == "RECONCILING":
                        self.transition(run, "PAUSING")
                    if run["status"] != "PAUSED":
                        self.transition(run, "PAUSED")
                    self._transfer_if_requested(run)
                    # 此时仅执行已停，恢复后才做验收/结算；未知账目不提前释放。
                    return
                self._complete(action, run, legacy_run)
            elif legacy_run.status == RunStatus.PENDING_RECONCILE:
                self._unknown(action, run)
            elif run["desired_state"] == "RUNNING":
                if action["status"] == "UNKNOWN_REMOTE":
                    self.transition(action, "EXECUTING")
                if legacy_run.status == RunStatus.WAITING_INPUT:
                    self.transition(run, "WAITING", reason="APPROVAL_REQUIRED")
                elif legacy_run.status == RunStatus.WAITING_AUTH:
                    self.transition(run, "WAITING", reason="AUTH_REQUIRED")
                elif run["status"] != "RUNNING":
                    self.transition(run, "RUNNING")

    def _cancel_not_applied(self, action, run, *, receipt_text=None):
        receipt = self._evidence(run["task_id"], action["action_sha256"], receipt_text or "本机未派发队列已取消，无供应商调用",
                                 run_id=run["id"], action_id=action["id"], validator="val_dispatch", verdict="FAIL")
        action["receipt_evidence_ids"] = [receipt["id"]]
        if action["status"] == "EXECUTING":
            self.transition(action, "UNKNOWN_REMOTE")
        self.transition(action, "CANCELED")
        self._settle_reservation(action, 0, [receipt["id"]])
        self.store.finish_delivery(action["id"], self.namespace)
        assignment = self.store.get("Assignment", action["assignment_id"], self.namespace)
        self.transition(assignment, "CANCELED")
        self.transition(run, "CANCELED")
        self.transition(self.store.get("Task", run["task_id"], self.namespace), "CANCELED")

    def _bind_session(self, action, run, legacy, legacy_run):
        assignment = self.store.get("Assignment", action["assignment_id"], self.namespace)
        saved = (self.app.store.get_session(legacy_run.conversation_id).to_dict() if legacy_run.conversation_id
                 else self.app._session_for_task(legacy))
        native = None
        if saved and saved.get("backend_session_id"):
            native = {"backend_instance_id": assignment["backend_instance_id"],
                      "provider_session_id": saved["backend_session_id"], "provider_turn_id": saved.get("latest_turn_id"),
                      "native_project_ref": legacy.workspace_root or None, "native_uri": None,
                      "account_ref": identifier("acc", saved["account_ref"]), "environment_ref": identifier("env", saved["runtime_ref"]),
                      "provider_state_ref": None, "desktop_visibility": "unverified", "verified_at": None}
        status = ("CREATING" if legacy_run.status == RunStatus.QUEUED else
                  "DISCONNECTED" if legacy_run.status == RunStatus.PENDING_RECONCILE else "IDLE" if legacy_run.terminated else "BUSY")
        if assignment["session_ids"]:
            session = self.store.get("Session", assignment["session_ids"][0], self.namespace)
            if session["native_binding"] == native and session["status"] == status:
                return
            session["native_binding"] = native
            session["last_observed_at"] = now()
            self.save(session, update=True)
            if session["status"] != status:
                self.transition(session, status)
            return
        session = self.entity("Session", "ses", task_id=run["task_id"], assignment_id=assignment["id"], status="CREATING",
                              backend_instance_id=assignment["backend_instance_id"], execution_context_id=assignment["execution_context_id"],
                              model_provider="unknown", model_id=None, billing_pool_ref=None, native_binding=native,
                              restored_from_capsule_id=None, last_observed_at=now(), capability_snapshot_sha256=protocol.digest({
                                  "backend": legacy_run.backend.value, "configuration": run["configuration_sha256"]}))
        session["extensions"] = {"asterun.legacy_conversation_id": saved["conversation_id"] if saved else None,
                                  "asterun.simulated": self.app.backends[legacy_run.backend.value].config.kind == "fake"}
        self.save(session)
        self.event(run["task_id"], session, "session.bound", run_id=run["id"])
        self.transition(session, status)
        assignment["session_ids"] = [session["id"]]
        self.save(assignment, update=True)

    def _settle_reservation(self, action, amount, evidence):
        if self.app.admission is not None:
            self.app.admission.control_settle(action["id"], not_applied=amount == 0)
        reservation = self.store.settle(self.namespace, action["extensions"]["asterun.reservation_id"],
                                        [{"meter": "turns", "amount": amount, "billing_pool_ref": None}], evidence)
        self.event(action["task_id"], reservation, "budget.settled", run_id=action["run_id"], evidence=evidence)

    def _complete(self, action, run, legacy_run):
        task = self.store.get("Task", run["task_id"], self.namespace)
        assignment = self.store.get("Assignment", action["assignment_id"], self.namespace)
        content = legacy_run.summary.encode()
        if run["workflow_id"] == "wf_capability_call":
            import json
            content = json.dumps(legacy_run.output if run["extensions"]["asterun.capability_plan"]["typed"] else {"text": legacy_run.summary}, ensure_ascii=False).encode()
        output = self._artifact(task["id"], content, run_id=run["id"],
                                assignment_id=assignment["id"], media_type="application/json" if run["workflow_id"] == "wf_capability_call" else "text/plain", artifact_type="document")
        receipt = self._evidence(task["id"], action["action_sha256"], "后端执行终态回执；不代表业务验收", run_id=run["id"],
                                 action_id=action["id"], validator="val_receipt", artifacts=[output["id"]])
        action["receipt_evidence_ids"] = [receipt["id"]]
        if action["status"] == "UNKNOWN_REMOTE":
            self.transition(action, "VERIFYING")
        elif action["status"] == "EXECUTING":
            self.transition(action, "VERIFYING")
        self.transition(action, "SUCCEEDED")  # submitTurn 的效果已确认，模型任务可能失败。
        self._settle_reservation(action, 0 if action["extensions"].get("asterun.not_delivered") else 1, [receipt["id"]])
        self.store.finish_delivery(action["id"], self.namespace)
        if run["desired_state"] == "CANCELED":
            if run["status"] != "CANCELING":
                self.transition(run, "CANCELING")
            self.transition(assignment, "CANCELED")
            self.transition(run, "CANCELED")
            task["result_artifact_ids"].append(output["id"])
            self.transition(task, "CANCELED")
            return
        if run["status"] != "RUNNING":
            self.transition(run, "RUNNING")
        self.transition(assignment, "VERIFYING")
        passed = True
        evidence_ids = []
        subject = protocol.digest({"contract_revision": task["contract_revision"], "configuration_sha256": run["configuration_sha256"],
                                   "output_sha256": output["sha256"], "legacy_status": str(legacy_run.status)})
        for criterion in task["acceptance"]:
            result = (legacy_run.status == RunStatus.SUCCEEDED
                      and run["configuration_sha256"] == self.configuration_sha256
                      and run["contract_revision"] == task["contract_revision"])
            if criterion["validator_id"] == "val_output":
                result = result and bool(legacy_run.summary.strip())
            evidence = self._evidence(task["id"], subject, VALIDATORS[criterion["validator_id"]], run_id=run["id"],
                                      validator=criterion["validator_id"], verdict="PASS" if result else "FAIL", artifacts=[output["id"]])
            evidence_ids.append(evidence["id"])
            if criterion["required"]:
                passed = passed and result
        assignment["result_artifact_ids"] = [output["id"]]
        assignment["evidence_ids"] = evidence_ids
        self.transition(assignment, "SUCCEEDED" if passed else "FAILED")
        if passed:
            self.event(task["id"], assignment, "assignment.accepted", run_id=run["id"], evidence=evidence_ids)
        if run["workflow_id"] == "wf_code_change" and passed:
            quality_evidence = self.app.capabilities.quality_evidence(task, run, action)
            evidence_ids.append(quality_evidence["id"])
            assignment["evidence_ids"] = evidence_ids
            self.save(assignment, update=True)
        run["completion_evidence_ids"] = evidence_ids
        self.transition(run, "SUCCEEDED" if passed else "FAILED")
        task["result_artifact_ids"].append(output["id"])
        self.transition(task, "SUCCEEDED" if passed else "FAILED")
