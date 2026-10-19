"""显式 opt-in 的 v2 连接配置；只做静态校验，不登录或启动插件。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import re
from typing import Any

from asterun.errors import AsterunError, INVALID_CONFIG, UNKNOWN_FIELD
from asterun.plugin_api.protocol import json_copy

V2_KEYS = {"plugins", "provider_accounts", "billing_pools", "connections", "routing", "quota"}
PROVIDER_KEYS = {"provider", "credential_ref", "credential_revision", "revoked"}
POOL_KEYS = {
    "billing_mode", "remaining", "paid_overage_allowed", "overage_verified", "meter", "local_limit",
    "max_concurrency", "window_id", "observed_at", "valid_until", "allow_unknown_subscription",
    "risk_acceptance", "estimate_per_action",
}
POOL_METERS = {"requests", "turns", "tokens", "micro_usd", "wall_ms", "tool_actions", "bytes"}
CONNECTION_KEYS = {"plugin_id", "enabled", "provider_account_ref", "runtime_ref", "auth_mode", "billing_pool_refs", "options", "credential_ref", "upstream_version"}
EXTERNAL_KEYS = {"enabled", "manifest_path", "runner", "installation_path", "installation_sha256", "runtime_path", "runtime_sha256", "allow_unpinned_runtime"}
ROUTING_KEYS = {
    "automatic_paid_fallback", "automatic_account_switch", "default_order", "rules",
    "candidate_groups", "quota_preference",
}
_GROUP_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_GROUP_KEYS = {"role", "members", "primary", "models_allowed"}
_ROLE_KINDS = {
    "implementation": {"grok"},
    "review": {"codex"},
    "planning": {"antigravity", "claude"},
}
_PREFERENCE_KEYS = {
    "enabled", "horizon_hours", "waste_threshold_bp", "switch_margin_bp", "require_confidence",
    "consider_short_windows", "floor_short_bp", "floor_long_bp", "count_reset_grants",
}
_BACKEND_COMPARE = (
    "kind", "enabled", "plugin_id", "execution_profile", "max_turns", "timeout_seconds",
    "desktop_projects", "approval_policy", "load_user_settings", "session_sync_home", "session_policy",
)
_OPTION_IGNORE = {"bin", "home", "model", "quota_epoch", "extra_env"}
_OPENAI_MODEL = re.compile(r"o\d")
QUOTA_KEYS = {"probes", "passive"}
QUOTA_PROBE_IDS = {"codex_app_server", "grok_acp_billing"}
QUOTA_PROBE_KEYS = {"enabled", "ttl_seconds", "min_interval_seconds", "timeout_seconds"}
GROK_QUOTA_PROBE_KEYS = {
    "enabled", "acknowledge_undocumented", "ttl_seconds", "min_interval_seconds", "timeout_seconds",
}
QUOTA_PASSIVE_KEYS = {"codex_rate_limits_updated"}


def invalid(message: str) -> None:
    raise AsterunError(INVALID_CONFIG, message)


def mapping(value: Any, where: str, allowed: set[str] | None = None, required=()) -> dict:
    if not isinstance(value, dict):
        invalid(f"{where} 必须为对象")
    if allowed is not None and set(value) - allowed:
        raise AsterunError(UNKNOWN_FIELD, f"{where} 含有未知字段：{', '.join(sorted(set(value) - allowed))}")
    if set(required) - set(value):
        invalid(f"{where} 缺少必需字段：{', '.join(sorted(set(required) - set(value)))}")
    return value


def text(value: Any, where: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str) or not value.strip() or value != value.strip() or any(ord(c) < 32 for c in value):
        invalid(f"{where} 必须为非空字符串")


def flag(value: Any, where: str) -> None:
    if type(value) is not bool:
        invalid(f"{where} 必须为布尔值")


def integer(value: Any, where: str, *, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        invalid(f"{where} 必须是大于等于 {minimum} 的整数")


def integer_between(value: Any, where: str, low: int, high: int) -> None:
    if type(value) is not int or value < low or value > high:
        invalid(f"{where} 必须是 {low} 到 {high} 的整数")


def default_quota() -> dict[str, Any]:
    return {
        "probes": {
            "codex_app_server": {
                "enabled": True,
                "ttl_seconds": 600,
                "min_interval_seconds": 60,
                "timeout_seconds": 10,
            },
            "grok_acp_billing": {
                "enabled": False,
                "acknowledge_undocumented": False,
                "ttl_seconds": 900,
                "min_interval_seconds": 300,
                "timeout_seconds": 10,
            },
        },
        "passive": {"codex_rate_limits_updated": True},
    }


def _normalize_probe(probes: dict, probe_id: str, allowed: set[str], defaults: dict) -> dict:
    probe_in = mapping(probes.get(probe_id, {}), f"quota.probes.{probe_id}", allowed)
    probe = {key: probe_in.get(key, defaults[key]) for key in defaults}
    where = f"quota.probes.{probe_id}"
    flag(probe["enabled"], f"{where}.enabled")
    if "acknowledge_undocumented" in defaults:
        flag(probe["acknowledge_undocumented"], f"{where}.acknowledge_undocumented")
    integer_between(probe["ttl_seconds"], f"{where}.ttl_seconds", 60, 3600)
    integer_between(probe["min_interval_seconds"], f"{where}.min_interval_seconds", 60, 3600)
    integer_between(probe["timeout_seconds"], f"{where}.timeout_seconds", 1, 30)
    return probe


def normalize_quota(raw: Any) -> dict[str, Any]:
    """补齐 quota 默认值，并拒绝未知字段和越界数值。"""
    body = mapping(raw if raw is not None else {}, "quota", QUOTA_KEYS)
    probes = mapping(body.get("probes", {}), "quota.probes", QUOTA_PROBE_IDS)
    defaults = default_quota()
    passive_in = mapping(body.get("passive", {}), "quota.passive", QUOTA_PASSIVE_KEYS)
    passive_flag = passive_in.get("codex_rate_limits_updated", True)
    flag(passive_flag, "quota.passive.codex_rate_limits_updated")
    return {
        "probes": {
            "codex_app_server": _normalize_probe(
                probes, "codex_app_server", QUOTA_PROBE_KEYS, defaults["probes"]["codex_app_server"],
            ),
            "grok_acp_billing": _normalize_probe(
                probes, "grok_acp_billing", GROK_QUOTA_PROBE_KEYS, defaults["probes"]["grok_acp_billing"],
            ),
        },
        "passive": {"codex_rate_limits_updated": passive_flag},
    }


def utc_timestamp(value: Any, where: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)", value):
        invalid(f"{where} 必须为 UTC 时间戳或 null")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AsterunError(INVALID_CONFIG, f"{where} UTC 时间戳无效") from exc
    if parsed.utcoffset() != timedelta(0):
        invalid(f"{where} 必须使用 UTC")
    return parsed


def credential_reference(value: Any, where: str) -> None:
    text(value, where, nullable=True)
    if value is None:
        return
    if value.startswith("credential-ref:"):
        suffix = value[len("credential-ref:"):]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:-]{0,255}", suffix):
            invalid(f"{where} 的不透明凭据引用无效")
    elif value.startswith("env:"):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value[4:]):
            invalid(f"{where} 环境变量引用只允许名称，禁止赋值或秘密值")
    elif value.startswith(("file:", "native-home:")):
        referenced = value.split(":", 1)[1]
        if not Path(referenced).is_absolute():
            invalid(f"{where} 的文件/原生 HOME 引用必须为绝对路径")
    else:
        invalid(f"{where} 只能保存 credential-ref/env/file/native-home 引用，不能保存凭据值")


def reject_secret_options(value: Any, where: str) -> None:
    """配置不提供环境透传/明文秘密字段；凭据解析留在独立受控入口。"""
    prohibited = {"env", "environment", "secret", "secrets", "credentials", "api_key", "apikey",
                  "access_token", "refresh_token", "authorization", "password", "token", "client_secret", "private_key"}
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = key.lower().replace("-", "_")
            if normalized in prohibited or normalized.endswith(("_api_key", "_token", "_secret", "_password")):
                invalid(f"{where} 不允许环境透传或明文凭据字段；请使用 connection/provider_account 的 credential_ref")
            reject_secret_options(child, where)
    elif isinstance(value, list):
        for child in value:
            reject_secret_options(child, where)


def references(value: Any, where: str, known: dict) -> None:
    if not isinstance(value, list):
        invalid(f"{where} 必须为引用数组")
    for ref in value:
        text(ref, where)
        if ref not in known:
            invalid(f"{where} 指向未知引用：{ref}")
    if len(set(value)) != len(value):
        invalid(f"{where} 不能重复")


def _path(raw: Any, source_path: Path, where: str) -> str:
    text(raw, where)
    value = Path(raw)
    return str((source_path.parent / value).resolve() if not value.is_absolute() else value)


def vendor_tags(kind: str, model: str | None) -> set[str]:
    """一家供应商。kind 与模型前缀都算，审查组不能和实现组相交。"""
    tags = set()
    name = (model or "").strip().lower()
    if kind == "grok":
        tags.add("xai")
    if kind == "codex" or name.startswith("gpt") or _OPENAI_MODEL.match(name):
        tags.add("openai")
    if name.startswith("claude"):
        tags.add("anthropic")
    if name.startswith("gemini"):
        tags.add("google")
    if kind == "claude":
        tags.add("anthropic")
    return tags


def _execution_signature(backend: dict, connection: dict) -> dict:
    signature = {}
    for key in _BACKEND_COMPARE:
        value = backend.get(key)
        if key == "enabled" and value is None:
            value = True
        if key == "load_user_settings" and value is None:
            value = False
        signature[key] = value
    options = connection.get("options") or {}
    signature["options"] = {key: options[key] for key in sorted(options) if key not in _OPTION_IGNORE}
    return signature


def _validate_quota_preference(routing: dict) -> None:
    if "quota_preference" not in routing:
        return
    pref = mapping(routing["quota_preference"], "routing.quota_preference", _PREFERENCE_KEYS)
    if "enabled" in pref:
        flag(pref["enabled"], "routing.quota_preference.enabled")
    if "horizon_hours" in pref:
        integer_between(pref["horizon_hours"], "routing.quota_preference.horizon_hours", 1, 168)
    if "waste_threshold_bp" in pref:
        integer_between(pref["waste_threshold_bp"], "routing.quota_preference.waste_threshold_bp", 0, 10000)
    if "switch_margin_bp" in pref:
        integer_between(pref["switch_margin_bp"], "routing.quota_preference.switch_margin_bp", 0, 10000)
    if "require_confidence" in pref and pref["require_confidence"] not in {"high", "medium"}:
        invalid("routing.quota_preference.require_confidence 只能是 high 或 medium")
    if "consider_short_windows" in pref:
        flag(pref["consider_short_windows"], "routing.quota_preference.consider_short_windows")
    if "floor_short_bp" in pref:
        integer_between(pref["floor_short_bp"], "routing.quota_preference.floor_short_bp", 500, 5000)
    if "floor_long_bp" in pref:
        integer_between(pref["floor_long_bp"], "routing.quota_preference.floor_long_bp", 1000, 5000)
    if "count_reset_grants" in pref:
        flag(pref["count_reset_grants"], "routing.quota_preference.count_reset_grants")
        if pref["count_reset_grants"]:
            invalid("routing.quota_preference.count_reset_grants 只允许 false；重置券不计入浪费")


def _validate_candidate_routing(routing: dict, backends: dict, connections: dict, pools: dict) -> None:
    _validate_quota_preference(routing)
    if "candidate_groups" not in routing:
        return
    groups = mapping(routing["candidate_groups"], "routing.candidate_groups")
    parsed = []
    seen_roles = set()
    for name, body in groups.items():
        if not isinstance(name, str) or _GROUP_NAME.fullmatch(name) is None:
            invalid(f"候选组名 {name!r} 必须匹配 ^[a-z][a-z0-9_-]{{0,31}}$")
        group = mapping(body, f"routing.candidate_groups.{name}", _GROUP_KEYS, {"role", "members", "primary"})
        role = group["role"]
        if role not in _ROLE_KINDS:
            invalid(f"候选组 {name} 的 role 只能是 implementation、review 或 planning")
        if role in seen_roles:
            invalid(f"同一角色只能有一个候选组：{role}")
        seen_roles.add(role)
        members = group["members"]
        references(members, f"routing.candidate_groups.{name}.members", backends)
        if not 1 <= len(members) <= 8:
            invalid(f"候选组 {name} 的成员数量必须是 1 到 8")
        text(group["primary"], f"routing.candidate_groups.{name}.primary")
        if group["primary"] not in members:
            invalid(f"候选组 {name} 的 primary 必须是成员")
        allowed = None
        if "models_allowed" in group:
            allowed = group["models_allowed"]
            if not isinstance(allowed, list) or not 1 <= len(allowed) <= 32:
                invalid(f"候选组 {name} 的 models_allowed 必须是 1 到 32 个模型")
            for model in allowed:
                text(model, f"routing.candidate_groups.{name}.models_allowed")
            if len(set(allowed)) != len(allowed):
                invalid(f"候选组 {name} 的 models_allowed 不能重复")
        parsed.append((name, role, members, group["primary"], allowed))

    by_role = {role: members for _, role, members, _, _ in parsed}
    review_members = set(by_role.get("review", []))
    implementation_members = set(by_role.get("implementation", []))
    if review_members & implementation_members:
        invalid("审查组不能包含实现组的后端")
    occupied: dict[str, str] = {}
    for _, role, members, _, _ in parsed:
        for alias in members:
            previous = occupied.get(alias)
            if previous is not None and previous != role:
                invalid(f"不同角色的组成员不能相交：{alias}")
            occupied[alias] = role

    for name, role, members, _, allowed in parsed:
        allowed_kinds = _ROLE_KINDS[role]
        kinds = []
        for alias in members:
            kind = backends[alias].get("kind")
            if kind not in allowed_kinds:
                invalid(f"候选组 {name} 的后端 {alias} 种类 {kind} 不能担任 {role}")
            kinds.append(kind)
        if len(set(kinds)) != 1:
            invalid(f"候选组 {name} 的成员种类必须相同")
        signature = None
        for alias in members:
            backend = backends[alias]
            if allowed is not None and backend.get("model") not in allowed:
                invalid(f"候选组 {name} 的成员 {alias} 的模型不在 models_allowed 中")
            current = _execution_signature(backend, connections[backend["connection_ref"]])
            if signature is None:
                signature = current
            elif current != signature:
                invalid(f"候选组 {name} 的执行选项必须相同")
            connection = connections[backend["connection_ref"]]
            for pool_ref in connection["billing_pool_refs"]:
                pool = pools[pool_ref]
                if pool["billing_mode"] not in {"subscription", "local"}:
                    invalid(f"候选组 {name} 的计费池 {pool_ref} 必须是 subscription 或 local")
                if pool.get("paid_overage_allowed") is True:
                    invalid(f"候选组 {name} 的计费池 {pool_ref} 不能允许付费超额")

    if not implementation_members or not review_members:
        return
    accounts = set()
    vendors = set()
    for alias in implementation_members:
        backend = backends[alias]
        account = connections[backend["connection_ref"]].get("provider_account_ref")
        if account:
            accounts.add(account)
        vendors |= vendor_tags(backend.get("kind"), backend.get("model"))
    for alias in review_members:
        backend = backends[alias]
        account = connections[backend["connection_ref"]].get("provider_account_ref")
        if account in accounts:
            invalid(f"审查组不能包含实现组的供应商账户 {account}")
        shared = vendor_tags(backend.get("kind"), backend.get("model")) & vendors
        if shared:
            invalid(f"审查组不能包含与实现组同一家的模型：{alias}")


def parse_v2(raw: dict, source_path: Path) -> tuple[dict[str, dict], dict[str, dict]]:
    from asterun.plugins.builtins import (
        GROK_EXECUTION_OPTIONS, builtin_kind, builtin_registry, compatibility_options,
        validate_grok_execution_options,
    )
    from asterun.backends.codex_policy import validate_codex_approval_policy
    from asterun.plugins.registry import PluginRegistry

    try:
        namespaces = {key: json_copy(mapping(raw.get(key, {}), key)) for key in V2_KEYS}
    except AsterunError as exc:
        if exc.code == UNKNOWN_FIELD:
            raise
        raise AsterunError(INVALID_CONFIG, "配置 v2 命名空间包含无效 JSON") from exc
    plugins, accounts, pools, connections, routing = (namespaces[key] for key in (
        "plugins", "provider_accounts", "billing_pools", "connections", "routing",
    ))
    namespaces["quota"] = normalize_quota(namespaces.get("quota", {}))
    manifests = {}
    builtins = builtin_registry()
    external = PluginRegistry()
    for plugin_id, settings in plugins.items():
        text(plugin_id, "plugins key")
        kind = builtin_kind(plugin_id) if isinstance(settings, dict) and "manifest_path" not in settings else None
        mapping(settings, f"plugins.{plugin_id}", {"enabled"} if kind else EXTERNAL_KEYS, {"enabled"})
        flag(settings["enabled"], f"plugins.{plugin_id}.enabled")
        if kind:
            manifests[plugin_id] = builtins.get(plugin_id).manifest
            continue
        if "allow_unpinned_runtime" in settings:
            flag(settings["allow_unpinned_runtime"], f"plugins.{plugin_id}.allow_unpinned_runtime")
        required = {"manifest_path", "runner", "installation_path", "installation_sha256"}
        if settings.get("allow_unpinned_runtime") is not True:
            required |= {"runtime_path", "runtime_sha256"}
        missing = required - set(settings)
        if missing & {"runtime_path", "runtime_sha256"}:
            invalid(f"外部插件 {plugin_id} 必须钉定运行代码摘要")
        if missing:
            invalid(f"外部插件 {plugin_id} 缺少显式注册信息")
        for key in ("manifest_path", "installation_path", "runtime_path"):
            if key in settings:
                settings[key] = _path(settings[key], source_path, f"plugins.{plugin_id}.{key}")
        registration = external.register_external(**settings)
        if registration.plugin_id != plugin_id:
            invalid(f"插件 ID 与 manifest 不一致：{plugin_id}")
        manifests[plugin_id] = registration.manifest
    for ref, account in accounts.items():
        text(ref, "provider_accounts key")
        mapping(account, f"provider_accounts.{ref}", PROVIDER_KEYS, {"provider", "credential_ref"})
        text(account["provider"], f"provider_accounts.{ref}.provider")
        credential_reference(account["credential_ref"], f"provider_accounts.{ref}.credential_ref")
        if "credential_revision" in account:
            integer(account["credential_revision"], f"provider_accounts.{ref}.credential_revision", minimum=1)
        if "revoked" in account:
            flag(account["revoked"], f"provider_accounts.{ref}.revoked")
    for ref, pool in pools.items():
        text(ref, "billing_pools key")
        mapping(pool, f"billing_pools.{ref}", POOL_KEYS, {"billing_mode", "paid_overage_allowed"})
        if not isinstance(pool["billing_mode"], str) or pool["billing_mode"] not in {"local", "subscription", "metered", "manual", "unknown"}:
            invalid(f"billing_pools.{ref}.billing_mode 无效")
        flag(pool["paid_overage_allowed"], f"billing_pools.{ref}.paid_overage_allowed")
        if pool["paid_overage_allowed"]:
            invalid("M1 不支持自动允许 overage")
        for name in ("overage_verified", "allow_unknown_subscription"):
            if name in pool:
                flag(pool[name], f"billing_pools.{ref}.{name}")
        if pool.get("remaining") is not None and (type(pool["remaining"]) is not int or pool["remaining"] < 0):
            invalid(f"billing_pools.{ref}.remaining 只接受未知 null 或非负整数；小数计量须显式单位")
        if "meter" in pool and (not isinstance(pool["meter"], str) or pool["meter"] not in POOL_METERS):
            invalid(f"billing_pools.{ref}.meter 未声明或不支持；金额单位为整数 micro_usd")
        for name in ("local_limit", "max_concurrency"):
            if name in pool:
                integer(pool[name], f"billing_pools.{ref}.{name}", minimum=1)
        if "estimate_per_action" in pool:
            integer(pool["estimate_per_action"], f"billing_pools.{ref}.estimate_per_action")
        if "window_id" in pool:
            text(pool["window_id"], f"billing_pools.{ref}.window_id")
        observed_at = utc_timestamp(pool.get("observed_at"), f"billing_pools.{ref}.observed_at")
        valid_until = utc_timestamp(pool.get("valid_until"), f"billing_pools.{ref}.valid_until")
        if observed_at is not None and valid_until is not None and valid_until <= observed_at:
            invalid(f"billing_pools.{ref}.valid_until 必须晚于 observed_at")
        if "risk_acceptance" in pool and pool["risk_acceptance"] not in (None, "local_estimate"):
            invalid(f"billing_pools.{ref}.risk_acceptance 仅接受 local_estimate 或 null")
    for ref, connection in connections.items():
        text(ref, "connections key")
        mapping(connection, f"connections.{ref}", CONNECTION_KEYS, CONNECTION_KEYS - {"credential_ref", "upstream_version"})
        plugin_id = connection["plugin_id"]
        text(plugin_id, f"connections.{ref}.plugin_id")
        if plugin_id not in plugins:
            invalid(f"连接 {ref} 指向未注册插件")
        flag(connection["enabled"], f"connections.{ref}.enabled")
        account = connection["provider_account_ref"]
        text(account, f"connections.{ref}.provider_account_ref", nullable=True)
        if account is not None and account not in accounts:
            invalid(f"连接 {ref} 指向未知供应商账户")
        text(connection["runtime_ref"], f"connections.{ref}.runtime_ref")
        text(connection["auth_mode"], f"connections.{ref}.auth_mode")
        manifest = manifests[plugin_id].to_dict()
        if connection["auth_mode"] not in manifest["auth_modes"]:
            invalid(f"连接 {ref} 的认证模式不在插件声明中")
        if connection["auth_mode"] != "none" and account is None:
            invalid(f"连接 {ref} 的认证模式需要独立供应商账户引用")
        if "credential_ref" in connection:
            credential_reference(connection["credential_ref"], f"connections.{ref}.credential_ref")
        if "upstream_version" in connection:
            text(connection["upstream_version"], f"connections.{ref}.upstream_version", nullable=True)
        references(connection["billing_pool_refs"], f"connections.{ref}.billing_pool_refs", pools)
        for pool_ref in connection["billing_pool_refs"]:
            if pools[pool_ref]["billing_mode"] not in manifest["billing_modes"] + ["unknown"]:
                invalid(f"连接 {ref} 的计费模式不在插件声明中")
        options = mapping(connection["options"], f"connections.{ref}.options")
        reject_secret_options(options, f"connections.{ref}.options")
        kind = builtin_kind(plugin_id) if "manifest_path" not in plugins[plugin_id] else None
        if kind:
            mapping(options, f"connections.{ref}.options", compatibility_options(kind))
            if kind == "grok":
                validate_grok_execution_options(options, f"connections.{ref}.options")
            if "desktop_projects" in options:
                flag(options["desktop_projects"], f"connections.{ref}.options.desktop_projects")
            if "approval_policy" in options:
                validate_codex_approval_policy(options["approval_policy"], f"connections.{ref}.options")
            if "extra_env" in options:
                from asterun.child_env import validate_extra_env
                validate_extra_env(options["extra_env"], f"connections.{ref}.options.extra_env")
            if "load_user_settings" in options:
                flag(options["load_user_settings"], f"connections.{ref}.options.load_user_settings")
            if kind == "antigravity" and "timeout_seconds" in options:
                from asterun.backends.antigravity import validate_antigravity_timeout
                validate_antigravity_timeout(options["timeout_seconds"], f"connections.{ref}.options.timeout_seconds")
            for key in ("bin", "home", "model"):
                if key in options:
                    text(options[key], f"connections.{ref}.options.{key}")
            if "home" in options and not Path(options["home"]).is_absolute():
                invalid(f"connections.{ref}.options.home 必须为绝对路径")
    mapping(routing, "routing", ROUTING_KEYS)
    for key in ("automatic_paid_fallback", "automatic_account_switch"):
        if key in routing:
            flag(routing[key], f"routing.{key}")
            if routing[key]:
                invalid(f"M1 不支持 {key}")
    if "default_order" in routing:
        references(routing["default_order"], "routing.default_order", connections)
    if routing.get("rules", []) != []:
        invalid("M1 仅支持显式连接，routing.rules 必须为空数组")
    backends = {}
    for alias, item in mapping(raw.get("backends", {}), "backends").items():
        text(alias, "backends key")
        mapping(item, f"backends.{alias}", {"connection_ref"}, {"connection_ref"})
        ref = item["connection_ref"]
        text(ref, f"backends.{alias}.connection_ref")
        if ref not in connections:
            invalid(f"backends.{alias} 指向未知连接")
        connection = connections[ref]
        plugin_id = connection["plugin_id"]
        options = connection["options"]
        kind = builtin_kind(plugin_id) if "manifest_path" not in plugins[plugin_id] else None
        backends[alias] = {
            "kind": kind or plugin_id,
            "enabled": plugins[plugin_id]["enabled"] and connection["enabled"],
            "bin": options.get("bin"), "home": options.get("home"), "model": options.get("model"),
            "connection_ref": ref, "plugin_id": plugin_id, "options": json_copy(options),
        }
        if kind == "grok":
            backends[alias].update({key: options[key] for key in GROK_EXECUTION_OPTIONS if key in options})
        if kind == "codex" and "desktop_projects" in options:
            backends[alias]["desktop_projects"] = options["desktop_projects"]
        if kind == "codex" and "approval_policy" in options:
            backends[alias]["approval_policy"] = options["approval_policy"]
        if kind in {"codex", "claude"} and "extra_env" in options:
            from asterun.child_env import validate_extra_env
            backends[alias]["extra_env"] = validate_extra_env(options["extra_env"], f"connections.{ref}.options.extra_env")
        if kind == "claude" and options.get("load_user_settings") is True:
            backends[alias]["load_user_settings"] = True
        if kind == "antigravity" and "timeout_seconds" in options:
            backends[alias]["timeout_seconds"] = options["timeout_seconds"]
    _validate_candidate_routing(routing, backends, connections, pools)
    return backends, namespaces
