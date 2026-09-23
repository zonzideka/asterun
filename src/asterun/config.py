from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from asterun.errors import (
    CONFIG_REQUIRED,
    INVALID_CONFIG,
    UNKNOWN_FIELD,
    UNKNOWN_PROJECT,
    AsterunError,
)
from asterun.paths import resolve_allowed_root
from asterun.plugins.builtins import BUILTINS, GROK_EXECUTION_OPTIONS, validate_grok_execution_options

SCHEMA_VERSION = 1
CONFIG_KEYS = {
    "schema_version",
    "revision",
    "workspaces",
    "backends",
    "default_backend",
    "workflow",
    "scheduler",
    "entries",
    "approval_policy",
}
WORKSPACE_KEYS = {"root", "allow_non_git"}
BACKEND_KEYS = {"kind", "enabled", "bin", "home", "model", "desktop_projects", *GROK_EXECUTION_OPTIONS}
WORKFLOW_KEYS = {"preset", "require_review", "review_backend", "approved_substitute", "max_repairs"}
SCHEDULER_KEYS = {"max_queue", "per_backend_concurrency", "max_runs_per_task"}
ENTRIES_KEYS = {"cli", "mcp"}
KNOWN_BACKEND_KINDS = set(BUILTINS)


@dataclass(slots=True)
class WorkspaceBinding:
    alias: str
    root: Path
    allow_non_git: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "root": str(self.root),
            "allow_non_git": self.allow_non_git,
            "is_git": (self.root / ".git").exists(),
        }


@dataclass(slots=True)
class BackendConfig:
    name: str
    kind: str
    enabled: bool = True
    bin: str | None = None
    home: str | None = None
    model: str | None = None
    connection_ref: str | None = None
    plugin_id: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    execution_profile: str | None = None
    max_turns: int | None = None
    timeout_seconds: int | None = None
    desktop_projects: bool | None = None
    session_sync_home: str | None = None

    @property
    def executable(self) -> bool:
        return self.enabled and self.kind == "fake"

    def to_dict(self) -> dict[str, Any]:
        result = {
            "name": self.name,
            "kind": self.kind,
            "enabled": self.enabled,
            "bin": self.bin,
            "executable": self.executable,
            "is_real_backend": self.kind != "fake",
        }
        if self.kind in {"grok", "antigravity"}:
            result.update(home=self.home, model=self.model)
        if self.kind == "grok":
            result.update({key: getattr(self, key) for key in GROK_EXECUTION_OPTIONS
                           if getattr(self, key) is not None})
        if self.kind == "codex" and self.desktop_projects is not None:
            result["desktop_projects"] = self.desktop_projects
        if self.connection_ref is not None:
            result.update(connection_ref=self.connection_ref, plugin_id=self.plugin_id, options=self.options)
        return result


@dataclass(slots=True)
class WorkflowConfig:
    preset: str = "none"
    require_review: bool = False
    review_backend: str | None = None
    approved_substitute: str | None = None
    max_repairs: int = 2

    def to_dict(self) -> dict[str, Any]:
        return {
            "preset": self.preset,
            "require_review": self.require_review,
            "review_backend": self.review_backend,
            "approved_substitute": self.approved_substitute,
            "max_repairs": self.max_repairs,
        }


@dataclass(slots=True)
class SchedulerConfig:
    max_queue: int = 32
    per_backend_concurrency: int = 1
    max_runs_per_task: int = 4

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_queue": self.max_queue,
            "per_backend_concurrency": self.per_backend_concurrency,
            "max_runs_per_task": self.max_runs_per_task,
        }


@dataclass(slots=True)
class EntriesConfig:
    cli: bool = True
    mcp: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"cli": {"enabled": self.cli}, "mcp": {"enabled": self.mcp}}

    def enabled(self, entry: str) -> bool:
        if entry == "cli":
            return self.cli
        if entry == "mcp":
            return self.mcp
        return True


@dataclass(slots=True)
class AsterunConfig:
    schema_version: int
    revision: int
    workspaces: dict[str, WorkspaceBinding]
    backends: dict[str, BackendConfig]
    default_backend: str | None
    source_path: Path | None
    workflow: WorkflowConfig
    scheduler: SchedulerConfig
    entries: EntriesConfig
    approval_policy: dict[str, Any] = field(default_factory=lambda: {"mode": "manual", "rules": []})
    plugins: dict[str, Any] = field(default_factory=dict)
    provider_accounts: dict[str, Any] = field(default_factory=dict)
    billing_pools: dict[str, Any] = field(default_factory=dict)
    connections: dict[str, Any] = field(default_factory=dict)
    routing: dict[str, Any] = field(default_factory=dict)

    def get_workspace(self, alias: str) -> WorkspaceBinding:
        try:
            return self.workspaces[alias]
        except KeyError as exc:
            raise AsterunError(
                UNKNOWN_PROJECT,
                f"未知项目/工作区：{alias}",
                next_action="使用 config.json 中已声明的 workspace 别名，不要依赖作者机器上的默认项目清单",
                details={"workspace": alias, "known": sorted(self.workspaces)},
            ) from exc

    def get_backend(self, name: str | None) -> BackendConfig:
        chosen = name or self.default_backend
        if not chosen:
            raise AsterunError(
                CONFIG_REQUIRED,
                "未指定后端，且配置没有 default_backend",
                next_action="在请求中提供 backend，或在配置中设置 default_backend",
            )
        try:
            return self.backends[chosen]
        except KeyError as exc:
            raise AsterunError(
                INVALID_CONFIG,
                f"未知后端：{chosen}",
                next_action="使用配置中已声明的后端名",
                details={"backend": chosen, "known": sorted(self.backends)},
            ) from exc

    def to_dict(self) -> dict[str, Any]:
        result = {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "source_path": None if self.source_path is None else str(self.source_path),
            "workspaces": {name: item.to_dict() for name, item in self.workspaces.items()},
            "backends": {name: item.to_dict() for name, item in self.backends.items()},
            "default_backend": self.default_backend,
            "workflow": self.workflow.to_dict(),
            "scheduler": self.scheduler.to_dict(),
            "entries": self.entries.to_dict(),
            "approval_policy": self.approval_policy,
        }
        # v1 的摘要/历史任务依赖此投影，不能注入空的新字段改变其语义。
        if self.schema_version == 2:
            result.update(plugins=self.plugins, provider_accounts=self.provider_accounts,
                          billing_pools=self.billing_pools, connections=self.connections, routing=self.routing)
        return result


def _reject_unknown(container: dict[str, Any], allowed: set[str], where: str) -> None:
    extra = sorted(set(container) - allowed)
    if extra:
        raise AsterunError(
            UNKNOWN_FIELD,
            f"{where} 含有未知字段：{', '.join(extra)}",
            next_action="删除未知字段后重新校验配置",
            details={"where": where, "unknown_fields": extra},
        )


def _require_mapping(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AsterunError(INVALID_CONFIG, f"{where} 必须是对象")
    return value


def load_config(path: Path) -> AsterunConfig:
    if not path.exists():
        raise AsterunError(
            CONFIG_REQUIRED,
            f"配置文件不存在：{path}",
            next_action="传入 --config，或设置 ASTERUN_CONFIG 指向一份显式配置",
            details={"path": str(path)},
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AsterunError(
            INVALID_CONFIG,
            f"配置不是合法 JSON：{exc}",
            next_action="修正 config.json 的 JSON 语法",
            details={"path": str(path)},
        ) from exc
    if not isinstance(raw, dict):
        raise AsterunError(INVALID_CONFIG, "配置根节点必须是对象")
    schema_version = raw.get("schema_version")
    from asterun.config_v2 import V2_KEYS, parse_v2
    _reject_unknown(raw, CONFIG_KEYS | (V2_KEYS if schema_version == 2 else set()), "config")
    if type(schema_version) is not int or schema_version not in {1, 2}:
        raise AsterunError(
            INVALID_CONFIG,
            f"不支持的 schema_version：{schema_version}（当前接受 1 或 2）",
            details={"schema_version": schema_version},
        )
    revision = raw.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise AsterunError(INVALID_CONFIG, "revision 必须是正整数")

    workspaces_raw = _require_mapping(raw.get("workspaces", {}), "workspaces")
    workspaces: dict[str, WorkspaceBinding] = {}
    for alias, item in workspaces_raw.items():
        body = _require_mapping(item, f"workspaces.{alias}")
        _reject_unknown(body, WORKSPACE_KEYS, f"workspaces.{alias}")
        if "root" not in body:
            raise AsterunError(INVALID_CONFIG, f"workspaces.{alias} 缺少 root")
        root = Path(str(body["root"]))
        if not root.is_absolute():
            root = (path.parent / root).resolve()
        allow_non_git = body.get("allow_non_git", True)
        if not isinstance(allow_non_git, bool):
            raise AsterunError(INVALID_CONFIG, f"workspaces.{alias}.allow_non_git 必须是布尔值")
        resolved = resolve_allowed_root(root)
        if not allow_non_git and not (resolved / ".git").exists():
            raise AsterunError(
                INVALID_CONFIG,
                f"工作区 {alias} 要求 Git 仓库，但 {resolved} 没有 .git",
                next_action="将该工作区设为 allow_non_git=true，或指向真实 Git 根",
            )
        workspaces[str(alias)] = WorkspaceBinding(
            alias=str(alias),
            root=resolved,
            allow_non_git=allow_non_git,
        )

    backends_raw = _require_mapping(raw.get("backends", {}), "backends")
    v2_backends, v2_namespaces = parse_v2(raw, path) if schema_version == 2 else ({}, {})
    backends: dict[str, BackendConfig] = {}
    for name, item in backends_raw.items():
        if schema_version == 2:
            backends[str(name)] = BackendConfig(name=str(name), **v2_backends[name])
            continue
        body = _require_mapping(item, f"backends.{name}")
        _reject_unknown(body, BACKEND_KEYS, f"backends.{name}")
        kind = body.get("kind")
        if not isinstance(kind, str) or kind not in KNOWN_BACKEND_KINDS:
            raise AsterunError(
                INVALID_CONFIG,
                f"backends.{name}.kind 无效：{kind}",
                details={"known_kinds": sorted(KNOWN_BACKEND_KINDS)},
            )
        enabled = body.get("enabled", True)
        if not isinstance(enabled, bool):
            raise AsterunError(INVALID_CONFIG, f"backends.{name}.enabled 必须是布尔值")
        bin_raw = body.get("bin")
        if bin_raw is None:
            bin_value = None
        elif isinstance(bin_raw, str):
            bin_value = bin_raw or None
        else:
            raise AsterunError(INVALID_CONFIG, f"backends.{name}.bin 必须是字符串")
        from asterun.plugins.builtins import compatibility_options
        if (set(body) - {"kind", "enabled"}) - compatibility_options(kind):
            raise AsterunError(INVALID_CONFIG, f"backends.{name} 含有不适用于 {kind} 的后端选项")
        if kind == "grok":
            validate_grok_execution_options(body, f"backends.{name}")
        if "desktop_projects" in body and type(body["desktop_projects"]) is not bool:
            raise AsterunError(INVALID_CONFIG, f"backends.{name}.desktop_projects 必须为布尔值")
        home_value = body.get("home")
        if "home" in body and (
            not isinstance(home_value, str)
            or not home_value.strip()
            or "\x00" in home_value
            or not Path(home_value).is_absolute()
        ):
            raise AsterunError(INVALID_CONFIG, f"backends.{name}.home 必须是显式绝对路径")
        model_value = body.get("model")
        if "model" in body and (
            not isinstance(model_value, str)
            or not model_value.strip()
            or model_value != model_value.strip()
            or "\x00" in model_value
        ):
            raise AsterunError(INVALID_CONFIG, f"backends.{name}.model 必须是非空模型 ID，且不能含首尾空白")
        backends[str(name)] = BackendConfig(
            name=str(name),
            kind=str(kind),
            enabled=enabled,
            bin=bin_value,
            home=home_value,
            model=model_value,
            desktop_projects=body.get("desktop_projects"),
            **{key: body[key] for key in GROK_EXECUTION_OPTIONS if key in body},
        )

    default_backend = raw.get("default_backend")
    if default_backend is not None:
        if not isinstance(default_backend, str) or not default_backend:
            raise AsterunError(INVALID_CONFIG, "default_backend 必须是非空字符串")
        if default_backend not in backends:
            raise AsterunError(INVALID_CONFIG, f"default_backend 指向未知后端：{default_backend}")

    workflow = _load_workflow(raw.get("workflow"))
    scheduler = _load_scheduler(raw.get("scheduler"))
    entries = _load_entries(raw.get("entries"))
    if workflow.review_backend and workflow.review_backend not in backends:
        raise AsterunError(INVALID_CONFIG, f"workflow.review_backend 指向未知后端：{workflow.review_backend}")

    from asterun.approval_policy import validate_policy
    approval_policy = validate_policy(raw.get("approval_policy"), workspaces, backends)

    return AsterunConfig(
        schema_version=schema_version,
        revision=revision,
        workspaces=workspaces,
        backends=backends,
        default_backend=default_backend,
        source_path=path,
        workflow=workflow,
        scheduler=scheduler,
        entries=entries,
        approval_policy=approval_policy,
        **v2_namespaces,
    )


def _load_workflow(raw: object) -> WorkflowConfig:
    if raw is None:
        return WorkflowConfig()
    body = _require_mapping(raw, "workflow")
    _reject_unknown(body, WORKFLOW_KEYS, "workflow")
    preset = str(body.get("preset") or "none")
    if preset not in {"none", "quality"}:
        raise AsterunError(INVALID_CONFIG, "workflow.preset 只能是 none 或 quality")
    require_review = body.get("require_review", False)
    if not isinstance(require_review, bool):
        raise AsterunError(INVALID_CONFIG, "workflow.require_review 必须是布尔值")
    review_backend = body.get("review_backend")
    if review_backend is not None and (not isinstance(review_backend, str) or not review_backend):
        raise AsterunError(INVALID_CONFIG, "workflow.review_backend 必须是非空字符串")
    substitute = body.get("approved_substitute")
    if substitute is not None and (not isinstance(substitute, str) or not substitute):
        raise AsterunError(INVALID_CONFIG, "workflow.approved_substitute 必须是非空字符串")
    max_repairs = body.get("max_repairs", 2)
    if not isinstance(max_repairs, int) or isinstance(max_repairs, bool) or max_repairs < 0:
        raise AsterunError(INVALID_CONFIG, "workflow.max_repairs 必须是非负整数")
    return WorkflowConfig(
        preset=preset,
        require_review=require_review,
        review_backend=None if review_backend is None else str(review_backend),
        approved_substitute=None if substitute is None else str(substitute),
        max_repairs=max_repairs,
    )


def _load_scheduler(raw: object) -> SchedulerConfig:
    if raw is None:
        return SchedulerConfig()
    body = _require_mapping(raw, "scheduler")
    _reject_unknown(body, SCHEDULER_KEYS, "scheduler")
    max_queue = body.get("max_queue", 32)
    concurrency = body.get("per_backend_concurrency", 1)
    max_runs = body.get("max_runs_per_task", 4)
    for name, value in (
        ("max_queue", max_queue),
        ("per_backend_concurrency", concurrency),
        ("max_runs_per_task", max_runs),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise AsterunError(INVALID_CONFIG, f"scheduler.{name} 必须是正整数")
    return SchedulerConfig(
        max_queue=int(max_queue),
        per_backend_concurrency=int(concurrency),
        max_runs_per_task=int(max_runs),
    )


def _load_entries(raw: object) -> EntriesConfig:
    if raw is None:
        return EntriesConfig()
    body = _require_mapping(raw, "entries")
    _reject_unknown(body, ENTRIES_KEYS, "entries")
    cli = _load_entry_flag(body.get("cli"), "entries.cli")
    mcp = _load_entry_flag(body.get("mcp"), "entries.mcp")
    return EntriesConfig(cli=cli, mcp=mcp)


def _load_entry_flag(raw: object, where: str) -> bool:
    if raw is None:
        return True
    if isinstance(raw, bool):
        return raw
    body = _require_mapping(raw, where)
    _reject_unknown(body, {"enabled"}, where)
    enabled = body.get("enabled", True)
    if not isinstance(enabled, bool):
        raise AsterunError(INVALID_CONFIG, f"{where}.enabled 必须是布尔值")
    return enabled
