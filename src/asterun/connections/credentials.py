"""获授权派发的最后时刻解析凭据引用；不发现账户、不登录、不保存秘密。"""

from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path
import re
import stat

from asterun.config_v2 import credential_reference
from asterun.errors import AsterunError, AUTH_REQUIRED, CAPABILITY_UNSUPPORTED, INVALID_CONFIG, SCOPE_DENIED
from asterun.plugin_api import PluginManifest

MAX_SECRET_BYTES = 64 * 1024
_SECRET_MODES = {"api_key", "user_api_key", "oauth"}
_SECRET_CLASSES = {
    "api_key": {"api_key", "user_api_key", "provider_api_key"},
    "user_api_key": {"api_key", "user_api_key", "provider_api_key"},
    "oauth": {"oauth_token", "oauth_access_token"},
}
_FORBIDDEN_ENV = {
    "HOME", "PATH", "TMPDIR", "TMP", "TEMP", "USER", "LOGNAME", "SHELL", "ENV", "BASH_ENV",
    "SHELLOPTS", "CDPATH", "IFS", "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE",
    "NODE_OPTIONS", "NODE_PATH", "RUBYOPT", "PERL5OPT", "JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS",
    "SSH_AUTH_SOCK", "SSH_AGENT_PID", "DOCKER_HOST", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "GOOGLE_APPLICATION_CREDENTIALS", "CODEX_HOME", "ASTERUN_HOME", "ASTERUN_CONFIG",
}
_EXISTING_OPERATIONS = {"execution.observe", "execution.reconcile", "execution.cancel"}


class ResolvedCredentials:
    """只在内存中供 worker 启动使用；不能被当作持久化结果序列化。"""

    __slots__ = ("_env", "_redactions", "native_home")

    def __init__(self, env: dict[str, str] | None = None, redactions: tuple[str, ...] = (),
                 native_home: Path | None = None):
        self._env = dict(env or {})
        self._redactions = tuple(redactions)
        self.native_home = native_home

    @property
    def env(self) -> dict[str, str]:
        return dict(self._env)

    @property
    def redactions(self) -> tuple[str, ...]:
        return self._redactions

    def clear(self) -> None:
        # 清除本对象持有的引用；不声称 Python 字符串具有安全擦除能力。
        self._env.clear()
        self._redactions = ()
        self.native_home = None

    def __repr__(self) -> str:
        return f"ResolvedCredentials(secret_count={len(self._env)}, native_home_configured={self.native_home is not None})"

    def __reduce_ex__(self, protocol):
        raise TypeError("ResolvedCredentials 不允许序列化")

    def __getstate__(self):
        raise TypeError("ResolvedCredentials 不允许序列化")


def _deny(message: str, code: str = AUTH_REQUIRED) -> None:
    raise AsterunError(code, message) from None


def _open_private_path(path: Path, *, directory: bool) -> int:
    """逐层 openat+NOFOLLOW，避免叶和父目录符号链接及检查后替换。"""
    if not path.is_absolute() or ".." in path.parts:
        _deny("凭据路径必须为显式绝对路径，且不能含上级跳转", INVALID_CONFIG)
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY") or not hasattr(os, "geteuid"):
        _deny("此平台尚未实现安全凭据文件读取", CAPABILITY_UNSUPPORTED)
    current = None
    try:
        current = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in path.parts[1:-1]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = following
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        if directory:
            flags |= os.O_DIRECTORY
        result = os.open(path.name or ".", flags, dir_fd=current)
        try:
            metadata = os.fstat(result)
            required_type = stat.S_ISDIR if directory else stat.S_ISREG
            allowed_mode = {0o700, 0o500} if directory else {0o600, 0o400}
            if not required_type(metadata.st_mode) or metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) not in allowed_mode:
                _deny("凭据路径必须由当前用户所有且只允许该用户读写")
            return result
        except BaseException:
            os.close(result)
            raise
    except OSError:
        _deny("无法安全读取凭据路径；要求现存私有路径且不含符号链接")
    finally:
        if current is not None:
            os.close(current)


def _fingerprint(metadata):
    return (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mode, metadata.st_uid,
            metadata.st_mtime_ns, metadata.st_ctime_ns)


def _secret_text(value: object, *, from_file: bool = False) -> str:
    if not isinstance(value, str):
        _deny("显式凭据引用不存在或不是字符串")
    if from_file:
        # 常见私有 token 文件有一个结尾换行；不接受多行配置或整个 token JSON 文件。
        if value.endswith("\r\n"):
            value = value[:-2]
        elif value.endswith("\n"):
            value = value[:-1]
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        _deny("凭据内容必须是有效 UTF-8 单行文本")
    if not value or size > MAX_SECRET_BYTES or value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        _deny("凭据内容必须是有界非空单行文本")
    return value


def _file_secret(path: Path) -> str:
    descriptor = _open_private_path(path, directory=False)
    try:
        before = os.fstat(descriptor)
        if before.st_size > MAX_SECRET_BYTES + 2:
            _deny("凭据文件超过允许大小")
        data = bytearray()
        while len(data) <= MAX_SECRET_BYTES + 2:
            chunk = os.read(descriptor, min(8192, MAX_SECRET_BYTES + 3 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(descriptor)
        if _fingerprint(before) != _fingerprint(after) or len(data) != before.st_size:
            _deny("读取期间凭据文件已改变；未使用不一致内容")
        try:
            value = data.decode("utf-8")
        except UnicodeError:
            _deny("凭据文件必须是 UTF-8 单行文本")
        return _secret_text(value, from_file=True)
    except OSError:
        _deny("凭据文件读取失败")
    finally:
        os.close(descriptor)


def _secret_target(manifest: dict, mode: str) -> str:
    declaration = manifest.get("extensions", {}).get("asterun.credentials")
    if not isinstance(declaration, dict) or set(declaration) != {"credential_class", "env_var"}:
        _deny("插件缺少明确的凭据类别和环境目标声明", INVALID_CONFIG)
    declared_class = declaration["credential_class"]
    if (not isinstance(declared_class, str) or declared_class not in manifest["permissions"]["credential_classes"]
            or declared_class not in _SECRET_CLASSES[mode]):
        _deny("插件环境目标没有匹配其已声明凭据类别", INVALID_CONFIG)
    target = declaration["env_var"]
    if (not isinstance(target, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", target)
            or target in _FORBIDDEN_ENV or target.startswith(("LD_", "DYLD_", "PYTHON", "ASTERUN_", "GIT_", "XDG_", "NPM_CONFIG_", "BASH_"))):
        _deny("插件凭据不能注入宿主控制环境变量", INVALID_CONFIG)
    return target


def resolve_credentials(connection: dict, provider_account: dict | None, manifest: PluginManifest | dict, *,
                        authorized: bool, environment: Mapping[str, str] | None = None,
                        operation: str = "execution.start") -> ResolvedCredentials:
    """仅核心已授权路径调用；authorized 不能取自请求 input 或模型输出。"""
    if authorized is not True:
        _deny("解析凭据前需要核心对该执行连接的授权", SCOPE_DENIED)
    if not isinstance(operation, str) or operation not in _EXISTING_OPERATIONS | {"execution.start"}:
        _deny("此操作不得解析执行凭据", SCOPE_DENIED)
    if not isinstance(connection, dict) or type(connection.get("enabled")) is not bool:
        _deny("连接启用状态无效", INVALID_CONFIG)
    if connection["enabled"] is not True and operation not in _EXISTING_OPERATIONS:
        _deny("连接未启用，不能解析凭据", SCOPE_DENIED)
    if provider_account is not None and not isinstance(provider_account, dict):
        _deny("供应商账户引用无效", INVALID_CONFIG)
    if provider_account and provider_account.get("revoked", False) is not False:
        _deny("供应商账户凭据已撤销", SCOPE_DENIED)
    if connection.get("provider_account_ref") is not None and provider_account is None:
        _deny("连接缺少已绑定的供应商账户")
    if isinstance(manifest, PluginManifest):
        manifest = manifest.to_dict()
    else:
        manifest = PluginManifest.from_dict(manifest).to_dict()
    if connection.get("plugin_id") != manifest["plugin_id"]:
        _deny("连接与凭据插件声明不一致", INVALID_CONFIG)
    mode = connection.get("auth_mode")
    if mode not in manifest["auth_modes"]:
        _deny("连接认证模式不在插件声明中", INVALID_CONFIG)
    if mode != "none":
        account_ref = connection.get("provider_account_ref")
        if not isinstance(account_ref, str) or not account_ref or provider_account is None:
            _deny("认证连接需要显式绑定供应商账户引用")
        if provider_account.get("ref", account_ref) != account_ref:
            _deny("供应商账户与连接引用不一致", SCOPE_DENIED)
    reference = connection.get("credential_ref", (provider_account or {}).get("credential_ref"))
    credential_reference(reference, "credential_ref")
    if mode == "none":
        if reference is not None:
            _deny("无认证模式不接受凭据注入", INVALID_CONFIG)
        return ResolvedCredentials()
    if reference is None:
        _deny("该认证模式缺少显式凭据引用")
    if reference.startswith("credential-ref:"):
        _deny("尚未配置此不透明凭据引用的解析器", CAPABILITY_UNSUPPORTED)
    if mode == "native_account":
        if not reference.startswith("native-home:"):
            _deny("原生账户模式只接受显式 native-home，禁止切换到 API key", INVALID_CONFIG)
        if "native_cli_account_session" not in manifest["permissions"]["credential_classes"]:
            _deny("插件未声明原生账户会话凭据类别", INVALID_CONFIG)
        home = Path(reference[len("native-home:"):])
        descriptor = _open_private_path(home, directory=True)
        os.close(descriptor)
        return ResolvedCredentials(native_home=home)
    if mode not in _SECRET_MODES:
        _deny("此认证模式没有已实现的安全凭据解析器", CAPABILITY_UNSUPPORTED)
    if reference.startswith("native-home:"):
        _deny("API/OAuth 模式不能借用原生账户 HOME", INVALID_CONFIG)
    target = _secret_target(manifest, mode)
    if reference.startswith("env:"):
        if environment is None or not isinstance(environment, Mapping):
            _deny("未提供获授权的显式环境变量来源")
        secret = _secret_text(environment.get(reference[4:]))
    elif reference.startswith("file:"):
        secret = _file_secret(Path(reference[len("file:"):]))
    else:
        _deny("此凭据引用没有已实现的解析器", CAPABILITY_UNSUPPORTED)
    return ResolvedCredentials(env={target: secret}, redactions=(secret,))
