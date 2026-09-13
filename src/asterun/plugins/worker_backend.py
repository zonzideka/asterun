"""独立 worker 到现有 Backend 的窄桥；计划、状态和输出验收仍由核心拥有。"""
from __future__ import annotations

from collections import OrderedDict
import os
from pathlib import Path
from threading import Event, Lock
import tempfile
import time

from asterun.backends.base import BackendCall, require_dispatch_cwd
from asterun.errors import AsterunError, BACKEND_UNAVAILABLE, CAPABILITY_UNSUPPORTED, INVALID_REQUEST
from asterun.plugin_api.protocol import json_copy
from asterun.plugins.worker import WorkerFailure, WorkerHost, WorkerLimits


def _identifier(value):
    return value.value if hasattr(value, "value") else str(value)


def _text(value, *, nullable=False):
    if nullable and value is None:
        return None
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value if c not in "\n\t"):
        raise ValueError("invalid provider text")
    return value


def _reference(value):
    if not isinstance(value, str) or not value or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise ValueError("invalid provider reference")
    return value


def _remember_references(reply, context):
    # 响应其余部分损坏时仍保留已经拿到的精确上游引用，供核心持久对账。
    for key in ("provider_job_ref", "provider_session_id", "provider_turn_id"):
        if reply.result.get(key) is not None:
            try:
                context[key] = _reference(reply.result[key])
            except ValueError:
                continue


def _events(value):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 128:
        raise ValueError("invalid provider events")
    result = []
    for event in value:
        if not isinstance(event, dict):
            raise ValueError("invalid provider event")
        item = {}
        for key in ("provider_event_id", "category", "summary", "observed_at", "provider_job_ref",
                    "provider_session_id", "provider_turn_id"):
            if key in event:
                item[key] = _text(event[key], nullable=True)
        if "provider_sequence" in event:
            sequence = event["provider_sequence"]
            if sequence is not None and (type(sequence) is not int or not 0 <= sequence <= 9007199254740991):
                raise ValueError("invalid provider sequence")
            item["provider_sequence"] = sequence
        if item:
            result.append(item)
    return result


def _usage(value):
    if value is None:
        return []
    values = [value] if isinstance(value, dict) else value
    if not isinstance(values, list) or len(values) > 32:
        raise ValueError("invalid provider usage")
    result = []
    for raw in values:
        if not isinstance(raw, dict):
            raise ValueError("invalid provider usage")
        item = {key: raw[key] for key in ("pool_ref", "meter", "used", "scope", "scope_ref", "cursor",
                "observed_at", "window_id", "sequence", "cumulative") if key in raw}
        if "used" not in item and "amount" in raw:
            item["used"] = raw["amount"]
        result.append(item)
    return json_copy(result)


class WorkerBackend:
    def __init__(self, registry, registration, backend_config, connection, provider_account=None,
                 *, limits: WorkerLimits | None = None, credential_resolver=None):
        self.registry = registry
        self.registration = registration
        self.config = backend_config
        self.connection = json_copy(connection)
        self.provider_account = json_copy(provider_account or {})
        self.host = WorkerHost(registry, registration, limits=limits)
        self.credential_resolver = credential_resolver
        self.calls = []
        self._dispatch_count = 0
        self._bound = OrderedDict()
        self._lock = Lock()

    @property
    def dispatch_count(self):
        return self._dispatch_count

    def _record_call(self, method, run_id=None):
        with self._lock:
            self.calls.append(BackendCall(method, {} if run_id is None else {"run_id": _identifier(run_id)}))
            del self.calls[:-128]

    def can_dispatch(self):
        return bool(self.config.enabled and self.connection.get("enabled") and
                    self.registry.get(self.registration.plugin_id).enabled)

    @property
    def supports_native_resume(self):
        return any(item["name"] == "agent.resume" and item["support"] in {"offline_verified", "supported"}
                   for item in self.registration.manifest.to_dict()["capabilities"])

    def check_native_resume(self, run_id, native, cwd):
        if not self.supports_native_resume:
            raise AsterunError(CAPABILITY_UNSUPPORTED, "插件没有声明已接线的原生续接")
        context, supplier = self._binding(run_id)
        if (context.get("provider_session_id") != native.get("backend_session_id")
                or context.get("provider_turn_id") != native.get("turn_id")):
            raise AsterunError("BINDING_MISMATCH", "原生续接引用与原运行绑定不一致")
        reply = self._rpc("execution.reconcile", {}, {**context, "resume_check": True}, supplier, cwd=require_dispatch_cwd(cwd))
        result = self._normalize(reply, context)
        if (result["status"] not in {"succeeded", "failed", "cancelled"}
                or result["native"].get("provider_session_id") != native["backend_session_id"]
                or result["native"].get("provider_turn_id") != native["turn_id"]):
            raise AsterunError("REMOTE_STATE_UNKNOWN", "未核验原生确切轮次，不能续接")
        return {"native_checked": True, "native_resumed": False, "resume_ready": True,
                "message": "原生引用已核验；下一次 task.submit 将重放原权限快照后续接"}

    def unavailable_error(self):
        return AsterunError(BACKEND_UNAVAILABLE, "插件或连接未启用，未派发 worker")

    def inspect(self):
        self._record_call("inspect")
        return {"backend": self.config.name, "kind": self.config.kind,
                "plugin_id": self.registration.plugin_id, "enabled_in_config": self.config.enabled,
                "execution_enabled": self.can_dispatch(), "is_real_connection": False,
                "authentication": "not_verified", "connection_display": "none",
                "capabilities": self.registration.manifest.to_dict()["capabilities"],
                "permission_enforcement": "advisory", "isolation": "trusted_code_process_isolation_only",
                "resume_verified": False, "native_visible_verified": False,
                "message": "静态插件状态；未启动 worker、读取凭据或验证供应商账户"}

    def bind_execution(self, run_id, context, *, credentials=None):
        context = json_copy(context)
        identifier = _identifier(run_id)
        if not isinstance(context, dict) or context.get("run_id") != identifier:
            raise AsterunError(INVALID_REQUEST, "worker 上下文必须绑定当前运行")
        if any(not isinstance(context.get(key), str) or not context[key]
               for key in ("plan_id", "binding_id", "task_id", "capability")):
            raise AsterunError(INVALID_REQUEST, "worker 上下文缺少核心持久执行绑定")
        if credentials is not None and not callable(credentials):
            raise AsterunError(INVALID_REQUEST, "凭据必须通过最终调用前的 supplier 取得")
        with self._lock:
            self._bound[identifier] = (context, credentials)
            self._bound.move_to_end(identifier)
            while len(self._bound) > 128:
                self._bound.popitem(last=False)

    def _binding(self, run_id):
        with self._lock:
            value = self._bound.get(_identifier(run_id))
        if value is None:
            raise AsterunError(INVALID_REQUEST, "worker 执行需要核心先绑定已持久计划")
        return json_copy(value[0]), value[1]

    def _resolve(self, supplier, method):
        if supplier is not None:
            return supplier()
        if method not in {"execution.start", "execution.observe", "execution.reconcile", "execution.cancel"}:
            from asterun.connections.credentials import ResolvedCredentials
            return ResolvedCredentials()
        if self.credential_resolver is not None:
            return self.credential_resolver(self.connection, self.provider_account, self.registration.manifest,
                                            authorized=True, environment=os.environ, operation=method)
        from asterun.connections.credentials import resolve_credentials
        return resolve_credentials(self.connection, self.provider_account, self.registration.manifest,
                                   authorized=True, environment=os.environ, operation=method)

    def _rpc(self, method, input, context, supplier, *, cwd, stopping=None, deadline=None):
        # 先核验禁用和实际安装字节，再解析秘密；worker拿不到核心对象或数据库。
        self.host._verify(method)
        credentials = self._resolve(supplier, method)
        environment = {}
        try:
            environment = dict(credentials.env)
            if credentials.native_home is not None:
                environment["HOME"] = str(credentials.native_home)
            reply = self.host.call(method, input, context, cwd=cwd, environment=environment,
                                   secrets=credentials.redactions, stopping=stopping,
                                   timeout_seconds=None if deadline is None else deadline - time.monotonic())
            return reply
        finally:
            environment.clear()
            credentials.clear()

    def verify_connection(self):
        self._record_call("verify_connection")
        if not self.can_dispatch():
            raise self.unavailable_error()
        # 此显式方法只接受供应商自报，不把自报升级为核心认证或调用证明。
        with tempfile.TemporaryDirectory(prefix="asterun-plugin-inspect-") as cwd:
            reply = self._rpc("connection.inspect", {}, {"connection_ref": self.config.connection_ref}, None, cwd=cwd)
        reported = reply.result.get("authenticated")
        if reported not in (True, False, "not_required", "unknown", "not_verified", "authenticated"):
            reported = "unknown"
        return {"backend": self.config.name, "plugin_id": self.registration.plugin_id,
                "authenticated": reported, "verified": False, "source": "untrusted_provider_observation",
                "is_real_connection": False, "stderr_bytes": reply.stderr_bytes}

    def _normalize(self, reply, context):
        raw = reply.result
        status = raw.get("status")
        if status in {"unknown", "requested", "acknowledged"}:
            status = "pending_reconcile"
        if status not in {"succeeded", "failed", "running", "waiting_input", "waiting_auth", "pending_reconcile", "cancelled"}:
            raise ValueError("invalid execution status")
        if status == "cancelled" and raw.get("terminated_verified") is not True:
            status = "pending_reconcile"
        if status == "succeeded" and "output" not in raw:
            raise ValueError("missing typed output")
        native = {"plugin_id": self.registration.plugin_id}
        for key in ("provider_job_ref", "provider_session_id", "provider_turn_id"):
            value = raw.get(key, context.get(key))
            if value is not None:
                native[key] = _reference(value)
        result = {"status": status, "terminated": status in {"succeeded", "failed", "cancelled"},
                  "summary": "插件执行结束" if status == "succeeded" else "插件运行状态已观察",
                  "native": native, "plugin_events": _events(raw.get("events")), "usage": _usage(raw.get("usage")),
                  "stderr_bytes": reply.stderr_bytes}
        if "output" in raw:
            result["output"] = json_copy(raw["output"])
            if context["capability"] == "agent.execute" and isinstance(raw["output"], dict) and isinstance(raw["output"].get("text"), str):
                result["summary"] = raw["output"]["text"]
        if status == "pending_reconcile":
            result["error_code"] = "REMOTE_STATE_UNKNOWN"
        elif status == "failed":
            result["error_code"] = "PLUGIN_EXECUTION_FAILED"
        return result

    def _failure(self, error, *, attempted, context):
        sent = attempted or isinstance(error, WorkerFailure) and error.request_sent
        result = {"status": "pending_reconcile" if sent else "failed", "terminated": not sent,
                  "error_code": "REMOTE_STATE_UNKNOWN" if sent else BACKEND_UNAVAILABLE,
                  "summary": "插件调用结果未确认，需要对账" if sent else "插件启动前校验失败，未发送执行请求",
                  "plugin_events": [], "usage": [],
                  "native": {"plugin_id": self.registration.plugin_id,
                             **{key: context[key] for key in ("provider_job_ref", "provider_session_id", "provider_turn_id")
                                if context.get(key) is not None}}}
        if isinstance(error, WorkerFailure):
            result["worker_failure"] = error.reason
            result["stderr_bytes"] = error.stderr_bytes
        return result

    def dispatch(self, task_id, run_id, script, text, *, cwd=None):
        return self.dispatch_capability(task_id, run_id, "agent.execute", {"text": text}, cwd=cwd)

    def dispatch_stream(self, task_id, run_id, script, text, *, cwd=None, native=None, publish, stopping):
        return self.dispatch_capability_stream(task_id, run_id, "agent.execute", {"text": text},
                                               cwd=cwd, publish=publish, stopping=stopping)

    def dispatch_capability(self, task_id, run_id, capability, input, *, cwd=None):
        return self.dispatch_capability_stream(task_id, run_id, capability, input, cwd=cwd,
                                               publish=lambda result: None, stopping=Event())

    def dispatch_capability_stream(self, task_id, run_id, capability, input, *, cwd=None, publish, stopping):
        context, supplier = self._binding(run_id)
        if context["task_id"] != _identifier(task_id) or context["capability"] != capability:
            raise AsterunError(INVALID_REQUEST, "worker 的任务或能力与固定计划不一致")
        if not self.can_dispatch():
            raise self.unavailable_error()
        input = self.registration.manifest.validate_input(capability, input)
        cwd = require_dispatch_cwd(cwd)
        self._record_call("dispatch", run_id)
        self._dispatch_count += 1
        attempted = False
        deadline = time.monotonic() + self.host.limits.timeout_seconds
        try:
            reply = self._rpc("execution.start", input, context, supplier, cwd=cwd, stopping=stopping, deadline=deadline)
            attempted = True
            while True:
                _remember_references(reply, context)
                result = self._normalize(reply, context)
                context.update({key: value for key, value in result["native"].items() if key != "plugin_id"})
                if result["terminated"] or result["status"] in {"pending_reconcile", "waiting_input", "waiting_auth"}:
                    return result
                publish(result)  # 现有 Executor 在核心持久确认前阻塞，保持有界背压。
                if stopping.wait(min(0.05, max(0.0, deadline - time.monotonic()))) or time.monotonic() >= deadline:
                    raise WorkerFailure("observation_timeout", request_sent=True)
                reply = self._rpc("execution.observe", {}, context, supplier, cwd=cwd, stopping=stopping, deadline=deadline)
        except (AsterunError, ValueError, TypeError) as error:
            # start 已发送后，观察断流、插件错误或格式错误均不能推断远端失败。
            return self._failure(error, attempted=attempted, context=context)

    def request_cancel(self, run_id, script=""):
        context, supplier = self._binding(run_id)
        self._record_call("request_cancel", run_id)
        try:
            reply = self._rpc("execution.cancel", {}, context, supplier, cwd=require_dispatch_cwd(Path(context["workspace_root"])))
            _remember_references(reply, context)
            raw = reply.result
            if raw.get("terminated_verified") is True and raw.get("status") == "cancelled":
                return {**self._normalize(reply, context), "cancel_accepted": True}
            return {"cancel_accepted": raw.get("status") in {"requested", "acknowledged", "running"},
                    "terminated": False, "status": "pending_reconcile", "error_code": "REMOTE_STATE_UNKNOWN",
                    "summary": "取消仅为请求；远端是否终止尚未确认", "plugin_events": _events(raw.get("events")),
                    "usage": _usage(raw.get("usage")), "stderr_bytes": reply.stderr_bytes}
        except (AsterunError, ValueError, TypeError) as error:
            return self._failure(error, attempted=True, context=context)

    def reconcile_execution(self, run_id, *, cwd):
        context, supplier = self._binding(run_id)
        self._record_call("reconcile", run_id)
        try:
            reply = self._rpc("execution.reconcile", {}, context, supplier, cwd=require_dispatch_cwd(cwd))
            _remember_references(reply, context)
            return self._normalize(reply, context)
        except (AsterunError, ValueError, TypeError) as error:
            return self._failure(error, attempted=True, context=context)

    def respond_approval(self, run_id, decision):
        raise AsterunError(CAPABILITY_UNSUPPORTED, "M1 外部插件尚未接入原生审批桥接")

    def close(self):
        with self._lock:
            self._bound.clear()
