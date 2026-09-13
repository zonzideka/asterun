"""v2 内部固定计划与准入；不启动供应商，不拥有第二个执行器。"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from asterun.control_protocol import digest
from asterun.contracts import OperationStatus, RunStatus, TERMINAL_RUN_STATUSES
from asterun.errors import AsterunError
from asterun.plugin_api.protocol import json_copy
from asterun.plugins.builtins import build_registry
from asterun.policy import enforce


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def expired(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) <= datetime.now(timezone.utc)


def input_digest(text, script="success", role="implementation", *, capability=None, capability_input=None):
    if capability:
        return digest({"capability": capability, "input": capability_input, "role": role})
    return digest({"text": text, "script": script, "role": role})


class AdmissionService:
    def __init__(self, app):
        self.app = app
        self.namespace = "ns_local"
        self.store = None
        if hasattr(app.store, "_conn"):
            from asterun.plugin_store import PluginStore
            self.store = PluginStore(app.store._conn)
            app.store.intent_hook = self.admit_intent
            app.store.run_hook = self.sync_run

    @property
    def principal(self):
        return self.app.principal.subject_id

    def persistent(self):
        if self.store is None:
            raise AsterunError("CONFIG_REQUIRED", "v2 执行绑定需要独立 SQLite 状态目录")
        return self.store

    def _authorize(self, workspace):
        enforce(self.app.policy.authorize(self.app.principal, "task.submit", workspace))
        self.app._require_grant(workspace)
        self.app._require_applied_config()

    def _snapshot(self, workspace, backend_alias, input_sha256, role, *, existing=False, capability="agent.execute"):
        if not existing:
            self._authorize(workspace)
        app = self.app
        backend = app.config.get_backend(backend_alias)
        ref = backend.connection_ref
        if not ref or ref not in app.config.connections:
            raise AsterunError("BINDING_MISMATCH", "v2 后端缺少显式连接绑定")
        connection = app.config.connections[ref]
        registration = build_registry(app.config).verify_installation(connection["plugin_id"])
        from asterun.plugins.management import plugin_enabled
        if not existing and (not connection["enabled"] or not plugin_enabled(app, registration)):
            raise AsterunError("BACKEND_UNAVAILABLE", "插件或连接已停用，不能创建新动作")
        provider = app.config.provider_accounts.get(connection["provider_account_ref"], {})
        if provider.get("revoked"):
            raise AsterunError("AUTH_REQUIRED", "供应商凭据引用已撤销")
        if connection["auth_mode"] not in registration.manifest.to_dict()["auth_modes"]:
            raise AsterunError("BINDING_MISMATCH", "认证模式与已固定插件声明不匹配")
        declaration = registration.manifest.capability(capability)
        if declaration["support"] == "unsupported":
            raise AsterunError("CAPABILITY_UNSUPPORTED", "插件不支持请求的能力")
        if not existing:
            for permission in declaration["permission_requirements"]:
                enforce(app.policy.authorize(app.principal, permission, workspace))
        pools = {key: app.config.billing_pools[key] for key in connection["billing_pool_refs"]}
        if not pools:
            raise AsterunError("BUDGET_EXHAUSTED", "执行必须绑定有界本地计费池")
        if registration.source == "external" and any(pool["billing_mode"] not in registration.manifest.to_dict()["billing_modes"] for pool in pools.values()):
            raise AsterunError("BINDING_MISMATCH", "计费模式与外部插件声明不匹配")
        return json_copy({
            "workspace": workspace, "workspace_root": str(app.config.get_workspace(workspace).root.resolve()),
            "backend_alias": backend_alias, "connection_ref": ref, "connection": connection,
            "provider_account_ref": connection["provider_account_ref"], "provider_account": provider,
            "core_account_ref": app.account_ref.value, "core_runtime_ref": app.runtime_ref.value,
            "plugin_id": registration.plugin_id, "plugin_version": registration.manifest.plugin_version,
            "manifest_sha256": registration.manifest.sha256,
            "installation_sha256": registration.installation_sha256,
            "runner_sha256": registration.runner_sha256, "runtime_sha256": registration.runtime_sha256,
            "configuration_sha256": digest(app.config.to_dict()), "config_revision": app.config.revision,
            "permission_policy_sha256": digest(app.config.approval_policy),
            "authorization_sha256": digest({"principal": self.principal,
                "grant": app.grant.to_dict() if app.grant else None, "policy_revision": app.config.revision}),
            "input_sha256": input_sha256, "role": role, "billing_pools": pools, "capability": capability,
        })

    def prepare(self, workspace, backend_alias, input_sha256, role="implementation", *, capability="agent.execute"):
        store = self.persistent()
        binding = self._snapshot(workspace, backend_alias, input_sha256, role, capability=capability)
        reservations = []
        for ref, pool in binding["billing_pools"].items():
            mode = pool["billing_mode"]
            if mode not in {"local", "subscription", "metered"}:
                raise AsterunError("BUDGET_EXHAUSTED", "计费模式未知，未批准派发")
            if mode != "local":
                expiry = pool.get("valid_until")
                observed = pool.get("observed_at")
                if expiry and expired(expiry):
                    raise AsterunError("BUDGET_EXHAUSTED", "额度或费用观察已过期")
                if pool.get("remaining") is not None:
                    if not observed or not expiry or expired(expiry):
                        raise AsterunError("BUDGET_EXHAUSTED", "额度观察缺少有效期或已过期")
                if mode == "subscription" and pool.get("remaining") is None:
                    if not pool.get("overage_verified") or not pool.get("allow_unknown_subscription"):
                        raise AsterunError("BUDGET_EXHAUSTED", "未知订阅额度需明确无 overage 证据及有限尝试政策")
                if mode == "metered" and (pool.get("risk_acceptance") != "local_estimate" or not pool.get("estimate_per_action")):
                    raise AsterunError("BUDGET_EXHAUSTED", "API 费用估计或接受本地估计政策缺失")
            amount = pool.get("estimate_per_action", 1 if mode == "local" else None)
            limit = pool.get("local_limit", 100 if mode == "local" else None)
            if type(amount) is not int or amount <= 0 or type(limit) is not int or limit < amount:
                raise AsterunError("BUDGET_EXHAUSTED", "缺少有界本地预算或预留已超限")
            reservations.append({"pool_ref": ref, "meter": pool.get("meter", "requests"),
                                 "amount": amount, "window_id": pool.get("window_id", "local")})
        plan = {"id": "ppl_" + uuid4().hex, "namespace_id": self.namespace,
                "principal_id": self.principal, "binding": binding, "binding_sha256": digest(binding),
                "reservations": reservations, "created_at": now()}
        return store.put_plan(plan)

    def assert_plan(self, plan):
        self.persistent()
        if plan["principal_id"] != self.principal or plan["namespace_id"] != self.namespace:
            raise AsterunError("SCOPE_DENIED", "计划不属于当前主体或命名空间")
        binding = plan["binding"]
        current = self._snapshot(binding["workspace"], binding["backend_alias"], binding["input_sha256"], binding["role"],
                                 capability=binding.get("capability", "agent.execute"))
        if digest(current) != plan["binding_sha256"]:
            raise AsterunError("BINDING_MISMATCH", "计划的配置、账户、模型、插件、权限或计费池已变化，请重新准备")
        # dispatch 也核验 quota TTL；不新建或重新预留计划。
        for pool in current["billing_pools"].values():
            if pool["billing_mode"] != "local" and (
                    pool.get("valid_until") and expired(pool["valid_until"]) or
                    pool.get("remaining") is not None and not pool.get("valid_until")):
                raise AsterunError("BUDGET_EXHAUSTED", "计划的额度观察已过期")
        return plan

    def _plan(self, binding):
        return self.store.get_plan(binding["plan_id"], self.principal, self.namespace)

    def guard_run(self, run_id):
        binding = self.persistent().binding_for_run(run_id, self.namespace, self.principal)
        if binding is None:
            raise AsterunError("BINDING_MISMATCH", "v2 运行缺少持久执行绑定，禁止回退到旧派发路径")
        return self.assert_plan(self._plan(binding))

    def guard_control(self, action_id):
        binding = self.persistent().binding_for_control(action_id, self.namespace, self.principal)
        if binding is None:
            raise AsterunError("BINDING_MISMATCH", "标准 Action 缺少插件计划")
        return self.assert_plan(self._plan(binding))

    @staticmethod
    def session_lock(plan):
        transient = {"input_sha256", "role", "authorization_sha256", "config_revision", "configuration_sha256"}
        lock = json_copy({key: value for key, value in plan["binding"].items() if key not in transient})
        # 停用只禁止新派发；已授权取消/对账仍必须使用相同的账户和插件字节。
        lock["connection"].pop("enabled", None)
        return lock

    def guard_existing_run(self, run_id):
        binding = self.persistent().binding_for_run(run_id, self.namespace, self.principal)
        if binding is None:
            raise AsterunError("BINDING_MISMATCH", "旧运行没有插件连接绑定")
        plan = self._plan(binding)
        old = plan["binding"]
        current = self._snapshot(old["workspace"], old["backend_alias"], old["input_sha256"], old["role"], existing=True,
                                 capability=old.get("capability", "agent.execute"))
        if digest(self.session_lock({"binding": current})) != digest(self.session_lock(plan)):
            raise AsterunError("BINDING_MISMATCH", "运行的插件、账户、模型或权限已改变，不能在新连接上取消或对账")
        return plan

    def admit_intent(self, operation, task, run, session):
        store = self.persistent()
        expected_input = input_digest(run.prompt or task.text, task.script, run.role,
                                      capability=task.capability, capability_input=task.capability_input)
        managed = self.app.control.legacy_action(task.id.value)
        # 事务 A 已预留的标准动作，通过保留键查到原计划，只绑定一次。
        if managed is None and operation.idempotency_key.startswith("rcp_"):
            for action in self.app.control.store.list("Action", self.namespace):
                if action["idempotency_key"] == operation.idempotency_key:
                    managed = action
                    break
        if managed is not None and run.role in {"review", "repair"}:
            managed = self.app.capabilities.admit_quality_intent(managed, operation, task, run)
        if managed is not None:
            plan = self.guard_control(managed["id"])
            if plan["binding"]["input_sha256"] != expected_input or plan["binding"]["backend_alias"] != run.backend.value:
                raise AsterunError("BINDING_MISMATCH", "底层执行与标准 Action 的已预留输入不一致")
            binding = store.bind_legacy(managed["id"], run.id.value, self.namespace)
        else:
            plan = self.prepare(task.workspace, run.backend.value, expected_input, run.role,
                                capability=task.capability or "agent.execute")
            binding = store.admit(plan, plan["binding"]["billing_pools"], legacy_run_id=run.id.value)
        if session is None and (run.conversation_id or task.conversation_id):
            session = self.app.store.get_session(run.conversation_id or task.conversation_id)
        if session is not None:
            old = store.get_session_lock(session.conversation_id.value, self.namespace, self.principal)
            if old is None and session.backend_session_id is not None:
                raise AsterunError("BINDING_MISMATCH", "旧原生会话没有插件连接锁，不能自动认领为新账户会话")
            store.bind_session(session.conversation_id.value, self.session_lock(plan), self.namespace, principal=self.principal)
        return binding

    def admit_control(self, action, payload, *, prepared_plan=None, role="implementation"):
        expected = input_digest(payload["text"], role=role, capability=payload.get("capability"), capability_input=payload.get("input"))
        plan = prepared_plan or self.prepare(payload["workspace"], payload["backend"], expected, role,
                                             capability=payload.get("capability", "agent.execute"))
        self.assert_plan(plan)
        if plan["binding"]["input_sha256"] != expected or plan["binding"]["backend_alias"] != payload["backend"]:
            raise AsterunError("BINDING_MISMATCH", "标准动作输入与准备计划不同")
        binding = self.store.admit(plan, plan["binding"]["billing_pools"], control_action_id=action["id"])
        return binding

    def assert_session(self, session):
        if self.store is None:
            return
        lock = self.store.get_session_lock(session.conversation_id.value, self.namespace, self.principal)
        if lock is None:
            if session.backend_session_id is not None:
                raise AsterunError("BINDING_MISMATCH", "旧原生会话缺少插件连接锁")
            return
        current = self._snapshot(session.workspace, session.backend.value, "unused", "implementation",
                                 capability=lock["lock"].get("capability", "agent.execute"))
        if digest(self.session_lock({"binding": current})) != lock["lock_sha256"]:
            raise AsterunError("BINDING_MISMATCH", "会话绑定的插件、账户、模型或权限已改变")

    def sync_run(self, run):
        binding = self.store.binding_for_run(run.id.value, self.namespace, self.principal)
        if binding is None:
            return
        if run.status == RunStatus.PENDING_RECONCILE:
            self.store.mark_uncertain(binding["id"])
        elif run.status in TERMINAL_RUN_STATUSES:
            operation = self.app.store.find_operation_for_run(run.id)
            not_applied = (operation is not None and operation.status == OperationStatus.INTENDED
                           or self.app.control._confirmed_prompt_not_sent(run)
                           or self.app.control._confirmed_cancel_before_spawn(run))
            self.store.settle(binding["id"], actual=None, not_applied=not_applied,
                              evidence={"source": "local_lifecycle", "run_id": run.id.value,
                                        "billed_usage_verified": False})

    def control_uncertain(self, action_id):
        binding = self.store.binding_for_control(action_id, self.namespace, self.principal)
        if binding:
            self.store.mark_uncertain(binding["id"])

    def control_settle(self, action_id, not_applied=False):
        binding = self.store.binding_for_control(action_id, self.namespace, self.principal)
        if binding:
            self.store.settle(binding["id"], actual=None, not_applied=not_applied,
                              evidence={"source": "control_lifecycle", "billed_usage_verified": False})
