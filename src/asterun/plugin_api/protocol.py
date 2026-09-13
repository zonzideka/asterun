"""有版本的 worker 消息；传输、授权和生命周期由核心负责。"""

from __future__ import annotations

import json
from typing import Any, Protocol

from asterun.errors import AsterunError, INVALID_REQUEST

PLUGIN_API_MAJOR = 1
WORKER_PROTOCOL_VERSION = "asterun-worker/v1"
WORKER_METHODS = frozenset({
    "plugin.describe", "connection.inspect", "capability.prepare", "execution.start",
    "execution.observe", "execution.cancel", "execution.reconcile", "usage.observe",
})


class CapabilityProvider(Protocol):
    """插件实现一次操作；不能持有核心 Store、签发 Grant 或推进工作流。"""

    def prepare(self, capability: str, input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]: ...
    def start(self, capability: str, input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]: ...
    def observe(self, input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]: ...
    def cancel(self, input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]: ...
    def reconcile(self, input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]: ...


def _reject(message: str) -> None:
    raise AsterunError(INVALID_REQUEST, message)


def json_copy(value: Any) -> Any:
    """拒绝 Python 非 JSON 值、非有限数和过深输入，返回独立副本。"""
    def check(item, depth=0):
        if depth > 64:
            _reject("插件 JSON 嵌套超过 64 层")
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                _reject("插件 JSON 对象的键必须是字符串")
            for child in item.values():
                check(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                check(child, depth + 1)
        elif item is not None and type(item) not in {str, int, float, bool}:
            _reject("插件输入必须为 JSON 值")
    check(value)
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        encoded.encode("utf-8")
        return json.loads(encoded)
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise AsterunError(INVALID_REQUEST, "插件 JSON 值无效") from exc


def _id(value: Any) -> bool:
    return (isinstance(value, str) and bool(value) and len(value) <= 256) or type(value) is int


def validate_worker_request(value: Any) -> dict[str, Any]:
    value = json_copy(value)
    if not isinstance(value, dict) or set(value) != {"jsonrpc", "id", "method", "params"}:
        _reject("worker request 必须包含且仅包含 jsonrpc/id/method/params")
    if value["jsonrpc"] != "2.0" or not _id(value["id"]):
        _reject("worker JSON-RPC 版本或 request id 无效")
    if not isinstance(value["method"], str) or value["method"] not in WORKER_METHODS:
        _reject("worker 方法未注册")
    params = value["params"]
    if not isinstance(params, dict) or set(params) - {"protocol_version", "input", "context"}:
        _reject("worker params 只接受 protocol_version/input/context")
    if params.get("protocol_version") != WORKER_PROTOCOL_VERSION:
        _reject("worker 协议版本不兼容")
    if any(name in params and not isinstance(params[name], dict) for name in ("input", "context")):
        _reject("worker input/context 必须是对象")
    return value


def validate_worker_response(value: Any, *, request_id: str | int | None = None) -> dict[str, Any]:
    value = json_copy(value)
    if not isinstance(value, dict) or set(value) not in (
        {"jsonrpc", "id", "result"}, {"jsonrpc", "id", "error"},
    ):
        _reject("worker response 必须返回 result 或 error，不能同时返回")
    if value["jsonrpc"] != "2.0" or not _id(value["id"]):
        _reject("worker JSON-RPC 版本或 response id 无效")
    if request_id is not None and (type(value["id"]) is not type(request_id) or value["id"] != request_id):
        _reject("worker response id 与 request 不一致")
    if "result" in value:
        result = value["result"]
        if not isinstance(result, dict) or result.get("protocol_version") != WORKER_PROTOCOL_VERSION:
            _reject("worker result 缺少兼容的 protocol_version")
    else:
        error = value["error"]
        if not isinstance(error, dict) or set(error) - {"code", "message", "data"}:
            _reject("worker error 结构无效")
        if type(error.get("code")) is not int or not isinstance(error.get("message"), str):
            _reject("worker error 需要整数 code 和字符串 message")
        if "data" in error and not isinstance(error["data"], dict):
            _reject("worker error.data 必须为对象")
        if "protocol_version" in error.get("data", {}) and error["data"]["protocol_version"] != WORKER_PROTOCOL_VERSION:
            _reject("worker error 协议版本不兼容")
    return value
