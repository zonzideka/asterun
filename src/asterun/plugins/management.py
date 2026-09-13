"""可选插件的本地管理；配置候选、停用闩和状态均不启动 worker。"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from asterun import config_migration
from asterun.config import load_config
from asterun.config_v2 import EXTERNAL_KEYS, parse_v2
from asterun.control_protocol import digest, loads
from asterun.errors import AsterunError
from asterun.plugin_api.protocol import json_copy
from asterun.plugins.builtins import build_registry, builtin_kind
from asterun.policy import enforce, reject_self_reported_authority
from asterun.usage.models import mapping, text, timestamp

LATCH_PREFIX = "plugin.disabled."
LATCH_VERSION = "asterun-plugin-latch/v1"
METHOD_FIELDS = {
    "plugin.list": set(),
    "plugin.inspect": {"plugin_id"},
    "plugin.register": {"plugin_id", "registration", "output", "backup", "expected_source_sha256"},
    "plugin.enable": {"plugin_id"},
    "plugin.disable": {"plugin_id"},
    "connection.setup": {"connection_ref"},
    "usage.inspect": {"pool_ref", "meter", "window_id"},
}


def registration_identity(registration):
    """安装身份不含 enabled；停用操作不能改变原有计划配置摘要。"""
    return {
        "plugin_id": registration.plugin_id, "plugin_version": registration.manifest.plugin_version,
        "source": registration.source, "manifest_sha256": registration.manifest.sha256,
        "manifest_file_sha256": registration.manifest_file_sha256,
        "manifest_path": str(registration.manifest_path) if registration.manifest_path else None,
        "installation_path": str(registration.installation_path) if registration.installation_path else None,
        "installation_sha256": registration.installation_sha256,
        "runner": list(registration.runner), "runner_sha256": registration.runner_sha256,
        "runtime_path": str(registration.runtime_path) if registration.runtime_path else None,
        "runtime_sha256": registration.runtime_sha256, "factory": registration.factory,
    }


def _latch_status(app, registration):
    if app.config.schema_version != 2:
        return {"disabled": False, "reason": None, "identity_matches": None}
    if not hasattr(app.store, "_conn"):
        return {"disabled": True, "reason": "persistent_state_required", "identity_matches": None}
    try:
        row = app.store._conn.execute("SELECT value FROM meta WHERE key=?", (LATCH_PREFIX + registration.plugin_id,)).fetchone()
    except sqlite3.Error as exc:
        raise AsterunError("INVALID_STATE", "无法读取插件停用记录；拒绝新派发") from exc
    if row is None:
        return {"disabled": False, "reason": None, "identity_matches": None}
    try:
        value = loads(row[0])
        fields = {"schema_version", "plugin_id", "disabled", "identity_sha256", "principal_id", "updated_at"}
        mapping(value, fields, fields)
        if value["schema_version"] != LATCH_VERSION or value["plugin_id"] != registration.plugin_id or value["disabled"] is not True:
            raise AsterunError("INVALID_STATE", "停用记录的身份、版本或状态无效")
        if not isinstance(value["identity_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["identity_sha256"]):
            raise AsterunError("INVALID_STATE", "停用记录缺少安装摘要")
        text(value["principal_id"], "principal_id")
        timestamp(value["updated_at"])
    except (AsterunError, ValueError, TypeError, UnicodeError, RecursionError):
        return {"disabled": True, "reason": "invalid_latch", "identity_matches": False}
    matches = value["identity_sha256"] == digest(registration_identity(registration))
    return {"disabled": True, "reason": "operator_disabled" if matches else "installation_changed_while_disabled",
            "identity_matches": matches}


def plugin_enabled(app, registration):
    """主线程查询；闩损坏或安装更换都不能隐式重新启用。"""
    return registration.enabled and not _latch_status(app, registration)["disabled"]


def sync_runtime_latches(app, *, plugin_id=None):
    """主线程将持久闩同步到 worker 最终调用前检查的注册表。

    启动恢复时遍历全部实例；单次管理动作只同步目标插件。配置和已固定的
    registration 不改写，停用后的 observe/reconcile/cancel 仍由 WorkerHost 放行。
    """
    if app.config.schema_version != 2:
        return
    seen, targets = set(), []
    for backend in app.backends.values():
        pinned, registry = getattr(backend, "registration", None), getattr(backend, "registry", None)
        if pinned is None or registry is None or pinned.source != "external":
            continue
        if plugin_id is not None and pinned.plugin_id != plugin_id:
            continue
        key = (id(registry), pinned.plugin_id)
        if key in seen:
            continue
        seen.add(key)
        # 先置停用；后续核验失败不能使旧的 True 留在执行线程的注册表中。
        registry.set_enabled(pinned.plugin_id, False)
        targets.append((pinned, registry))
    fresh = build_registry(app.config)
    for pinned, registry in targets:
        current = fresh.get(pinned.plugin_id)
        if plugin_enabled(app, current):
            if digest(registration_identity(pinned)) != digest(registration_identity(current)):
                raise AsterunError("BINDING_MISMATCH", "活动 worker 安装身份已改变，保持停用")
            registry.set_enabled(pinned.plugin_id, True)


def _authorize(app, method):
    enforce(app.policy.authorize(app.principal, method, "*"))
    app._require_grant("*")
    if app.config.schema_version != 2:
        raise AsterunError("CONFIG_REQUIRED", "插件管理需要显式配置 v2；原 v1 文件保持不变")
    if not hasattr(app.store, "_conn"):
        raise AsterunError("CONFIG_REQUIRED", "插件管理需要 SQLite 状态目录")
    app._require_applied_config()


def _report(app, registration):
    latch = _latch_status(app, registration)
    manifest = registration.manifest.to_dict()
    return {
        "plugin_id": registration.plugin_id, "plugin_version": registration.manifest.plugin_version,
        "source": registration.source, "configured": registration.plugin_id in app.config.plugins,
        "configuration_enabled": registration.enabled,
        "enabled": registration.enabled and not latch["disabled"], "runtime_latch": latch,
        "identity_sha256": digest(registration_identity(registration)),
        "manifest_sha256": registration.manifest.sha256,
        "installation_sha256": registration.installation_sha256,
        "runner_sha256": registration.runner_sha256, "runtime_sha256": registration.runtime_sha256,
        "runtime_integrity_pinned": registration.source == "builtin" or registration.runtime_path is not None,
        "roles": manifest["roles"], "auth_modes": manifest["auth_modes"], "billing_modes": manifest["billing_modes"],
        "capabilities": [{"name": capability["name"], "support": capability["support"]} for capability in manifest["capabilities"]],
        "installation_verified": True, "authenticated": "not_tested", "callable": "not_tested",
        "resume_verified": False, "native_visible_verified": False, "worker_started": False,
        "scope": "local_registration_and_persistent_latch",
    }


def _set_enabled(app, plugin_id, enabled, registry):
    if plugin_id not in app.config.plugins:
        raise AsterunError("INVALID_CONFIG", "插件未在当前 v2 配置中显式注册")
    registration = registry.get(plugin_id)
    if enabled:
        if not app.config.plugins[plugin_id]["enabled"] or not registration.enabled:
            raise AsterunError("SCOPE_DENIED", "启用只能清除临时停用闩；配置 disabled 需要审阅新配置后重启")
        registry.verify_installation(plugin_id)
        latch = _latch_status(app, registration)
        if latch["disabled"] and not latch["identity_matches"]:
            raise AsterunError("BINDING_MISMATCH", "停用记录损坏或安装身份已改变；不能借启用操作解除核验失败")
        for backend in app.backends.values():
            pinned = getattr(backend, "registration", None)
            if pinned is not None and pinned.plugin_id == plugin_id and digest(registration_identity(pinned)) != digest(registration_identity(registration)):
                raise AsterunError("BINDING_MISMATCH", "插件安装与实例启动时的固定身份不一致")
    store = app.admission.persistent()
    with store.transaction():
        if enabled:
            app.store._conn.execute("DELETE FROM meta WHERE key=?", (LATCH_PREFIX + plugin_id,))
        else:
            value = {"schema_version": LATCH_VERSION, "plugin_id": plugin_id, "disabled": True,
                     "identity_sha256": digest(registration_identity(registration)),
                     "principal_id": app.principal.subject_id,
                     "updated_at": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")}
            app.store._conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                   (LATCH_PREFIX + plugin_id, json.dumps(value, sort_keys=True, separators=(",", ":"))))
    sync_runtime_latches(app, plugin_id=plugin_id)
    return {"plugin": _report(app, registration), "configuration_unchanged": True,
            "new_execution_allowed": plugin_enabled(app, registration),
            "inflight_observation_allowed": True, "remote_termination_verified": False,
            "effect": "clear_runtime_disable_latch" if enabled else "persist_runtime_disable_latch"}


def _register(app, payload):
    source = app.config.source_path
    if source is None:
        raise AsterunError("CONFIG_REQUIRED", "注册需要可核对原始字节的 v2 配置文件")
    source = Path(source).absolute()
    plugin_id, settings = payload["plugin_id"], json_copy(payload["registration"])
    text(plugin_id, "plugin_id")
    mapping(settings)
    allowed = {"enabled"} if builtin_kind(plugin_id) and "manifest_path" not in settings else EXTERNAL_KEYS
    mapping(settings, allowed, {"enabled"})
    expected = payload["expected_source_sha256"]
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise AsterunError("INVALID_REQUEST", "expected_source_sha256 必须为已审阅源配置摘要")
    for key in ("output", "backup"):
        text(payload[key], key)
    destination, backup = (Path(payload[key]).expanduser().absolute() for key in ("output", "backup"))
    if len({path.resolve() for path in (source, destination, backup)}) != 3:
        raise AsterunError("INVALID_CONFIG", "注册源配置、候选与备份必须为不同路径")
    if any(path.exists() or path.is_symlink() for path in (destination, backup)):
        raise AsterunError("INVALID_CONFIG", "候选配置和备份必须是全新文件；不会覆盖现有内容")
    original = config_migration._read(source)
    if hashlib.sha256(original).hexdigest() != expected:
        raise AsterunError("REVISION_CONFLICT", "源配置与已审阅摘要不同，未写入插件配置")
    current = load_config(source)
    if digest(current.to_dict()) != digest(app.config.to_dict()):
        raise AsterunError("REVISION_CONFLICT", "源配置与当前运行实例不同，请先核对配置版本")
    proposed = loads(original)
    proposed["revision"] = current.revision + 1
    proposed["plugins"] = json_copy(current.plugins)
    replacing = plugin_id in proposed["plugins"]
    proposed["plugins"][plugin_id] = settings
    proposed["workspaces"] = {name: {"root": str(item.root), "allow_non_git": item.allow_non_git}
                              for name, item in current.workspaces.items()}
    _, namespaces = parse_v2(proposed, source)
    proposed.update(namespaces)
    # 原有与新注册相对路径全部在源位置解释，候选移到别处仍引用相同安装。
    proposed["plugins"] = namespaces["plugins"]
    if config_migration._read(source) != original:
        raise AsterunError("REVISION_CONFLICT", "校验期间源配置改变，未创建备份或候选")
    encoded = (json.dumps(proposed, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    config_migration._create_private(backup, original)
    if config_migration._read(source) != original:
        raise AsterunError("REVISION_CONFLICT", "备份后源配置改变；保留备份，未写入插件候选")
    config_migration._create_private(destination, encoded)
    return {"plugin_id": plugin_id, "source": str(source), "source_sha256": expected,
            "source_unchanged": True, "output": str(destination), "destination": str(destination), "backup": str(backup),
            "destination_sha256": hashlib.sha256(encoded).hexdigest(), "candidate_revision": proposed["revision"],
            "replaces_registration": replacing, "candidate_written": True,
            "restart_required": True, "reload_required": True, "configuration_applied": False,
            "worker_started": False, "authenticated": "not_tested",
            "compatibility": "原配置、当前实例、会话和部署保持不变；审阅候选后显式使用新配置并应用 revision"}


def _connection_setup(app, ref, registry):
    if ref not in app.config.connections:
        raise AsterunError("INVALID_CONFIG", "未找到显式连接引用")
    connection = app.config.connections[ref]
    registration = registry.verify_installation(connection["plugin_id"])
    provider = app.config.provider_accounts.get(connection["provider_account_ref"], {})
    manifest = registration.manifest.to_dict()
    return {"connection_ref": ref, "plugin_id": registration.plugin_id,
            "provider_account_ref": connection["provider_account_ref"], "runtime_ref": connection["runtime_ref"],
            "auth_mode": connection["auth_mode"], "auth_mode_supported": connection["auth_mode"] in manifest["auth_modes"],
            "configuration_enabled": connection["enabled"],
            "plugin_enabled": plugin_enabled(app, registration),
            "credential_reference_configured": bool(connection.get("credential_ref") or provider.get("credential_ref")),
            "credential_reference_resolved": False, "provider_revoked": provider.get("revoked", False),
            "capabilities": [{"name": item["name"], "support": item["support"]} for item in manifest["capabilities"]],
            "authenticated": "not_tested", "callable": "not_tested", "native_visible_verified": False,
            "worker_started": False, "live_authorized": False, "setup_mode": "static_guidance",
            "next_action": "使用独立授权流程核对供应商身份、认证与权益；本命令仅返回本地连接状态"}


def _usage_inspect(app, payload):
    requested = payload.get("pool_ref")
    if requested is not None and requested not in app.config.billing_pools:
        raise AsterunError("INVALID_CONFIG", "未找到显式计费池引用")
    store = app.admission.persistent()
    namespace = app.admission.namespace
    result = []
    for ref in ([requested] if requested is not None else sorted(app.config.billing_pools)):
        pool = app.config.billing_pools[ref]
        observations = store.observations(ref, namespace)
        if payload.get("meter"):
            observations = [item for item in observations if item["observation"]["meter"] == payload["meter"]]
        if payload.get("window_id"):
            observations = [item for item in observations if item["observation"]["window_id"] == payload["window_id"]]
        result.append({"pool_ref": ref, "billing_mode": pool["billing_mode"], "remaining": pool.get("remaining"),
                       "observed_at": pool.get("observed_at"), "valid_until": pool.get("valid_until"),
                       "local_limit": pool.get("local_limit"), "max_concurrency": pool.get("max_concurrency"),
                       "ledger": store.pool_totals(ref, namespace, meter=payload.get("meter", pool.get("meter")),
                                                   window_id=payload.get("window_id", pool.get("window_id"))),
                       "observations": observations[-100:], "observations_truncated": len(observations) > 100})
    return {"pools": result, "upstream_queried": False, "local_admission_only": True,
            "account_bill_hard_cap": False, "unknown_balance_is_null": True}


def handle(app, method, payload):
    if method not in METHOD_FIELDS:
        raise AsterunError("INVALID_REQUEST", "未知插件管理方法")
    payload = json_copy(payload)
    reject_self_reported_authority(payload)
    required = METHOD_FIELDS[method] if method != "usage.inspect" else set()
    mapping(payload, METHOD_FIELDS[method], required)
    for key, value in payload.items():
        if key != "registration":
            text(value, key)
    _authorize(app, method)
    if method == "plugin.register":
        return _register(app, payload)
    if method == "usage.inspect":
        return _usage_inspect(app, payload)
    registry = build_registry(app.config)
    if method == "plugin.list":
        return {"plugins": [_report(app, registry.verify_installation(item["plugin_id"])) for item in registry.describe()],
                "worker_started": False, "authenticated": "not_tested"}
    if method == "plugin.inspect":
        return {"plugin": _report(app, registry.verify_installation(payload["plugin_id"]))}
    if method in {"plugin.enable", "plugin.disable"}:
        return _set_enabled(app, payload["plugin_id"], method == "plugin.enable", registry)
    return _connection_setup(app, payload["connection_ref"], registry)
