"""配置 v1 到 v2 的可审阅迁移；从不覆盖原配置或触碰状态库。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from asterun.config import load_config
from asterun.config_v2 import parse_v2
from asterun.errors import AsterunError, INVALID_CONFIG, REVISION_CONFLICT
from asterun.plugins.builtins import GROK_EXECUTION_OPTIONS, builtin_plugin_id


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise AsterunError(INVALID_CONFIG, "无法读取迁移源配置") from exc


def migrate_v1_to_v2(path: str | Path) -> dict[str, Any]:
    """纯 dry-run；供应商账户引用是新占位，不使用核心 account_ref。"""
    path = Path(path).absolute()
    original = _read(path)
    config = load_config(path)
    if config.schema_version != 1:
        raise AsterunError(INVALID_CONFIG, "迁移只接受 v1 配置")
    if _read(path) != original:
        raise AsterunError(REVISION_CONFLICT, "读取期间源配置已改变")
    proposed = json.loads(original.decode("utf-8"))
    proposed.update(schema_version=2, revision=config.revision + 1,
                    plugins={}, provider_accounts={}, billing_pools={}, connections={}, backends={})
    # 目标文件可位于另一目录；工作区路径必须继续指向原来解析后的根。
    proposed["workspaces"] = {
        alias: {"root": str(item.root), "allow_non_git": item.allow_non_git}
        for alias, item in config.workspaces.items()
    }
    warnings = []
    original_enabled = {}
    for alias, backend in config.backends.items():
        plugin_id = builtin_plugin_id(backend.kind)
        suffix = hashlib.sha256(alias.encode("utf-8")).hexdigest()[:20]
        connection_ref, provider_ref, pool_ref = "connection_" + suffix, "provider_" + suffix, "pool_" + suffix
        is_fake = backend.kind == "fake"
        enabled = backend.enabled if is_fake else False
        proposed["plugins"].setdefault(plugin_id, {"enabled": False})
        proposed["plugins"][plugin_id]["enabled"] |= enabled
        proposed["provider_accounts"][provider_ref] = {"provider": backend.kind, "credential_ref": None,
                                                       "credential_revision": 1, "revoked": False}
        proposed["billing_pools"][pool_ref] = {
            "billing_mode": "local" if is_fake else "unknown", "remaining": None,
            "paid_overage_allowed": False, "overage_verified": False,
            "allow_unknown_subscription": False, "risk_acceptance": None,
        }
        if is_fake:
            proposed["billing_pools"][pool_ref].update(
                meter="requests", local_limit=100, max_concurrency=1,
                window_id="local", estimate_per_action=1,
            )
        options = {key: value for key, value in {"bin": backend.bin, "home": backend.home, "model": backend.model}.items() if value is not None}
        if backend.kind == "grok":
            options.update({key: getattr(backend, key) for key in GROK_EXECUTION_OPTIONS
                            if getattr(backend, key) is not None})
        if backend.kind == "codex" and backend.desktop_projects is not None:
            options["desktop_projects"] = backend.desktop_projects
        proposed["connections"][connection_ref] = {
            "plugin_id": plugin_id, "enabled": enabled, "provider_account_ref": provider_ref,
            "runtime_ref": "runtime:local" if is_fake else "runtime:configure",
            "auth_mode": "none" if is_fake else "native_account",
            "billing_pool_refs": [pool_ref], "options": options, "upstream_version": None,
        }
        proposed["backends"][alias] = {"connection_ref": connection_ref}
        original_enabled[alias] = backend.enabled
        if not is_fake:
            warnings.append({
                "backend": alias, "original_enabled": backend.enabled, "migrated_enabled": False,
                "reason": "新供应商账户/运行环境/计费池未核验；保留原生参数和 live 门闩，v2 连接需核验后显式启用",
            })
    proposed["routing"] = {
        "automatic_paid_fallback": False, "automatic_account_switch": False,
        "default_order": [], "rules": [],
    }
    parse_v2(proposed, path)
    return {
        "source": str(path), "source_sha256": hashlib.sha256(original).hexdigest(),
        "source_unchanged": True, "dry_run": True, "config": proposed,
        "original_backend_enabled": original_enabled, "warnings": warnings,
        "compatibility": "原 v1 文件、任务和原生会话不改写；v2 使用新 revision/配置摘要，旧记录不重算",
        "rollback": "继续使用原 v1 文件；不要把旧二进制指向后续升级的新状态库",
    }


def _create_private(path: Path, data: bytes) -> None:
    """同目录临时文件写完后以 link 排他发布，已存在目标永不覆盖。"""
    temporary = None
    try:
        fd, name = tempfile.mkstemp(prefix=".asterun-migration-", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    except OSError as exc:
        raise AsterunError(INVALID_CONFIG, "迁移目标/备份必须为可创建的全新文件；已有内容未覆盖") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_v2_migration(source: str | Path, destination: str | Path, backup: str | Path, *,
                       expected_source_sha256: str) -> dict[str, Any]:
    source, destination, backup = (Path(value).absolute() for value in (source, destination, backup))
    paths = [value.resolve() for value in (source, destination, backup)]
    if len(set(paths)) != 3:
        raise AsterunError(INVALID_CONFIG, "迁移源、目标与备份必须为不同路径")
    if any(path.exists() or path.is_symlink() for path in (destination, backup)):
        raise AsterunError(INVALID_CONFIG, "迁移目标和备份必须是全新文件")
    report = migrate_v1_to_v2(source)
    if report["source_sha256"] != expected_source_sha256:
        raise AsterunError(REVISION_CONFLICT, "源配置与已审阅的迁移摘要不一致")
    original = _read(source)
    if hashlib.sha256(original).hexdigest() != expected_source_sha256:
        raise AsterunError(REVISION_CONFLICT, "备份前源配置已改变")
    _create_private(backup, original)
    if _read(source) != original:
        raise AsterunError(REVISION_CONFLICT, "备份后源配置已改变；保留备份，未写入 v2 目标")
    encoded = (json.dumps(report["config"], ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _create_private(destination, encoded)
    return {**report, "dry_run": False, "destination": str(destination), "backup": str(backup),
            "destination_sha256": hashlib.sha256(encoded).hexdigest()}
