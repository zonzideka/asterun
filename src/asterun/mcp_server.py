from __future__ import annotations

import json
import sys
from queue import Empty, Full, Queue
from threading import Event as ThreadEvent, Thread
from typing import Any, TextIO

from asterun.application import Application
from asterun import __version__
from asterun.control_protocol import loads
from asterun.errors import INVALID_REQUEST, AsterunError
from asterun.requests import REQUEST_SCHEMAS

TOOL_METHODS = {
    "review_status": "review.status",
    "review_inbox": "review.inbox",
    "review_ack": "review.ack",
    **{name.replace(".", "_"): name for name in (
        "plugin.list", "plugin.inspect", "plugin.register", "plugin.enable", "plugin.disable",
        "connection.setup", "usage.inspect", "capability.invoke", "capability.discovery", "capability.prepare", "capability.submit", "capability.get")},
    "control_upload": "control.upload",
    "control_content": "control.content",
    "config_validate": "config.validate",
    "config_apply": "config.apply",
    "backend_inspect": "backend.inspect",
    "connection_verify": "connection.verify",
    "task_submit": "task.submit",
    "task_get": "task.get",
    "usage_report": "usage.report",
    "task_events": "task.events",
    "task_cancel": "task.cancel",
    "approval_respond": "approval.respond",
    "approval_assess": "approval.assess",
    "approval_apply_policy": "approval.apply-policy",
    "approval_batch": "approval.batch",
    "session_resume": "session.resume",
    "task_handoff": "task.handoff",
    "task_reconcile": "task.reconcile",
    "task_present": "task.present",
    "workflow_evaluate": "workflow.evaluate",
    "workflow_snapshot": "workflow.snapshot",
    "workflow_repair": "workflow.repair",
    "workflow_start": "workflow.start",
    "scheduler_status": "scheduler.status",
    "diagnose": "diagnose",
    "state_backup": "state.backup",
    "state_restore": "state.restore",
    "control_discovery": "control.discovery",
    "control_command": "control.command",
    "control_get": "control.get",
    "control_events": "control.events",
    "control_resources": "control.resources",
}


def _write_message(stream: TextIO, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False)
    stream.write(body + "\n")
    stream.flush()


def _read_message(stream: TextIO) -> dict[str, Any] | None:
    line = stream.readline(1024 * 1024 + 1)
    if line == "":
        return None
    if len(line) > 1024 * 1024:
        while line and not line.endswith("\n"):
            line = stream.readline(65536)
        raise AsterunError(INVALID_REQUEST, "MCP 请求超过大小上限")
    try:
        message = loads(line)
    except AsterunError as error:
        # JSON-RPC 保持标准 parse error；不把重复键等歧义输入交给应用。
        raise json.JSONDecodeError(error.message, "", 0) from error
    if message is None:
        raise AsterunError(INVALID_REQUEST, "请求不能是 null")
    return message


def handle_rpc(app: Application, message: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(message, dict):
        return _rpc_error(None, -32600, "请求必须是 JSON-RPC 对象")
    rpc_id = message.get("id")
    method = message.get("method")
    if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return _rpc_error(rpc_id, -32600, "无效的 JSON-RPC 请求")
    # 通知不产生响应，也不允许无 id 的 tools/call 执行副作用。
    if "id" not in message:
        return {}
    if isinstance(rpc_id, bool) or not isinstance(rpc_id, (int, str)):
        return _rpc_error(None, -32600, "请求 id 必须是字符串或整数")
    params = message.get("params", {})
    if not isinstance(params, dict):
        return _rpc_error(rpc_id, -32602, "params 必须是对象")
    try:
        if method == "initialize":
            result = {
                "protocolVersion": params.get("protocolVersion")
                if isinstance(params.get("protocolVersion"), str)
                and params["protocolVersion"] in {"2024-11-05", "2025-03-26", "2025-06-18"}
                else "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "asterun", "version": __version__},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": name,
                        "description": f"Asterun {semantic}",
                        "inputSchema": REQUEST_SCHEMAS[semantic],
                    }
                    for name, semantic in TOOL_METHODS.items()
                ]
            }
        elif method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str) or name not in TOOL_METHODS:
                return _rpc_error(rpc_id, -32602, "未知 MCP 工具")
            arguments = params.get("arguments", {})
            if not isinstance(arguments, dict):
                return _rpc_error(rpc_id, -32602, "arguments 必须是对象")
            envelope = app.handle(TOOL_METHODS[name], arguments)
            result = {
                "content": [{"type": "text", "text": json.dumps(envelope.to_dict(), ensure_ascii=False)}],
                "isError": not envelope.ok,
            }
        elif method == "notifications/initialized":
            return {}
        else:
            return _rpc_error(rpc_id, -32601, f"未知 JSON-RPC 方法：{method}")
        return {"jsonrpc": "2.0", "id": rpc_id, "result": result}
    except AsterunError as error:
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": -32000, "message": error.message, "data": error.to_dict()},
        }


def _rpc_error(rpc_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def serve(app: Application, stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
    incoming = stdin or sys.stdin
    outgoing = stdout or sys.stdout
    messages = Queue(maxsize=32)
    stop = ThreadEvent()

    def read():
        while not stop.is_set():
            unreadable = False
            try:
                item = _read_message(incoming)
            except (json.JSONDecodeError, AsterunError) as error:
                item = error
            except (UnicodeError, OSError):
                item = json.JSONDecodeError("无法读取 UTF-8 MCP 请求", "", 0)
                unreadable = True
            while not stop.is_set():
                try:
                    messages.put(item, timeout=0.03)
                    break
                except Full:
                    pass
            if item is None:
                return
            if unreadable:
                while not stop.is_set():
                    try:
                        messages.put(None, timeout=0.03)
                        return
                    except Full:
                        pass

    reader = Thread(target=read, name="asterun-mcp-input", daemon=True)
    reader.start()
    try:
        while True:
            getattr(app, "poll", lambda: None)()
            try:
                message = messages.get(timeout=0.03)
            except Empty:
                continue
            if isinstance(message, json.JSONDecodeError):
                _write_message(outgoing, _rpc_error(None, -32700, "无效的 JSON"))
                continue
            if isinstance(message, AsterunError):
                _write_message(outgoing, _rpc_error(None, -32600, message.message))
                continue
            if message is None:
                return
            response = handle_rpc(app, message)
            if response:
                _write_message(outgoing, response)
    finally:
        stop.set()
        app.close()
        reader.join(timeout=0.1)
