"""与 Principal、原生会话索引分开的不可变连接快照。"""

from __future__ import annotations

from dataclasses import dataclass
import json

from asterun.control_protocol import digest
from asterun.errors import AsterunError
from asterun.plugin_api.protocol import json_copy
from asterun.usage.models import integer, mapping, text, timestamp, reservation_amounts


def _serialized(value):
    return json.dumps(json_copy(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True, slots=True)
class ProviderAccount:
    """只保存身份/凭据引用；绝不解析引用或保存秘密值。"""

    _json: str

    @classmethod
    def from_dict(cls, ref, value):
        text(ref, "provider_account_ref")
        value = json_copy(value)
        mapping(value, {"provider", "credential_ref", "credential_revision", "revoked"}, {"provider", "credential_ref"})
        text(value["provider"], "provider")
        text(value["credential_ref"], "credential_ref", nullable=True)
        if "credential_revision" in value:
            integer(value["credential_revision"], "credential_revision", minimum=1)
        if "revoked" in value and type(value["revoked"]) is not bool:
            raise AsterunError("INVALID_REQUEST", "revoked 必须为布尔值")
        return cls(_serialized({"ref": ref, **value}))

    def to_dict(self):
        return json.loads(self._json)


@dataclass(frozen=True, slots=True)
class Connection:
    _json: str

    @classmethod
    def from_dict(cls, ref, value):
        text(ref, "connection_ref")
        value = json_copy(value)
        required = {"plugin_id", "enabled", "provider_account_ref", "runtime_ref", "auth_mode", "billing_pool_refs", "options"}
        mapping(value, required | {"credential_ref", "upstream_version"}, required)
        for key in ("plugin_id", "runtime_ref", "auth_mode"):
            text(value[key], key)
        text(value["provider_account_ref"], "provider_account_ref", nullable=True)
        if type(value["enabled"]) is not bool:
            raise AsterunError("INVALID_REQUEST", "连接 enabled 必须为布尔值")
        refs = value["billing_pool_refs"]
        if not isinstance(refs, list):
            raise AsterunError("INVALID_REQUEST", "billing_pool_refs 必须为数组")
        for item in refs:
            text(item, "billing_pool_ref")
        if len(set(refs)) != len(refs):
            raise AsterunError("INVALID_REQUEST", "billing_pool_refs 不能重复")
        mapping(value["options"])
        for key in ("credential_ref", "upstream_version"):
            if key in value:
                text(value[key], key, nullable=True)
        return cls(_serialized({"ref": ref, **value}))

    def to_dict(self):
        return json.loads(self._json)


@dataclass(frozen=True, slots=True)
class PreparedPlan:
    """内部计划，绑定由核心构造，调用者不能借此自授权限。"""

    _json: str

    @classmethod
    def from_dict(cls, value):
        value = json_copy(value)
        required = {"id", "namespace_id", "principal_id", "binding_sha256", "binding", "reservations", "created_at"}
        mapping(value, required | {"schema_version", "expires_at"}, required)
        for key in ("id", "namespace_id", "principal_id"):
            text(value[key], key)
        if value.get("schema_version", "asterun-prepared-plan/v1") != "asterun-prepared-plan/v1":
            raise AsterunError("INVALID_REQUEST", "PreparedPlan 版本不兼容")
        timestamp(value["created_at"], "created_at")
        if "expires_at" in value:
            timestamp(value["expires_at"], "expires_at", nullable=True)
            if value["expires_at"] is not None and timestamp(value["expires_at"]) <= timestamp(value["created_at"]):
                raise AsterunError("INVALID_REQUEST", "计划有效期必须晚于创建时间")
        mapping(value["binding"])
        if not value["binding"] or value["binding_sha256"] != digest(value["binding"]):
            raise AsterunError("BINDING_MISMATCH", "计划绑定摘要不一致")
        reservation_amounts(value["reservations"])
        return cls(_serialized(value))

    def to_dict(self):
        return json.loads(self._json)
