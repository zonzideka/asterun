"""Codex 常驻运行生命周期。原生引用先持久化，再发起下一次副作用。"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from threading import Lock

from asterun.backends.base import require_dispatch_cwd
from asterun.errors import AsterunError, BINDING_MISMATCH, CAPABILITY_UNSUPPORTED, REMOTE_STATE_UNKNOWN

APPROVAL_METHODS = {
    "item/commandExecution/requestApproval", "item/fileChange/requestApproval",
    "execCommandApproval", "applyPatchApproval",
}
TERMINALS = {"completed": "succeeded", "failed": "failed", "interrupted": "cancelled"}
DISPATCH_RECEIPT_KEY = "asterun.codex_dispatch_v1"
PRE_PROMPT_STAGES = {"preflight"}


from asterun.approval_digest import request_hash


class CodexRuntime:
    def __init__(self, backend):
        self.backend = backend
        self.sessions = {}
        self.transports = {}
        self.lock = Lock()
        self.cancelled = set()

    def _connect(self, cwd, run_key=None, stopping=None, *, scoped=False):
        from asterun.backends.codex import CodexAppServerTransport
        from asterun.backends.codex_protocol import READ_SCOPE_CONFIG, preflight_codex_read_scope

        if not self.backend.can_dispatch():
            raise self.backend.unavailable_error()
        binary = str(self.backend.discover_bin())
        if scoped:
            preflight_codex_read_scope(binary)
        transport = CodexAppServerTransport(codex_bin=binary,
            config_overrides=READ_SCOPE_CONFIG if scoped else None)
        if run_key:
            with self.lock:
                self.transports[run_key] = transport
        try:
            if stopping and stopping.is_set():
                raise RuntimeError("核心已停止")
            transport.spawn(require_dispatch_cwd(cwd))
            transport.initialize()
            return transport
        except BaseException:
            transport.close()
            if run_key:
                with self.lock:
                    self.transports.pop(run_key, None)
            raise

    @staticmethod
    def _read(transport, native, cwd, *, check_latest=False):
        thread_id = native.get("thread_id") or native.get("backend_session_id")
        if not thread_id:
            raise AsterunError(REMOTE_STATE_UNKNOWN, "缺少已持久化的原生线程引用")
        response = transport.request("thread/read", {"threadId": thread_id, "includeTurns": True})
        thread = response.get("thread") or {}
        if thread.get("id") != thread_id or Path(thread.get("cwd") or "").resolve() != cwd.resolve():
            raise AsterunError(BINDING_MISMATCH, "原生线程或工作区不匹配")
        turns = thread.get("turns") or []
        if check_latest:
            latest = turns[-1] if turns else {}
            if latest.get("status") not in {*TERMINALS, None}:
                raise AsterunError(REMOTE_STATE_UNKNOWN, "原生线程仍有未结束轮次，不能自动推进")
            if latest.get("id") != native.get("turn_id"):
                raise AsterunError(BINDING_MISMATCH, "原生最新轮次已变化，需先核对外部修改")
        return thread

    def run(self, task_id, run_id, script, text, *, cwd, native, publish, stopping, read_scope=None):
        from asterun.backends.base import BackendCall
        from asterun.backends.codex_protocol import read_scope_config, verify_read_scope_session

        binding = read_scope.binding if read_scope is not None else None
        if native.get("backend_session_id") and native.get("read_scope_binding") != binding:
            raise AsterunError(BINDING_MISMATCH, "原生线程的固定读取范围不匹配")

        self.backend.calls.append(BackendCall("dispatch", {"run_id": run_id.value}))
        key = run_id.value
        try:
            transport = self._connect(cwd, key, stopping, scoped=read_scope is not None)
        except Exception as exc:
            if read_scope is None:
                raise
            return self._scope_preflight_failure(exc, native, task_id, run_id)
        turn_requested = False
        bound_native = dict(native)
        scope_errors = []
        service_requests = None
        session = None
        try:
            thread_params = {"approvalPolicy": "untrusted", "sandbox": "workspace-write"}
            if read_scope is not None:
                thread_params = {"approvalPolicy": "never", "sandbox": "read-only",
                    "config": read_scope_config(transport, cwd)}
            if native.get("backend_session_id"):
                self._read(transport, native, cwd, check_latest=True)
                session = transport.resume_session(thread_id=native["backend_session_id"], cwd=cwd,
                    resume_params=thread_params)
                if session.session_id != native["backend_session_id"]:
                    raise AsterunError(BINDING_MISMATCH, "续接返回了不同的原生线程")
            else:
                if read_scope is not None:
                    thread_params.update({"environments": [], "dynamicTools": [read_scope.tool_spec()]})
                session = transport.create_session(cwd=cwd,
                    thread_params=thread_params)
            if read_scope is not None:
                verify_read_scope_session(transport, session)
            with self.lock:
                self.sessions[key] = session
            # 没有这次确认，不能发出 turn/start；重启时也不会猜测并重发。
            bound_native = {"thread_id": session.session_id, "backend_session_id": session.session_id}
            if read_scope is not None:
                bound_native["read_scope_binding"] = binding
                def latch_violation():
                    publish({"status": "running", "terminated": False, "approvals": [],
                        "summary": "固定读取请求失败，本轮停止",
                        "native": {**bound_native, "turn_id": session.current_turn_id,
                                   "read_scope_violation": True}})
                service_requests = self._attach_read_scope(session, read_scope, scope_errors, latch_violation)
            publish({"status": "dispatching", "native": bound_native, "terminated": False})
            if stopping.is_set():
                raise RuntimeError("核心已停止")
            with self.lock:
                cancelled = key in self.cancelled
            if cancelled:
                return {"status": "cancelled", "terminated": True, "summary": "轮次发出前已取消"}
            turn_requested = True
            transport.start_session_turn(session, text=text,
                turn_params={"environments": [], "approvalPolicy": "never"} if read_scope is not None else None)
            if self.backend.config.desktop_projects is True:
                # 原生空线程可能尚无持久历史；首轮受理后再登记，且先保存轮次引用。
                publish({"status": "running", "native": {**bound_native, "turn_id": session.current_turn_id},
                         "terminated": False})
                if stopping.is_set():
                    raise RuntimeError("核心已停止")
                presentation = self.backend._present_project(transport, cwd, session.session_id)
                bound_native["desktop_project"] = presentation
            previous = None
            scope_interrupted = False
            while not stopping.is_set():
                if service_requests is not None:
                    service_requests()
                snapshot = session.to_status_dict()
                status = snapshot["status"]
                result = {
                    "status": TERMINALS.get(status, "waiting_input" if read_scope is None and snapshot["pending_requests"] else "running"),
                    "terminated": status in TERMINALS,
                    "summary": snapshot["message"],
                    "native": {**bound_native, "turn_id": snapshot["current_turn_id"]},
                    "approvals": snapshot["pending_requests"] if read_scope is None and status not in TERMINALS else [],
                }
                if snapshot.get("token_usage"):
                    result["native"].update(token_usage=deepcopy(snapshot["token_usage"]),
                                            usage_scope="thread_cumulative", usage_source="codex_app_server")
                if scope_errors:
                    result["native"]["read_scope_violation"] = True
                    result["summary"] = "原生请求超出固定只读工具协议，本轮停止"
                    result["error_code"] = CAPABILITY_UNSUPPORTED
                    result["approvals"] = []
                if status in TERMINALS:
                    if scope_errors:
                        result["status"] = "failed"
                    result["events"] = [{"type": "run_succeeded" if status == "completed" else "run_terminated",
                                         "text": result["summary"]}]
                    if scope_errors:
                        result["events"][0]["type"] = "run_terminated"
                    return result
                if not transport.alive or status not in {"running", "waiting_input", "idle"}:
                    return {**result, "status": "pending_reconcile", "error_code": REMOTE_STATE_UNKNOWN}
                if result != previous:
                    publish(result)
                    previous = result
                if scope_errors and not scope_interrupted and snapshot["current_turn_id"]:
                    scope_interrupted = True
                    transport.interrupt_turn(session.session_id, snapshot["current_turn_id"])
                with self.lock:
                    cancelled = key in self.cancelled
                    if cancelled and snapshot["current_turn_id"]:
                        self.cancelled.discard(key)
                if cancelled and snapshot["current_turn_id"]:
                    transport.interrupt_turn(session.session_id, snapshot["current_turn_id"])
                stopping.wait(0.03)
            raise RuntimeError("观察停止，结果待对账")
        except Exception as exc:
            if read_scope is None:
                raise
            if turn_requested:
                if service_requests is not None:
                    service_requests(abandon=True)
                observed_native = {**bound_native, "turn_id": session.current_turn_id}
                if scope_errors:
                    observed_native["read_scope_violation"] = True
                return {"status": "pending_reconcile", "terminated": False,
                        "error_code": REMOTE_STATE_UNKNOWN, "native": observed_native,
                        "summary": "固定读取运行的原生结果未确认，需要对账"}
            return self._scope_preflight_failure(exc, bound_native, task_id, run_id)
        finally:
            transport.close()
            with self.lock:
                self.sessions.pop(key, None)
                self.transports.pop(key, None)
                self.cancelled.discard(key)

    @staticmethod
    def _scope_preflight_failure(error, native, task_id, run_id):
        return {"status": "failed", "terminated": True, "native": {**native, DISPATCH_RECEIPT_KEY: {
                    "task_id": task_id.value, "run_id": run_id.value, "stage": "preflight",
                    "prompt_attempted": False, "delivery": "not_sent", "failure_type": type(error).__name__}},
                "error_code": error.code if isinstance(error, AsterunError) else CAPABILITY_UNSUPPORTED,
                "summary": error.message if isinstance(error, AsterunError) else "固定只读入口预检未通过，未发送模型请求",
                "events": [{"type": "run_terminated", "request_sent": False}]}

    @staticmethod
    def _attach_read_scope(session, reader, errors, latch_violation):
        from asterun.backends.codex_protocol import build_codex_dynamic_tool_answer
        from queue import Empty, SimpleQueue
        seen = set()
        name = reader.tool_spec()["name"]
        requests = SimpleQueue()

        def handle(message, *, abandon=False):
            if abandon:
                # No reply or reader call was made. Persist uncertainty as a
                # quality failure before reconcile can accept a remote result.
                errors.append("unresolved_native_tool_request")
                return
            params = message.get("params")
            if not isinstance(params, dict):
                params = {}
            with session._lock:
                pending = session.pending_requests.get(str(message.get("id")))
                recorded = {key: message[key] for key in ("id", "method", "params") if key in message}
                valid = (pending == recorded and message.get("method") == "item/tool/call"
                    and params.get("threadId") == session.session_id
                    and bool(session.current_turn_id) and params.get("turnId") == session.current_turn_id
                    and params.get("tool") == name and params.get("namespace") is None
                    and isinstance(params.get("callId"), str) and bool(params["callId"])
                    and params["callId"] not in seen and isinstance(params.get("arguments"), dict))
            if valid:
                seen.add(params["callId"])
                try:
                    output = reader.call(params["arguments"])
                    answer = build_codex_dynamic_tool_answer(text=json.dumps(output, ensure_ascii=False))
                except Exception:
                    errors.append("bound_read_failed")
                    answer = build_codex_dynamic_tool_answer(success=False, text="固定读取请求失败；请核对限定路径与参数")
            else:
                errors.append("unsupported_native_request")
                if message.get("method") in {"execCommandApproval", "applyPatchApproval"}:
                    answer = {"decision": "denied"}
                elif message.get("method") in APPROVAL_METHODS:
                    answer = {"decision": "decline"}
                else:
                    answer = build_codex_dynamic_tool_answer(success=False, text="仅允许已绑定的固定读取工具")
            if errors:
                # Runs on the runtime worker, never the transport reader. The
                # core acknowledges durable failure BEFORE the native reply.
                latch_violation()
            with session._lock:
                session.transport.respond(message["id"], answer)
                session.pending_requests.pop(str(message["id"]), None)
                if session.status not in TERMINALS:
                    session.status = "waiting_input" if session.pending_requests else "running"

        def service(*, abandon=False):
            while True:
                try:
                    message = requests.get_nowait()
                except Empty:
                    return
                handle(message, abandon=abandon)

        session.transport.on_request(requests.put)
        return service

    def cancel(self, run_id):
        with self.lock:
            self.cancelled.add(run_id.value)
        return {"cancel_requested": True, "terminated": False}

    def respond(self, run_id, decision, request):
        with self.lock:
            session = self.sessions.get(run_id.value)
        if session is None:
            raise AsterunError(REMOTE_STATE_UNKNOWN, "原审批连接已丢失，不能向新连接重发旧审批")
        if request.get("method") not in APPROVAL_METHODS:
            raise AsterunError(CAPABILITY_UNSUPPORTED, "该原生请求需要专用输入，不能用批准/拒绝代替")
        with session._lock:
            pending = session.pending_requests.get(str(request.get("id")))
            if pending != request:
                raise AsterunError(BINDING_MISMATCH, "原生审批请求已变化或结束")
            answer = "accept" if decision == "approve" else "decline"
            if request["method"] in {"execCommandApproval", "applyPatchApproval"}:
                answer = "approved" if decision == "approve" else "denied"
            session.transport.respond(request["id"], {"decision": answer})
            session.pending_requests.pop(str(request["id"]))
            session.status = "waiting_input" if session.pending_requests else "running"
        return {"status": session.status, "terminated": False}

    def resume(self, native, cwd, *, read_scope=None):
        from asterun.backends.codex_protocol import read_scope_config, verify_read_scope_session
        scoped = native.get("read_scope_binding") is not None
        if scoped and (read_scope is None or read_scope.binding != native["read_scope_binding"]):
            raise AsterunError(BINDING_MISMATCH, "原生线程的固定读取范围不匹配")
        transport = self._connect(cwd, scoped=scoped)
        try:
            self._read(transport, native, cwd, check_latest=True)
            params = {"approvalPolicy": "never", "sandbox": "read-only",
                      "config": read_scope_config(transport, cwd)} if scoped else None
            session = transport.resume_session(thread_id=native["backend_session_id"], cwd=cwd, resume_params=params)
            if session.session_id != native["backend_session_id"]:
                raise AsterunError(BINDING_MISMATCH, "续接返回了不同的原生线程")
            if scoped:
                verify_read_scope_session(transport, session)
            report = {"native_checked": True, "native_resumed": True, "thread_id": session.session_id}
            presentation = self.backend._present_project(transport, cwd, session.session_id)
            if presentation is not None:
                report["desktop_project"] = presentation
            return report
        finally:
            transport.close()

    def reconcile(self, native, cwd):
        transport = self._connect(cwd)
        try:
            thread = self._read(transport, native, cwd)
            turn = next((item for item in thread.get("turns", []) if item.get("id") == native.get("turn_id")), None)
            if not turn or turn.get("status") not in TERMINALS:
                return {"status": "pending_reconcile", "terminated": False, "error_code": REMOTE_STATE_UNKNOWN}
            from asterun.backends.codex_output import final_text
            output = final_text(turn.get("items", []))
            violated = native.get("read_scope_violation") is True
            presentation = self.backend._present_project(transport, cwd, thread["id"])
            if presentation is not None:
                native = {**native, "desktop_project": presentation}
            return {"status": "failed" if violated else TERMINALS[turn["status"]], "terminated": True,
                    "summary": "原生请求超出固定只读工具协议" if violated else output,
                    "native": native, "events": [{"type": "reconciled", "native_checked": True}]}
        finally:
            transport.close()

    def close(self):
        with self.lock:
            transports = list(self.transports.values())
        for transport in transports:
            transport.close()
