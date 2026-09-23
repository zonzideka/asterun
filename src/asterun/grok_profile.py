"""显式创建和检查 Grok 专用环境；导入不接触用户目录或凭据。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import tomllib
from collections.abc import Mapping

from .errors import AsterunError, INVALID_CONFIG

PROFILE = "text-only-v1"
CODE_PROFILE = "workspace-code-v1"
CODE_SANDBOX = "asterun-workspace-code-v1"
CODE_SANDBOX_TEXT = f'''# Asterun {CODE_PROFILE}; an explicitly named custom profile fails closed.
[profiles.{CODE_SANDBOX}]
extends = "workspace"
'''
_CODE_SANDBOX = tomllib.loads(CODE_SANDBOX_TEXT)
CONFIG_TEXT = '''# Asterun text-only-v1; credentials remain in the native CLI.
[auth]
preferred_method = "oidc"
disable_api_key_auth = true
[cli]
auto_update = false
installer = "internal"
[features]
managed_config = false
campaigns = false
codebase_indexing = false
[session]
load_envrc = false
[managed_mcps]
enabled = false
gateway_tools_enabled = false
[ui]
max_thoughts_width = 120
fork_secondary_model = "grok-4.6"
yolo = false
compact_mode = false
''' + "".join(
    f"\n[compat.{vendor}]\n" + "".join(f"{area} = false\n" for area in
    ("skills", "rules", "agents", "mcps", "hooks", "sessions"))
    for vendor in ("claude", "cursor", "codex")
)
_CONFIG = tomllib.loads(CONFIG_TEXT)
_KEEP_ENV = frozenset((
    "HOME", "USER", "LOGNAME", "PATH", "TMPDIR", "LANG", "LC_ALL",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy", "SSL_CERT_FILE",
))
_SYSTEM_CONFIG = Path("/etc/grok")


def _invalid(message: str) -> AsterunError:
    return AsterunError(INVALID_CONFIG, message,
        next_action="使用 asterun-grok prepare --home 创建独立目录；不覆盖已有原生配置")


def _path(path: str | Path) -> Path:
    if not isinstance(path, (str, Path)) or not str(path) or not Path(path).is_absolute():
        raise _invalid("Grok home 必须是显式绝对路径")
    target = Path(path)
    if target.is_symlink():
        raise _invalid("Grok home 不能是符号链接")
    # Canonicalize OS aliases such as macOS /tmp before handing the path to Grok.
    return target.resolve()


def _owned_private(path: Path, *, directory: bool = False) -> None:
    info = path.lstat()
    valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not valid_type or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise _invalid(f"Grok 私有路径类型、所有者或权限不正确：{path}")


def _empty_source(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    info = path.lstat()
    if stat.S_ISREG(info.st_mode) and info.st_size == 0:
        return  # Grok creates empty native placeholders on first launch.
    if stat.S_ISDIR(info.st_mode) and not any(path.iterdir()):
        return
    raise _invalid(f"受限 Grok 环境发现额外配置或扩展来源：{path}")


def _execution_profile(value: str) -> str:
    if value not in (PROFILE, CODE_PROFILE):
        raise _invalid(f"不支持的 Grok execution_profile：{value}")
    return value


def _validate_sandbox(target: Path, *, required: bool) -> None:
    sandbox = target / "sandbox.toml"
    if not required:
        try:
            _empty_source(sandbox)
            return
        except AsterunError:
            pass
    _owned_private(sandbox)
    if tomllib.loads(sandbox.read_text(encoding="utf-8")) != _CODE_SANDBOX:
        raise _invalid("Grok sandbox.toml 不是 Asterun workspace-code-v1 配置；拒绝启动")


def validate_home(path: str | Path, *, execution_profile: str = PROFILE) -> Path:
    """只检查原生 auth 文件元数据，不读取或输出其中内容。"""
    profile = _execution_profile(execution_profile)
    target = _path(path)
    try:
        _owned_private(target, directory=True)
        config = target / "config.toml"
        _owned_private(config)
        if tomllib.loads(config.read_text(encoding="utf-8")) != _CONFIG:
            raise _invalid("Grok 配置已偏离 text-only-v1；拒绝启动")
        _validate_sandbox(target, required=profile == CODE_PROFILE)
        for name in ("managed_config.toml", "requirements.toml",
                     "trusted_folders.toml", "hooks-paths", "hooks", "plugins",
                     "skills", "agents", "rules", "mcp.json"):
            _empty_source(target / name)
        for name in ("managed_config.toml", "requirements.toml"):
            _empty_source(_SYSTEM_CONFIG / name)
        auth = target / "auth.json"
        if auth.exists() or auth.is_symlink():
            _owned_private(auth)
    except (OSError, ValueError) as exc:
        raise _invalid(f"无法验证 Grok 专用目录：{type(exc).__name__}") from exc
    return target


def _prepare_code_sandbox(target: Path) -> None:
    sandbox = target / "sandbox.toml"
    # Native Grok creates an empty sandbox.toml placeholder. Only that exact
    # regular, private, current-user-owned placeholder may be filled in place.
    # O_NOFOLLOW and fstat validate the file actually opened, without truncating
    # or replacing another native file, even when discovery raced with open.
    flags = os.O_WRONLY | os.O_NOFOLLOW
    try:
        fd = os.open(sandbox, flags | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        fd = os.open(sandbox, flags)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise _invalid(f"Grok sandbox.toml 类型、所有者或权限不正确：{sandbox}")
        if info.st_size:
            return  # The caller validates the complete existing profile.
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(CODE_SANDBOX_TEXT)
    finally:
        if fd != -1:
            os.close(fd)


def prepare_home(path: str | Path, *, execution_profile: str = PROFILE) -> dict[str, object]:
    """初始化新目录，或显式为合法文本目录添加编码沙箱；不覆盖原生文件。"""
    profile = _execution_profile(execution_profile)
    target = _path(path)
    created = False
    try:
        if target.exists():
            _owned_private(target, directory=True)
            if any(target.iterdir()):
                # The shared native configuration remains the text profile's
                # exact configuration. The coding capability is selected by the
                # adapter and its separately validated custom sandbox.
                validate_home(target)
        else:
            target.mkdir(parents=True, mode=0o700)
        if not any(target.iterdir()):
            fd = os.open(target / "config.toml", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(CONFIG_TEXT)
            created = True
        if profile == CODE_PROFILE:
            _prepare_code_sandbox(target)
    except OSError as exc:
        raise _invalid(f"无法初始化 Grok 专用目录：{type(exc).__name__}") from exc
    validate_home(target, execution_profile=profile)
    return {"profile": profile, "home": str(target), "created": created}


def _git_workspace_root(target: Path) -> Path | None:
    # A .git name alone is not proof of a Git root. Ask Git without inherited
    # GIT_* overrides or global/system configuration and without hooks/fsmonitor.
    # Invalid markers, unavailable Git and unusual layouts retain the broader
    # ancestor inspection instead of weakening project-source validation.
    env = {key: value for key, value in os.environ.items()
           if key in {"PATH", "LANG", "LC_ALL", "SYSTEMROOT"}}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS="0")
    try:
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}",
             "-C", str(target), "rev-parse", "--show-toplevel"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=5, check=False,
        )
        if result.returncode:
            return None
        root = Path(result.stdout.rstrip("\r\n"))
        if not root.is_absolute():
            return None
        root = root.resolve(strict=True)
        return root if root.is_dir() and target.is_relative_to(root) else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def validate_workspace(path: str | Path, *, execution_profile: str = PROFILE) -> Path:
    profile = _execution_profile(execution_profile)
    target = Path(path).resolve(strict=True)
    ancestors = (target, *target.parents)
    if profile == CODE_PROFILE:
        root = _git_workspace_root(target)
        if root in ancestors:
            ancestors = ancestors[:ancestors.index(root) + 1]
    for parent in ancestors:
        source = parent / ".grok"
        if not source.exists() and not source.is_symlink():
            continue
        # The official installer uses ~/.grok/bin and downloads; those are not
        # project configuration sources and need not prevent a fresh install.
        if source.is_symlink() or not source.is_dir() or any(
            child.name not in {"bin", "downloads"} for child in source.iterdir()
        ):
            raise _invalid(f"Grok 工作区祖先含项目配置来源：{source}")
    return target


def build_environment(
    path: str | Path, base_env: Mapping[str, str] | None = None, *, execution_profile: str = PROFILE,
) -> dict[str, str]:
    target = validate_home(path, execution_profile=execution_profile)
    source = os.environ if base_env is None else base_env
    env = {key: value for key, value in source.items() if key in _KEEP_ENV}
    env.update(HOME=str(target), GROK_HOME=str(target), GROK_DISABLE_API_KEY_AUTH="true",
        GROK_MANAGED_MCPS_ENABLED="false", GROK_MANAGED_MCP_GATEWAY_TOOLS_ENABLED="false",
        GROK_DISABLE_AUTOUPDATER="1", GROK_CAMPAIGNS="0")
    for vendor in ("CLAUDE", "CURSOR", "CODEX"):
        for area in ("SKILLS", "RULES", "AGENTS", "MCPS", "HOOKS", "SESSIONS"):
            env[f"GROK_{vendor}_{area}_ENABLED"] = "false"
    return env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="准备并检查 Grok 专用执行环境")
    sub = parser.add_subparsers(dest="command", required=True)
    sync = sub.add_parser("sync-sessions", help="将原生会话日志单向同步给本地用量工具")
    sync.add_argument("--home", required=True)
    sync.add_argument("--destination-home", required=True)
    sync.add_argument("--session-id")
    for name in ("prepare", "inspect", "login"):
        command = sub.add_parser(name)
        command.add_argument("--home", required=True)
        command.add_argument("--execution-profile", choices=(PROFILE, CODE_PROFILE), default=PROFILE)
        if name == "login":
            command.add_argument("--bin", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "sync-sessions":
            from asterun.grok_sync import sync_sessions
            result = sync_sessions(args.home, args.destination_home, session_id=args.session_id)
            print(json.dumps(result, ensure_ascii=False))
            return 1 if result["conflicts"] or result["skipped"] else 0
        elif args.command == "prepare":
            result = prepare_home(args.home, execution_profile=args.execution_profile)
        else:
            target = validate_home(args.home, execution_profile=args.execution_profile)
            if args.command == "login":
                binary = Path(args.bin)
                if not binary.is_absolute() or not binary.is_file():
                    raise _invalid("Grok 二进制必须是已安装文件的绝对路径")
                # Native device authorization owns all credential I/O.
                return subprocess.call([str(binary), "login", "--device-auth"],
                    cwd=target, env=build_environment(target, execution_profile=args.execution_profile))
            result = {"profile": args.execution_profile, "home": str(target),
                "credential_file_present": (target / "auth.json").exists(),
                "authentication_verified": False}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except AsterunError as exc:
        print(json.dumps({"error": exc.to_dict()}, ensure_ascii=False))
        return 1
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": {"code": "GROK_PROFILE_IO_ERROR", "type": type(exc).__name__}}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
