"""子进程环境白名单。配置只能追加变量名，值仍从调用进程的环境复制。"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence

from asterun.errors import INVALID_CONFIG, AsterunError

# 登录、区域设置和 TLS 所需的最小集合。密钥、云凭据和宿主配置入口不在其中。
BASE_CHILD_ENV = frozenset({
    "HOME", "USER", "LOGNAME", "PATH", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
})
CODEX_AUTH_ENV = frozenset({"CODEX_HOME"})
CLAUDE_AUTH_ENV = frozenset({"CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR"})
# 即使配置点名，也不允许改写加载器或解释器入口。
DENIED_CHILD_ENV = frozenset({
    "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT",
    "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH",
    "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONINSPECT",
    "NODE_OPTIONS", "NODE_PATH", "BASH_ENV", "ENV", "GIT_SSH_COMMAND",
    "SHELLOPTS", "PS4",
})
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")


def validate_extra_env(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 32:
        raise AsterunError(INVALID_CONFIG, f"{where} 必须为最多 32 个环境变量名")
    names: list[str] = []
    for item in value:
        if not isinstance(item, str) or _NAME.fullmatch(item) is None:
            raise AsterunError(INVALID_CONFIG, f"{where} 只接受环境变量名，不接受赋值或秘密值")
        if item in DENIED_CHILD_ENV or item.startswith(("LD_", "DYLD_")):
            raise AsterunError(INVALID_CONFIG, f"{where} 不能追加会改变进程加载的变量：{item}")
        if item in names:
            raise AsterunError(INVALID_CONFIG, f"{where} 不能重复：{item}")
        names.append(item)
    return tuple(names)


def build_child_env(
    base_env: Mapping[str, str] | None = None,
    *,
    keep: frozenset[str] = frozenset(),
    extra: Sequence[str] = (),
) -> dict[str, str]:
    source = os.environ if base_env is None else base_env
    allowed = (set(BASE_CHILD_ENV) | set(keep) | set(extra)) - DENIED_CHILD_ENV
    env: dict[str, str] = {}
    for key, value in source.items():
        if key in allowed and isinstance(key, str) and isinstance(value, str) and "\x00" not in key + value:
            env[key] = value
    return env
