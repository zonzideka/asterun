"""Antigravity CLI 专用配置；权限策略不是操作系统隔离。"""
from __future__ import annotations

import argparse
from collections.abc import Mapping
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import sys
import tempfile

from .errors import AsterunError, INVALID_CONFIG

PROFILE = "workspace-read-v1"
SETTINGS = {
    "toolPermission": "request-review",
    "artifactReviewPolicy": "asks-for-review",
    "allowNonWorkspaceAccess": False,
    "useG1Credits": False,
    "notifications": False,
    "showTips": False,
    "showFeedbackSurvey": False,
    "permissions": {
        "allow": [],
        "ask": [],
        "deny": [
            "write_file(*)", "read_url(*)", "execute_url(*)", "command(*)",
            "unsandboxed(*)", "mcp(*)",
        ],
    },
}
SETTINGS_TEXT = json.dumps(SETTINGS, ensure_ascii=False, indent=2) + "\n"
_KEEP_ENV = frozenset((
    "USER", "LOGNAME", "PATH", "TMPDIR", "LANG", "LC_ALL",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy", "SSL_CERT_FILE",
))
_EXTENSION_DIRS = frozenset(("plugins", "skills", "agents", "rules", "hooks"))
_STATE_DIRS = frozenset((
    "cache", "logs", "conversations", "brain", "crashes", "knowledge", "log", "updater",
))
_STATE_FILES = frozenset((
    "cli.log", "agy.log", "conversation_summaries.db", "conversation_summaries.db-shm",
    "conversation_summaries.db-wal", "jetski_state.pbtxt", "jetbox_summaries_proto.pb", "last_check.timestamp", "installation_id",
))
_CREDENTIAL_FILES = frozenset(("auth.json", "credentials.json", "oauth_creds.json", "antigravity-oauth-token"))
# Native state files that may hold conversation content stay owner-only.
_PRIVATE_STATE_FILES = frozenset(("jetski_state.pbtxt", "jetbox_summaries_proto.pb"))
_MAX_STATE_ENTRIES = 10000
# The CLI ships builtin skills, a webm_encoder helper and a Playwright cache
# directory whose bytes change with every release. They are checked by
# structure (type, owner, mode, size, names), not by a per-version digest.
# Operators who want byte pinning set PINS_ENV to an absolute path of a JSON
# file {"builtin": {"relative/path": "<sha256>"}, "webm_encoder":
# {"size": int, "sha256": "<sha256>"}}; when present it is enforced exactly.
PINS_ENV = "ASTERUN_ANTIGRAVITY_PINS"
_MAX_ENCODER_BYTES = 64 * 1024 * 1024
_MAX_BUILTIN_ENTRIES = 512
_MAX_BUILTIN_FILE_BYTES = 131072
_BUILTIN_TOP_FILES = frozenset((".checksum", "keep.txt"))
_BUILTIN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
_CACHE_VERSION = re.compile(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}")
_MAX_CACHE_VERSIONS = 8
# Native sparse persistence removes these values because they equal the
# documented defaults, including request-review. It does not remove deny
# rules or nondefault false display preferences.
_OPTIONAL_DEFAULTS = {
    "toolPermission": "request-review",
    "artifactReviewPolicy": "asks-for-review", "allowNonWorkspaceAccess": False,
    "useG1Credits": False, "notifications": False,
}


def _host_platform() -> tuple[str, str]:
    """返回 (sys.platform, 机器名)。Linux 的 amd64 与 x86_64 共用同一固定摘要。"""
    machine = platform.machine().lower()
    if machine == "amd64":
        machine = "x86_64"
    return sys.platform, machine


def _allowed_home_entries() -> frozenset[str]:
    system, _machine = _host_platform()
    if system == "darwin":
        return frozenset({".gemini", "Library"})
    if system == "linux":
        return frozenset({".gemini", ".cache"})
    return frozenset({".gemini"})


def _invalid(message: str) -> AsterunError:
    return AsterunError(INVALID_CONFIG, message,
        next_action="使用 asterun-antigravity prepare --home 创建新的独立目录；"
                    "使用无扩展和上下文规则的独立工作区或快照，不覆盖已有原生配置")


def _path(path: str | Path) -> Path:
    if not isinstance(path, (str, Path)) or not str(path) or not Path(path).is_absolute():
        raise _invalid("Antigravity home 必须是显式绝对路径")
    target = Path(path)
    try:
        if target.is_symlink():
            raise _invalid("Antigravity home 不能是符号链接")
        return target.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise _invalid(f"无法解析 Antigravity home：{type(exc).__name__}") from exc


def _metadata(path: Path, *, directory: bool, private: bool = True) -> None:
    info = path.lstat()
    valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    # Ordinary native caches may be 0644 inside the private HOME. They must
    # never grant write access to other users, or point outside that HOME.
    forbidden = 0o077 if private else 0o022
    if not valid_type or info.st_uid != os.getuid() or info.st_mode & forbidden:
        raise _invalid(f"Antigravity 路径类型、所有者或权限不正确：{path}")


def _empty_source(path: Path, *, private: bool = True) -> None:
    info = path.lstat()
    if stat.S_ISREG(info.st_mode) and info.st_size == 0:
        _metadata(path, directory=False, private=private)
        return
    if stat.S_ISDIR(info.st_mode) and not any(path.iterdir()):
        _metadata(path, directory=True, private=private)
        return
    raise _invalid(f"受限 Antigravity 环境发现额外配置或扩展来源：{path}")


def _state_tree(path: Path) -> None:
    """只读取元数据；原生运行历史、缓存和认证内容不进入检查结果。"""
    remaining = _MAX_STATE_ENTRIES
    pending = [path]
    while pending:
        current = pending.pop()
        remaining -= 1
        if remaining < 0:
            raise _invalid("Antigravity 原生状态目录超出检查上限")
        info = current.lstat()
        directory = stat.S_ISDIR(info.st_mode)
        _metadata(current, directory=directory,
            private=current.name in _CREDENTIAL_FILES)
        if directory:
            for entry in current.iterdir():
                if len(pending) >= remaining:
                    raise _invalid("Antigravity 原生状态目录超出检查上限")
                pending.append(entry)


def _parse_json_object(content: str | bytes) -> dict[str, object]:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    if len(content) > 16384:
        raise _invalid("Antigravity 配置超过检查大小限制")
    value = json.loads(content, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise _invalid("Antigravity 配置必须是 JSON 对象")
    return value


def _json_object(path: Path) -> dict[str, object]:
    if path.stat().st_size > 16384:
        raise _invalid("Antigravity 配置超过检查大小限制")
    return _parse_json_object(path.read_text(encoding="utf-8"))


def _equal_json(left: object, right: object) -> bool:
    return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)


def _read_permission(workspace: Path) -> str:
    text = str(workspace)
    if any(char in text for char in "*?[]()\\") or any(ord(char) < 32 for char in text):
        raise _invalid("Antigravity 读取工作区路径含权限语法分隔符或通配符")
    return f"read_file({text})"


def _allowed_workspace(permissions: dict[str, object]) -> Path | None:
    rules = permissions.get("allow", [])
    if rules == []:
        return None
    if not isinstance(rules, list) or len(rules) != 1 or not isinstance(rules[0], str):
        raise _invalid("Antigravity allow 仅允许一个绑定工作区的读取规则")
    rule = rules[0]
    if not rule.startswith("read_file(") or not rule.endswith(")"):
        raise _invalid("Antigravity allow 包含非工作区读取权限")
    root = rule[len("read_file("):-1]
    target = validate_workspace(root)
    _metadata(target, directory=True, private=False)
    if rule != _read_permission(target):
        raise _invalid("Antigravity 读取权限必须使用规范绝对工作区路径")
    return target


def _validate_settings_object(actual: dict[str, object], *, legacy_strict: bool = False) -> None:
    if set(actual) - (set(SETTINGS) | {"trustedWorkspaces"}):
        raise _invalid("Antigravity 配置已偏离 workspace-read-v1：出现未获准字段")
    if "trustedWorkspaces" in actual:
        trusted = actual["trustedWorkspaces"]
        if not isinstance(trusted, list) or len(trusted) != 1 or not isinstance(trusted[0], str):
            raise _invalid("Antigravity 受限配置仅允许信任一个独立工作区")
        target = validate_workspace(trusted[0])
        _metadata(target, directory=True, private=False)
    for key, expected in SETTINGS.items():
        if key == "permissions":
            continue
        if key == "toolPermission":
            if not legacy_strict and actual.get(key) == "strict":
                raise AsterunError(INVALID_CONFIG,
                    "Antigravity 旧 strict 配置需要显式迁移；原配置未修改",
                    next_action="停止使用此 HOME 的原生 CLI 后，运行 asterun-antigravity "
                                "upgrade-permissions --home PATH --backup PATH；"
                                "备份须放在 HOME 外已有的私有目录中")
            expected = "strict" if legacy_strict else expected
        if not _equal_json(actual.get(key, _OPTIONAL_DEFAULTS.get(key)), expected):
            raise _invalid("Antigravity 配置已偏离 workspace-read-v1；拒绝启动")
    permissions = actual.get("permissions")
    if not isinstance(permissions, dict) or set(permissions) - {"allow", "ask", "deny"}:
        raise _invalid("Antigravity 权限配置不正确")
    if not _equal_json(permissions.get("deny"), SETTINGS["permissions"]["deny"]) or permissions.get("ask", []) != []:
        raise _invalid("Antigravity deny 权限配置已偏离 workspace-read-v1")
    read_root = _allowed_workspace(permissions)
    if read_root is not None and "trustedWorkspaces" in actual and Path(actual["trustedWorkspaces"][0]).resolve(strict=True) != read_root:
        raise _invalid("Antigravity 原生信任与读取权限的工作区不一致")


def _validate_settings(path: Path, *, legacy_strict: bool = False) -> None:
    _validate_settings_object(_json_object(path), legacy_strict=legacy_strict)


def _load_pins() -> dict[str, object] | None:
    """Optional operator digest pins; absent means structural checks only."""
    value = os.environ.get(PINS_ENV)
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        raise _invalid(f"{PINS_ENV} 必须是非符号链接的绝对路径")
    _metadata(path, directory=False, private=False)
    pins = _json_object(path)
    if set(pins) - {"builtin", "webm_encoder"}:
        raise _invalid("Antigravity 固定清单包含未知字段")
    digest = re.compile(r"[0-9a-f]{64}")
    builtin = pins.get("builtin")
    if builtin is not None and (not isinstance(builtin, dict) or not all(
            isinstance(key, str) and isinstance(item, str) and digest.fullmatch(item)
            for key, item in builtin.items())):
        raise _invalid("Antigravity 固定清单 builtin 格式不正确")
    encoder = pins.get("webm_encoder")
    if encoder is not None and (
            not isinstance(encoder, dict) or set(encoder) - {"size", "sha256"}
            or not isinstance(encoder.get("sha256"), str) or not digest.fullmatch(encoder["sha256"])
            or ("size" in encoder and (type(encoder["size"]) is not int or encoder["size"] <= 0))):
        raise _invalid("Antigravity 固定清单 webm_encoder 格式不正确")
    return pins


def _builtin_allowed(parts: tuple[str, ...], *, directory: bool) -> bool:
    if directory:
        return parts[0] == "skills" and len(parts) <= 4 and all(_BUILTIN_NAME.fullmatch(p) for p in parts)
    if len(parts) == 1:
        return parts[0] in _BUILTIN_TOP_FILES
    return (parts[0] == "skills" and 3 <= len(parts) <= 5 and parts[-1].endswith(".md")
            and all(_BUILTIN_NAME.fullmatch(p) for p in parts))


def _validate_builtin(path: Path, pins: dict[str, object] | None = None) -> None:
    """Version-independent check of the CLI's own builtin skills.

    Only owned, non-executable Markdown documents under skills/<name>/ plus
    the CLI's .checksum and keep.txt markers are accepted; symlinks, other
    file types and oversized files are rejected. Optional pins compare bytes.
    """
    _metadata(path, directory=True, private=False)
    expected = None if pins is None else pins.get("builtin")
    seen: dict[str, Path] = {}
    count = 0
    pending = [path]
    while pending:
        current = pending.pop()
        for entry in current.iterdir():
            count += 1
            if count > _MAX_BUILTIN_ENTRIES:
                raise _invalid("Antigravity 内置目录超出检查上限")
            parts = entry.relative_to(path).parts
            info = entry.lstat()
            directory = stat.S_ISDIR(info.st_mode)
            if not _builtin_allowed(parts, directory=directory):
                raise _invalid(f"Antigravity 内置目录发现额外扩展来源：{entry}")
            _metadata(entry, directory=directory, private=False)
            if directory:
                pending.append(entry)
                continue
            if info.st_mode & 0o111 or info.st_size > _MAX_BUILTIN_FILE_BYTES:
                raise _invalid(f"Antigravity 内置文件权限或大小不正确：{entry}")
            seen["/".join(parts)] = entry
    if ".checksum" not in seen:
        raise _invalid("Antigravity 内置扩展缺少 CLI 生成的 .checksum；请用官方 agy 重新初始化")
    if expected is not None:
        if set(seen) != set(expected):
            raise _invalid("Antigravity 内置扩展与固定清单不一致")
        for name, entry in seen.items():
            if hashlib.sha256(entry.read_bytes()).hexdigest() != expected[name]:
                raise _invalid("Antigravity 内置扩展偏离固定清单哈希")


def _validate_bin(path: Path, pins: dict[str, object] | None = None) -> None:
    _metadata(path, directory=True, private=False)
    names = {entry.name for entry in path.iterdir()}
    if names - {"webm_encoder"}:
        raise _invalid("Antigravity bin 目录包含未知可执行文件")
    if not names:
        return
    encoder = path / "webm_encoder"
    # Owned regular file that no other user can write; never symlinked.
    _metadata(encoder, directory=False, private=False)
    size = encoder.stat().st_size
    if not 0 < size <= _MAX_ENCODER_BYTES:
        raise _invalid("Antigravity webm_encoder 大小超出允许范围")
    pin = None if pins is None else pins.get("webm_encoder")
    if pin is None:
        return
    if "size" in pin and size != pin["size"]:
        raise _invalid("Antigravity webm_encoder 大小偏离固定清单")
    # Validation does not grant permission to invoke it or enable browser tools.
    with encoder.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    if digest != pin["sha256"]:
        raise _invalid("Antigravity webm_encoder 偏离固定清单哈希")


def _validate_uuid_state(path: Path, suffix: str, *, private_files: bool = False) -> None:
    _metadata(path, directory=True, private=False)
    for number, entry in enumerate(path.iterdir()):
        if number >= _MAX_STATE_ENTRIES or not re.fullmatch(
            r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}" + re.escape(suffix), entry.name
        ):
            raise _invalid(f"Antigravity {path.name} 目录包含未知状态来源或超出检查上限")
        _metadata(entry, directory=False, private=private_files)


def _validate_customizations(path: Path) -> None:
    _metadata(path, directory=True)
    for source in path.iterdir():
        if source.name == "config.json":
            _metadata(source, directory=False)
            config = _json_object(source)
            settings = config.get("userSettings")
            if set(config) != {"userSettings"} or not isinstance(settings, dict) or set(settings) != {"remoteControlHostname"}:
                raise _invalid("Antigravity 全局 config.json 包含额外配置")
            hostname = settings["remoteControlHostname"]
            if not isinstance(hostname, str) or not hostname or len(hostname) > 255 or any(ord(c) < 32 for c in hostname):
                raise _invalid("Antigravity 原生主机显示名称不正确")
        elif source.name == "projects":
            _metadata(source, directory=True, private=False)
            for project in source.iterdir():
                _metadata(project, directory=False, private=False)
                if project.name != "default-cli-project.json" or not _equal_json(_json_object(project), {
                    "id": "default-cli-project", "name": "CLI Project", "projectResources": {},
                }):
                    raise _invalid("Antigravity 原生项目绑定包含额外工作区或配置")
        elif source.name == "mcp_config.json" and source.lstat().st_size:
            # Newer CLIs create this file; only an empty server map is accepted.
            _metadata(source, directory=False, private=False)
            if not (_equal_json(_json_object(source), {}) or _equal_json(_json_object(source), {"mcpServers": {}})):
                raise _invalid("Antigravity 全局 mcp_config.json 不能配置 MCP 服务器")
        else:
            # In particular, skills.json/plugins.json can inherit arbitrary
            # paths. Even disabled maps are not accepted as empty sources.
            _empty_source(source, private=False)


def _validate_named_cache(path: Path, names: tuple[str, ...], message: str) -> None:
    for expected in names:
        _metadata(path, directory=True, private=False)
        if {entry.name for entry in path.iterdir()} != {expected}:
            raise _invalid(message)
        path = path / expected
    # The Playwright version directory follows the CLI release; accept any
    # x.y.z name, but each must stay an empty placeholder.
    _metadata(path, directory=True, private=False)
    versions = list(path.iterdir())
    if not versions or len(versions) > _MAX_CACHE_VERSIONS or not all(
            _CACHE_VERSION.fullmatch(entry.name) for entry in versions):
        raise _invalid(message)
    for entry in versions:
        _empty_source(entry, private=False)


def _validate_library(path: Path) -> None:
    _validate_named_cache(
        path, ("Caches", "ms-playwright-go"),
        "Antigravity HOME Library 出现未知执行或配置来源",
    )


def _validate_linux_cache(path: Path) -> None:
    _validate_named_cache(
        path, ("ms-playwright-go",),
        "Antigravity HOME .cache 出现未知执行或配置来源",
    )


def _validate_log_alias(path: Path) -> None:
    if not path.is_symlink():
        _metadata(path, directory=False, private=False)
        return
    # CLI 1.2.0 rotates cli.log through this relative alias. Allow only the
    # observed shape, with a real log directory and regular owned target.
    target = path.readlink()
    if path.lstat().st_uid != os.getuid() or not re.fullmatch(r"log/cli-[0-9]{8}_[0-9]{6}\.log", str(target)):
        raise _invalid("Antigravity cli.log 指向未经允许的路径")
    _metadata(path.parent / "log", directory=True, private=False)
    _metadata(path.parent / target, directory=False, private=False)


def _validate_home(path: str | Path, *, legacy_strict: bool = False) -> Path:
    target = _path(path)
    try:
        pins = _load_pins()
        _metadata(target, directory=True)
        root_entries = {item.name for item in target.iterdir()}
        if ".gemini" not in root_entries or root_entries - _allowed_home_entries():
            raise _invalid("Antigravity 专用 HOME 出现未知配置来源")
        gemini = target / ".gemini"
        _metadata(gemini, directory=True)
        if {item.name for item in gemini.iterdir()} - {"antigravity-cli", "config"}:
            raise _invalid("Antigravity .gemini 目录出现额外配置或旧配置导入来源")
        cli = gemini / "antigravity-cli"
        _metadata(cli, directory=True)
        settings = cli / "settings.json"
        _metadata(settings, directory=False)
        _validate_settings(settings, legacy_strict=legacy_strict)
        for source in cli.iterdir():
            if source.name == "settings.json":
                continue
            if source.name == "builtin":
                _validate_builtin(source, pins)
            elif source.name == "bin":
                _validate_bin(source, pins)
            elif source.name == "implicit":
                _validate_uuid_state(source, ".pb")
            elif source.name == "annotations":
                _validate_uuid_state(source, ".pbtxt")
            elif source.name == "presence":
                _validate_uuid_state(source, ".lock", private_files=True)
            elif source.name == "scratch":
                _metadata(source, directory=True, private=False)
                _empty_source(source, private=False)
            elif source.name in _STATE_DIRS:
                _metadata(source, directory=True, private=False)
                _state_tree(source)
            elif source.name == "cli.log":
                _validate_log_alias(source)
            elif source.name in _STATE_FILES:
                _metadata(source, directory=False, private=source.name in _PRIVATE_STATE_FILES)
            elif source.name in _CREDENTIAL_FILES:
                _metadata(source, directory=False)
            elif source.name in _EXTENSION_DIRS or source.name in {"hooks.json", "mcp_config.json"}:
                _empty_source(source)
            else:
                raise _invalid(f"Antigravity CLI 目录出现未知配置来源：{source}")
        customizations = gemini / "config"
        if customizations.exists() or customizations.is_symlink():
            _validate_customizations(customizations)
        if "Library" in root_entries:
            _validate_library(target / "Library")
        if ".cache" in root_entries:
            _validate_linux_cache(target / ".cache")
    except (OSError, RuntimeError, ValueError) as exc:
        raise _invalid(f"无法验证 Antigravity 专用目录：{type(exc).__name__}") from exc
    return target


def validate_home(path: str | Path) -> Path:
    """验证当前配置和状态元数据，不读取原生凭据或 keyring。"""
    return _validate_home(path)


def _description(target: Path, *, created: bool | None = None) -> dict[str, object]:
    result: dict[str, object] = {
        "profile": PROFILE, "home": str(target),
        "settings": str(target / ".gemini" / "antigravity-cli" / "settings.json"),
        "isolation": "policy_only", "os_sandbox_verified": False,
        "authentication_verified": False, "keyring_reuse_verified": False,
    }
    if created is not None:
        result["created"] = created
    return result


def prepare_home(path: str | Path) -> dict[str, object]:
    """只初始化新/空目录；已有专用配置仅验证，不替换。"""
    target = _path(path)
    try:
        if target.exists():
            _metadata(target, directory=True)
            if any(target.iterdir()):
                return _description(validate_home(target), created=False)
        else:
            target.mkdir(parents=True, mode=0o700)
        gemini = target / ".gemini"
        gemini.mkdir(mode=0o700)
        cli = gemini / "antigravity-cli"
        cli.mkdir(mode=0o700)
        (gemini / "config").mkdir(mode=0o700)
        fd = os.open(cli / "settings.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(SETTINGS_TEXT)
    except OSError as exc:
        raise _invalid(f"无法初始化 Antigravity 专用目录：{type(exc).__name__}") from exc
    return _description(validate_home(target), created=True)


# agy reads customisations only from these directories (skills, workflows,
# plugins, hooks.json, rules). An empty real directory carries nothing.
_CONTEXT_DIRECTORIES = frozenset({".agents", ".agent", ".gemini"})


def _directory_has_any_entry(path: Path) -> bool:
    """True when scandir yields any name, including hidden names.

    Stops at the first entry. Listing errors propagate so the caller fails closed.
    """
    with os.scandir(path) as scan:
        for _entry in scan:
            return True
    return False


def _reject_context_marker(path: Path) -> None:
    """Reject agy customisation markers under a workspace or its ancestors.

    `.agents`, `.agent`, and `.gemini` are allowed only when lstat shows a real
    directory (S_ISDIR, not a symlink) with no entries. `AGENTS.md` and
    `GEMINI.md` are always rejected. Called on every validation; do not cache
    the result, because a later run can find the directory populated. TOCTOU
    remains between this listing and agy startup: entries created in that
    window are seen only on the next validation.
    """
    if path.name in _CONTEXT_DIRECTORIES:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode):
            reason = "symlink"
        elif not stat.S_ISDIR(info.st_mode):
            reason = "not a directory"
        elif _directory_has_any_entry(path):
            reason = "non-empty"
        else:
            # Empty real directory. Not cached; the next validation rechecks.
            # TOCTOU: contents can appear after this scan and before agy starts.
            return
        raise _invalid(f"Antigravity 受限工作区或祖先含扩展/上下文来源：{path} ({reason})")
    if path.exists() or path.is_symlink():
        raise _invalid(f"Antigravity 受限工作区或祖先含扩展/上下文来源：{path}")


def validate_workspace(path: str | Path) -> Path:
    try:
        if not isinstance(path, (str, Path)) or not Path(path).is_absolute():
            raise _invalid("Antigravity 工作区必须是绝对路径")
        source = Path(path)
        if source.is_symlink():
            raise _invalid("Antigravity 工作区不能是符号链接")
        target = source.resolve(strict=True)
        if not target.is_dir():
            raise _invalid("Antigravity 工作区必须是目录")
        for parent in (target, *target.parents):
            for name in (".agents", ".agent", ".gemini", "AGENTS.md", "GEMINI.md"):
                _reject_context_marker(parent / name)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _invalid(f"无法验证 Antigravity 工作区：{type(exc).__name__}") from exc
    return target


def build_environment(path: str | Path, base_env: Mapping[str, str] | None = None) -> dict[str, str]:
    target = validate_home(path)
    source = os.environ if base_env is None else base_env
    env = {key: value for key, value in source.items() if key in _KEEP_ENV}
    env["HOME"] = str(target)
    env.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    return env


def validate_workspace_binding(path: str | Path, workspace: str | Path) -> Path:
    """派发时核对已有原生启动信任与本次工作区；不会增加信任。"""
    target = validate_workspace(workspace)
    home = validate_home(path)
    try:
        settings = _json_object(home / ".gemini" / "antigravity-cli" / "settings.json")
        trusted = settings.get("trustedWorkspaces")
        if trusted is not None and Path(trusted[0]).resolve(strict=True) != target:
            raise _invalid("Antigravity 原生信任工作区与本次派发工作区不一致")
        read_root = _allowed_workspace(settings["permissions"])
        if read_root is not None and read_root != target:
            raise _invalid("Antigravity 读取权限工作区与本次派发工作区不一致")
    except (OSError, RuntimeError, ValueError) as exc:
        raise _invalid(f"无法验证 Antigravity 工作区绑定：{type(exc).__name__}") from exc
    return target


def bind_workspace(path: str | Path, workspace: str | Path) -> dict[str, object]:
    """显式绑定唯一读取目录；不增加原生信任、不改其它权限。"""
    home = validate_home(path)
    target = validate_workspace_binding(home, workspace)
    _metadata(target, directory=True, private=False)
    rule = _read_permission(target)
    settings_path = home / ".gemini" / "antigravity-cli" / "settings.json"
    temporary: Path | None = None
    try:
        original = settings_path.read_bytes()
        settings = _json_object(settings_path)
        permissions = settings["permissions"]
        if permissions.get("allow", []) == [rule]:
            return {**_description(home), "workspace": str(target), "changed": False}
        permissions["allow"] = [rule]
        fd, filename = tempfile.mkstemp(prefix=".asterun-settings-", dir=settings_path.parent)
        temporary = Path(filename)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        _metadata(settings_path, directory=False)
        if settings_path.read_bytes() != original:
            raise _invalid("Antigravity 配置在绑定期间变化；停止原生 CLI 后重试")
        os.replace(temporary, settings_path)
        temporary = None
    except (OSError, RuntimeError, ValueError) as exc:
        raise _invalid(f"无法绑定 Antigravity 读取工作区：{type(exc).__name__}") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as exc:
                raise _invalid(f"无法清理 Antigravity 配置暂存文件：{type(exc).__name__}") from exc
    validate_workspace_binding(home, target)
    return {**_description(home), "workspace": str(target), "changed": True}


def upgrade_permissions(path: str | Path, backup_path: str | Path) -> dict[str, object]:
    """显式迁移合格的旧 strict 配置；先独占备份，仅修改权限模式。"""
    home = _validate_home(path, legacy_strict=True)
    backup = _path(backup_path)
    settings_path = home / ".gemini" / "antigravity-cli" / "settings.json"
    temporary: Path | None = None
    try:
        if backup == home or home in backup.parents:
            raise _invalid("Antigravity 迁移备份必须位于专用 HOME 外")
        _metadata(backup.parent, directory=True)
        original = settings_path.read_bytes()
        settings = _parse_json_object(original)
        _validate_settings_object(settings, legacy_strict=True)
        roots = [_allowed_workspace(settings["permissions"])]
        roots.extend(Path(root).resolve(strict=True) for root in settings.get("trustedWorkspaces", []))
        if any(root is not None and (backup == root or root in backup.parents) for root in roots):
            raise _invalid("Antigravity 迁移备份不能放在已绑定的工作区内")
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(original)
            handle.flush()
            os.fsync(handle.fileno())
        settings["toolPermission"] = SETTINGS["toolPermission"]
        updated = (json.dumps(settings, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        fd, filename = tempfile.mkstemp(prefix=".asterun-settings-", dir=settings_path.parent)
        temporary = Path(filename)
        with os.fdopen(fd, "wb") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        _metadata(settings_path, directory=False)
        if settings_path.read_bytes() != original:
            raise _invalid("Antigravity 配置在迁移期间变化；原备份已保留，停止原生 CLI 后重试")
        os.replace(temporary, settings_path)
        temporary = None
    except (OSError, RuntimeError, ValueError) as exc:
        raise _invalid(f"无法迁移 Antigravity 权限模式：{type(exc).__name__}") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as exc:
                raise _invalid(f"无法清理 Antigravity 配置暂存文件：{type(exc).__name__}") from exc
    validate_home(home)
    return {
        **_description(home), "changed": True,
        "previous_permission_mode": "strict", "permission_mode": SETTINGS["toolPermission"],
        "backup": str(backup), "backup_sha256": hashlib.sha256(original).hexdigest(),
        "settings_sha256": hashlib.sha256(updated).hexdigest(),
        "rollback": "停止使用此 HOME 的原生 CLI 后，将备份原子恢复到 settings 路径，保持 0600 权限",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="准备、检查或显式迁移 Antigravity 受限配置；不自动登录")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "inspect"):
        command = sub.add_parser(name)
        command.add_argument("--home", required=True)
    binding = sub.add_parser("bind-workspace")
    binding.add_argument("--home", required=True)
    binding.add_argument("--workspace", required=True)
    upgrade = sub.add_parser("upgrade-permissions", help="将旧 strict 配置迁移为 request-review；须先停止原生 CLI")
    upgrade.add_argument("--home", required=True)
    upgrade.add_argument("--backup", required=True, help="HOME 外私有目录中的新备份文件；不覆盖已有文件")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare_home(args.home)
        elif args.command == "bind-workspace":
            result = bind_workspace(args.home, args.workspace)
        elif args.command == "upgrade-permissions":
            result = upgrade_permissions(args.home, args.backup)
        else:
            result = _description(validate_home(args.home))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except AsterunError as exc:
        print(json.dumps({"error": exc.to_dict()}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
