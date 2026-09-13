"""严格静态 manifest 和能力 JSON Schema；禁止网络 schema 解析。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from jsonschema import Draft202012Validator, SchemaError

from asterun.errors import AsterunError, CAPABILITY_UNSUPPORTED, INVALID_CONFIG, INVALID_REQUEST
from .protocol import PLUGIN_API_MAJOR, json_copy

MANIFEST_VERSION = "asterun-plugin/v1"
MAX_MANIFEST_BYTES = 256 * 1024
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_TOP = {
    "schema_version", "plugin_id", "plugin_version", "plugin_api_major", "distribution",
    "entry_point", "roles", "capabilities", "auth_modes", "billing_modes", "permissions",
    "platforms", "docs", "extensions",
}
_REQUIRED = _TOP - {"extensions"}
_CAP = {"name", "input_schema", "output_schema", "support", "effect_class", "permission_requirements", "extensions"}


def _invalid(message: str) -> None:
    raise AsterunError(INVALID_CONFIG, message)


def _mapping(value, where, allowed=None, required=()):
    if not isinstance(value, dict):
        _invalid(f"{where} 必须为对象")
    if allowed is not None and set(value) - allowed:
        _invalid(f"{where} 含有未知字段")
    if set(required) - set(value):
        _invalid(f"{where} 缺少必需字段")


def _text(value, where, *, identifier=False):
    if not isinstance(value, str) or not value.strip() or value != value.strip() or any(ord(c) < 32 for c in value):
        _invalid(f"{where} 必须为非空字符串")
    if identifier and not _ID.fullmatch(value):
        _invalid(f"{where} 不是有效标识")


def _strings(value, where, *, nonempty=False):
    if not isinstance(value, list) or (nonempty and not value):
        _invalid(f"{where} 必须为字符串数组")
    for item in value:
        _text(item, where)
    if len(set(value)) != len(value):
        _invalid(f"{where} 不能有重复值")


def _extensions(value, where):
    _mapping(value, where)
    if any(not _ID.fullmatch(key) or "." not in key for key in value):
        _invalid(f"{where} 扩展键必须有命名空间")


def _schema(schema, where):
    if not isinstance(schema, dict):
        _invalid(f"{where} 必须为 JSON Schema 对象")
    def walk(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"$ref", "$dynamicRef", "$recursiveRef"}:
                    if not isinstance(child, str) or not child.startswith("#"):
                        _invalid(f"{where} 禁止外部 schema 引用")
                if key == "$id" and (not isinstance(child, str) or child):
                    _invalid(f"{where} 不接受改变引用作用域的 $id")
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise AsterunError(INVALID_CONFIG, f"{where} 不是有效 JSON Schema") from exc


@dataclass(frozen=True, slots=True)
class PluginManifest:
    """规范化 JSON 保持深层不可变；公开读值均为副本。"""

    _json: str

    @classmethod
    def from_dict(cls, raw: Any) -> "PluginManifest":
        try:
            raw = json_copy(raw)
        except AsterunError as exc:
            raise AsterunError(INVALID_CONFIG, "插件 manifest 不是合法 JSON") from exc
        _mapping(raw, "manifest", _TOP, _REQUIRED)
        if raw["schema_version"] != MANIFEST_VERSION or type(raw["plugin_api_major"]) is not int or raw["plugin_api_major"] != PLUGIN_API_MAJOR:
            _invalid("插件 manifest 或 API 主版本不兼容")
        for field in ("plugin_id", "plugin_version", "distribution"):
            _text(raw[field], field, identifier=True)
        _text(raw["entry_point"], "entry_point")
        if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", raw["entry_point"]):
            _invalid("entry_point 必须为 module:attribute")
        for field in ("roles", "auth_modes", "billing_modes"):
            _strings(raw[field], field, nonempty=True)
        if not set(raw["roles"]) <= {"agent", "capability", "handoff", "workflow", "validator"}:
            _invalid("插件 roles 含不支持的角色")
        _mapping(raw["permissions"], "permissions", {"filesystem", "network", "process", "credential_classes"},
                 {"filesystem", "network", "process", "credential_classes"})
        for key, values in raw["permissions"].items():
            _strings(values, f"permissions.{key}")
        if not isinstance(raw["capabilities"], list) or not raw["capabilities"]:
            _invalid("capabilities 必须为非空数组")
        names = set()
        for cap in raw["capabilities"]:
            _mapping(cap, "capability", _CAP, _CAP - {"extensions"})
            _text(cap["name"], "capability.name", identifier=True)
            if cap["name"] in names:
                _invalid("capability.name 不能重复")
            names.add(cap["name"])
            if not isinstance(cap["support"], str) or cap["support"] not in {"declared_only", "supported", "unsupported", "unknown", "offline_verified"}:
                _invalid("capability.support 无效")
            if not isinstance(cap["effect_class"], str) or cap["effect_class"] not in {"read_only", "idempotent_write", "non_idempotent_write", "local", "manual"}:
                _invalid("capability.effect_class 无效")
            _strings(cap["permission_requirements"], "capability.permission_requirements")
            _schema(cap["input_schema"], "capability.input_schema")
            _schema(cap["output_schema"], "capability.output_schema")
            if "extensions" in cap:
                _extensions(cap["extensions"], "capability.extensions")
        if not isinstance(raw["platforms"], list) or not raw["platforms"]:
            _invalid("platforms 必须为非空数组")
        for item in raw["platforms"]:
            _mapping(item, "platform", {"os", "architectures", "verification"}, {"os", "architectures", "verification"})
            _text(item["os"], "platform.os")
            _strings(item["architectures"], "platform.architectures", nonempty=True)
            if not isinstance(item["verification"], str) or item["verification"] not in {"not_tested", "offline_verified", "verified"}:
                _invalid("platform.verification 无效")
        _mapping(raw["docs"], "docs")
        for name, value in raw["docs"].items():
            _text(name, "docs key", identifier=True)
            _text(value, "docs value")
        if "extensions" in raw:
            _extensions(raw["extensions"], "extensions")
        encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_MANIFEST_BYTES:
            _invalid("插件 manifest 超过 256 KiB")
        return cls(encoded)

    @classmethod
    def from_path(cls, path: str | Path) -> "PluginManifest":
        try:
            with Path(path).open("rb") as source:
                data = source.read(MAX_MANIFEST_BYTES + 1)
            if len(data) > MAX_MANIFEST_BYTES:
                _invalid("插件 manifest 超过 256 KiB")
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        _invalid("插件 manifest 含重复 JSON 键")
                    result[key] = value
                return result
            raw = json.loads(data.decode("utf-8"), object_pairs_hook=unique)
        except (OSError, ValueError, UnicodeError, RecursionError) as exc:
            raise AsterunError(INVALID_CONFIG, "无法读取合法的插件 manifest") from exc
        return cls.from_dict(raw)

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self._json)

    @property
    def plugin_id(self) -> str:
        return self.to_dict()["plugin_id"]

    @property
    def plugin_version(self) -> str:
        return self.to_dict()["plugin_version"]

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self._json.encode("utf-8")).hexdigest()

    def capability(self, name: str) -> dict[str, Any]:
        for cap in self.to_dict()["capabilities"]:
            if cap["name"] == name:
                return cap
        raise AsterunError(CAPABILITY_UNSUPPORTED, f"插件未声明能力：{name}")

    def _validate(self, capability: str, value: Any, field: str) -> Any:
        value = json_copy(value)
        schema = self.capability(capability)[field]
        try:
            Draft202012Validator(schema).validate(value)
        except Exception as exc:
            # 包括无法解析的本地引用；绝不因解析失败尝试网络 fallback。
            raise AsterunError(INVALID_REQUEST, f"插件能力 {field} 校验失败") from exc
        return value

    def validate_input(self, capability: str, value: Any) -> Any:
        return self._validate(capability, value, "input_schema")

    def validate_output(self, capability: str, value: Any) -> Any:
        return self._validate(capability, value, "output_schema")
