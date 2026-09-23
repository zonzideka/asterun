"""随核心分发的兼容插件目录；构建目录不会导入供应商模块。"""
from __future__ import annotations

from pathlib import Path

from asterun import __version__
from asterun.errors import AsterunError, INVALID_CONFIG
from asterun.plugins.registry import PluginRegistry

BUILTINS = {
    "fake": {"plugin_id": "asterun.fake", "factory": "asterun.backends.fake:FakeBackend", "options": {"bin"}},
    "codex": {"plugin_id": "openai.codex", "factory": "asterun.backends.codex:CodexBackend", "options": {"bin", "desktop_projects"}},
    "grok": {"plugin_id": "xai.grok", "factory": "asterun.backends.grok:GrokBackend", "options": {"bin", "home", "model", "execution_profile", "max_turns", "timeout_seconds", "session_sync_home"}},
    "claude": {"plugin_id": "anthropic.claude", "factory": "asterun.backends.claude:ClaudeBackend", "options": {"bin"}},
    "antigravity": {"plugin_id": "google.antigravity-cli", "factory": "asterun.backends.antigravity:AntigravityBackend", "options": {"bin", "home", "model"}},
}

GROK_EXECUTION_OPTIONS = ("execution_profile", "max_turns", "timeout_seconds", "session_sync_home")


def validate_grok_execution_options(options, where):
    """v1/v2 共用显式执行限制校验；不向旧配置注入默认值。"""
    profile = options.get("execution_profile", "text-only-v1")
    if not isinstance(profile, str) or profile not in {"text-only-v1", "workspace-code-v1"}:
        raise AsterunError(INVALID_CONFIG, f"{where}.execution_profile 必须为 text-only-v1 或 workspace-code-v1")
    for key, upper in (("max_turns", 100), ("timeout_seconds", 3600)):
        if key in options and (type(options[key]) is not int or not 1 <= options[key] <= upper):
            raise AsterunError(INVALID_CONFIG, f"{where}.{key} 必须为 1..{upper} 的整数")
    if profile == "text-only-v1" and "max_turns" in options and options["max_turns"] != 1:
        raise AsterunError(INVALID_CONFIG, f"{where}.max_turns 在 text-only-v1 下只能为 1")
    if "session_sync_home" in options:
        value = options["session_sync_home"]
        if (not isinstance(value, str) or not value.strip() or value != value.strip()
                or "\x00" in value or not Path(value).is_absolute() or ".." in Path(value).parts):
            raise AsterunError(INVALID_CONFIG, f"{where}.session_sync_home 必须为显式绝对路径")


def builtin_kind(plugin_id):
    return next((kind for kind, entry in BUILTINS.items() if entry["plugin_id"] == plugin_id), None)


def builtin_plugin_id(kind):
    if kind not in BUILTINS:
        raise AsterunError(INVALID_CONFIG, "未知兼容后端种类")
    return BUILTINS[kind]["plugin_id"]


def compatibility_options(kind):
    return set(BUILTINS[kind]["options"])


def builtin_manifest(kind):
    entry = BUILTINS[kind]
    return {
        "schema_version": "asterun-plugin/v1", "plugin_id": entry["plugin_id"],
        "plugin_version": __version__, "plugin_api_major": 1,
        "distribution": "asterun", "entry_point": entry["factory"], "roles": ["agent"],
        "capabilities": [{"name": "agent.execute", "support": "offline_verified" if kind == "fake" else "declared_only",
                          "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False},
                          "output_schema": {"type": "object"}, "effect_class": "non_idempotent_write",
                          "permission_requirements": ["workspace.read"]}],
        "auth_modes": ["none"] if kind == "fake" else ["native_account"],
        "billing_modes": ["local"] if kind == "fake" else ["subscription"],
        "permissions": {"filesystem": ["approved_workspace"], "network": [], "process": [], "credential_classes": []},
        "platforms": [{"os": "linux", "architectures": ["x86_64", "aarch64"], "verification": "not_tested"},
                      {"os": "macos", "architectures": ["arm64", "x86_64"], "verification": "not_tested"}], "docs": {}, "extensions": {"asterun.compatibility": {"backend_kind": kind}},
    }


def builtin_registry():
    registry = PluginRegistry()
    for kind, entry in BUILTINS.items():
        registry.register_builtin(builtin_manifest(kind), factory=entry["factory"])
    return registry


def build_registry(config):
    registry = builtin_registry()
    if config.schema_version == 2:
        for entry in BUILTINS.values():
            registry.set_enabled(entry["plugin_id"], False)
    for plugin_id, options in getattr(config, "plugins", {}).items():
        if builtin_kind(plugin_id) is not None and "manifest_path" not in options:
            registry.set_enabled(plugin_id, options["enabled"])
        else:
            # 外部包可以替代相同品牌的兼容内置实现，但必须显式注册和固定摘要。
            if builtin_kind(plugin_id) is not None:
                registry._registrations.pop(plugin_id)
            registration = registry.register_external(
                options["manifest_path"], runner=options["runner"],
                installation_path=options["installation_path"], installation_sha256=options["installation_sha256"],
                enabled=options["enabled"], runtime_path=options.get("runtime_path"), runtime_sha256=options.get("runtime_sha256"),
            )
            if registration.plugin_id != plugin_id:
                raise AsterunError(INVALID_CONFIG, "配置插件 ID 与 manifest 不匹配")
    return registry


def build_compatibility_backends(config):
    from asterun.backends.base import DisabledBackend

    registry = build_registry(config)
    backends = {}
    for name, item in config.backends.items():
        plugin_id = getattr(item, "plugin_id", None) or builtin_plugin_id(item.kind)
        registration = registry.get(plugin_id)
        if registration.source == "external":
            from asterun.plugins.worker_backend import WorkerBackend
            connection = config.connections[item.connection_ref]
            provider = config.provider_accounts.get(connection["provider_account_ref"])
            backends[name] = WorkerBackend(registry, registration, item, connection, provider)
        elif not item.enabled or not registration.enabled:
            backends[name] = DisabledBackend(item)
        else:
            backends[name] = registry.load_factory(plugin_id)(item)
    return backends
