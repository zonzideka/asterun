"""CLI、MCP 共用的请求契约；同一份 schema 同时用于发现和入参检查。"""

from __future__ import annotations

from typing import Any

from asterun.errors import INVALID_REQUEST, AsterunError


def obj(properties=None, required=()):
    return {"type": "object", "properties": properties or {}, "required": list(required), "additionalProperties": False}


STRING = {"type": "string"}
IDENTIFIER = {"type": "string", "minLength": 1}
REVISION = {"type": "integer", "minimum": 1}
TASK = {"task_id": IDENTIFIER}
JSON_OBJECT = {"type": "object", "additionalProperties": True}
CHECK = obj({
    "kind": {"type": "string", "enum": ["contains", "file_exists", "file_contains", "run_succeeded"]},
    "text": STRING, "path": IDENTIFIER,
}, ["kind"])

REQUEST_SCHEMAS = {
    "review.status": obj({"review_id": IDENTIFIER}, ["review_id"]),
    "review.inbox": obj({"consumer_id": {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,64}$"},
        "after": {"type": "string", "pattern": "^rev_[0-9a-f]{32}$"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ["consumer_id"]),
    "review.ack": obj({"review_id": IDENTIFIER, "consumer_id": {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,64}$"},
        "notification_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}},
        ["review_id", "consumer_id", "notification_sha256"]),
    "plugin.list": obj(),
    "plugin.inspect": obj({"plugin_id": IDENTIFIER}, ["plugin_id"]),
    "plugin.enable": obj({"plugin_id": IDENTIFIER}, ["plugin_id"]),
    "plugin.disable": obj({"plugin_id": IDENTIFIER}, ["plugin_id"]),
    "plugin.register": obj({"plugin_id": IDENTIFIER, "registration": JSON_OBJECT,
        "output": IDENTIFIER, "backup": IDENTIFIER, "expected_source_sha256": IDENTIFIER},
        ["plugin_id", "registration", "output", "backup", "expected_source_sha256"]),
    "connection.setup": obj({"connection_ref": IDENTIFIER}, ["connection_ref"]),
    "usage.inspect": obj({"pool_ref": IDENTIFIER, "meter": IDENTIFIER, "window_id": IDENTIFIER}),
    "capability.invoke": obj({"workspace": IDENTIFIER, "backend": IDENTIFIER, "capability": IDENTIFIER,
        "input": JSON_OBJECT, "idempotency_key": IDENTIFIER, "expected_revision": REVISION},
        ["workspace", "backend", "capability", "input", "idempotency_key"]),
    "control.discovery": obj(),
    "control.resources": obj(),
    "control.command": obj({"command": {"type": "object"}}, ["command"]),
    "control.get": obj({"kind": IDENTIFIER, "id": IDENTIFIER}, ["kind", "id"]),
    "control.events": obj({"task_id": IDENTIFIER, "after": {"type": "integer", "minimum": 0, "maximum": 9007199254740991},
                           "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ["task_id"]),
    "control.upload": obj({"task_id": IDENTIFIER, "text": STRING}, ["task_id", "text"]),
    "control.content": obj({"artifact_id": IDENTIFIER}, ["artifact_id"]),
    "config.validate": obj(),
    "config.apply": obj({"expected_revision": REVISION}, ["expected_revision"]),
    "backend.inspect": obj({"backend": IDENTIFIER}),
    "connection.verify": obj({"backend": IDENTIFIER}),
    "task.submit": obj({
        "workspace": IDENTIFIER, "text": STRING, "path": IDENTIFIER, "script": IDENTIFIER,
        "backend": IDENTIFIER, "expected_revision": REVISION,
        "idempotency_key": IDENTIFIER, "conversation_id": IDENTIFIER,
        "read_scope": obj({"manifest_path": IDENTIFIER, "manifest_sha256": IDENTIFIER},
                          ["manifest_path", "manifest_sha256"]),
    }, ["workspace"]),
    "task.get": obj(TASK, ["task_id"]),
    "task.present": obj(TASK, ["task_id"]),
    "task.events": obj({
        **TASK, "cursor": {"type": "integer", "minimum": 0},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 500},
    }, ["task_id"]),
    "task.cancel": obj(TASK, ["task_id"]),
    "approval.respond": obj({
        "approval_id": IDENTIFIER, "decision": {"type": "string", "enum": ["approve", "deny"]},
        "expected_run_id": IDENTIFIER, "expected_task_id": IDENTIFIER, "expected_target_hash": IDENTIFIER,
    }, ["approval_id", "decision"]),
    "session.resume": obj({
        "conversation_id": STRING, "session_id": STRING, "backend": IDENTIFIER,
        "workspace": IDENTIFIER, "expected_revision": REVISION,
        "native": {"type": "boolean"},
    }),
    "task.handoff": obj({
        **TASK, "action": {"type": "string", "enum": ["pause", "resume"]},
        "external_change": {"type": "boolean"},
    }, ["task_id", "action"]),
    "task.reconcile": obj(TASK),
    "workflow.evaluate": obj({
        **TASK, "expected_revision": REVISION, "expected_input_hash": IDENTIFIER,
        "require_review": {"type": "boolean"}, "review_backend": IDENTIFIER,
        "checks": {"type": "array", "items": CHECK},
    }, ["task_id"]),
    "workflow.repair": obj({
        **TASK, "max_repairs": {"type": "integer", "minimum": 0}, "idempotency_key": IDENTIFIER,
    }, ["task_id"]),
    "workflow.start": obj({
        **TASK, "idempotency_key": IDENTIFIER,
        "target_paths": {"type": "array", "items": IDENTIFIER},
        "checks": {"type": "array", "items": CHECK}, "auto_repair": {"type": "boolean"},
    }, ["task_id", "idempotency_key", "target_paths", "checks"]),
    "scheduler.status": obj(), "diagnose": obj(),
    "state.backup": obj({"output": IDENTIFIER, "path": IDENTIFIER}),
    "state.restore": obj({"input": IDENTIFIER, "path": IDENTIFIER}),
}

APPROVAL_BINDING = {"approval_id": IDENTIFIER, "expected_task_id": IDENTIFIER,
                    "expected_run_id": IDENTIFIER, "expected_target_hash": IDENTIFIER}
REQUEST_SCHEMAS["approval.assess"] = obj(APPROVAL_BINDING, APPROVAL_BINDING)
REQUEST_SCHEMAS["approval.apply-policy"] = obj(APPROVAL_BINDING, APPROVAL_BINDING)
REQUEST_SCHEMAS["approval.batch"] = obj({"items": {
    "type": "array", "minItems": 1, "maxItems": 100,
    "items": obj({**APPROVAL_BINDING, "decision": {"type": "string", "enum": ["approve", "deny", "policy"]}},
                 [*APPROVAL_BINDING, "decision"]),
}}, ["items"])

# tools/list 公布的信封与实际 Draft 2020-12 校验使用同一冻结定义。
from asterun.control_protocol import _schema

REQUEST_SCHEMAS["control.command"] = {
    **obj({"command": {"$ref": "#/$defs/Command"}}, ["command"]),
    "$defs": _schema()["$defs"],
}


from importlib.resources import files as _files
import json as _json
_capability_schema = _json.loads(_files("asterun.schemas").joinpath("asterun-capability-v1.schema.json").read_text())
REQUEST_SCHEMAS["capability.discovery"] = obj()
for _method in ("prepare", "submit", "get"):
    REQUEST_SCHEMAS["capability." + _method] = _capability_schema["$defs"][_method]


def validate_request(method: str, payload: Any) -> dict[str, Any]:
    if method not in REQUEST_SCHEMAS:
        raise AsterunError(INVALID_REQUEST, f"未知方法：{method}")
    if method in {"capability.prepare", "capability.submit", "capability.get"}:
        from jsonschema import Draft202012Validator
        from asterun.plugin_api.protocol import json_copy
        payload = json_copy(payload)
        errors = list(Draft202012Validator(REQUEST_SCHEMAS[method]).iter_errors(payload))
        if errors:
            raise AsterunError(INVALID_REQUEST, "能力 profile 请求不符合独立 schema")
        if len(_json.dumps(payload).encode()) > 256 * 1024:
            raise AsterunError(INVALID_REQUEST, "能力请求超过 256 KiB")
        return payload
    if method == "control.command":
        from asterun.control_protocol import validate

        if not isinstance(payload, dict) or set(payload) != {"command"}:
            raise AsterunError(INVALID_REQUEST, "control.command 需要且只接受 command 信封")
        validate(payload["command"], "Command")
        return dict(payload)
    _validate(payload, REQUEST_SCHEMAS[method], method)
    return dict(payload)


def _validate(value: Any, schema: dict, location: str) -> None:
    expected = schema["type"]
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool}
    if not isinstance(value, types[expected]) or (expected == "integer" and isinstance(value, bool)):
        raise AsterunError(INVALID_REQUEST, f"{location} 必须符合 {expected} 类型")
    if "enum" in schema and value not in schema["enum"]:
        raise AsterunError(INVALID_REQUEST, f"{location} 的取值无效")
    if expected == "object":
        if schema.get("additionalProperties") is True:
            import json
            from asterun.plugin_api.protocol import json_copy
            copied = json_copy(value)
            if len(json.dumps(copied, ensure_ascii=False).encode()) > 512 * 1024:
                raise AsterunError(INVALID_REQUEST, "结构化输入超过 512 KiB")
            return
        properties = schema["properties"]
        missing = set(schema["required"]) - value.keys()
        extra = value.keys() - properties.keys()
        if missing or extra:
            raise AsterunError(INVALID_REQUEST, f"{location} 的字段不符合契约", details={
                "missing": sorted(missing), "unknown": sorted(extra),
            })
        for name, item in value.items():
            _validate(item, properties[name], f"{location}.{name}")
    elif expected == "array":
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", len(value)):
            raise AsterunError(INVALID_REQUEST, f"{location} 项数超出允许范围")
        for index, item in enumerate(value):
            _validate(item, schema["items"], f"{location}[{index}]")
    elif expected == "integer":
        if value < schema.get("minimum", value) or value > schema.get("maximum", value):
            raise AsterunError(INVALID_REQUEST, f"{location} 超出允许范围")
    elif expected == "string" and len(value) < schema.get("minLength", 0):
        raise AsterunError(INVALID_REQUEST, f"{location} 不得为空")
