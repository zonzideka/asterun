"""诊断与导出用的脱敏。不把凭据、cookie 或完整账户标识写进默认输出。"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

SECRET_ENV_NAMES = {
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GROK_API_KEY",
    "CODEX_API_KEY",
    "ASTERUN_ACCOUNT_REF",
    "ASTERUN_RUNTIME_REF",
    "CODEX_HOME",
    "GROK_MODELS_CACHE",
}

_TOKEN_RE = re.compile(r"(sk-|gsk_|xox[baprs]-|ghp_|github_pat_)[A-Za-z0-9_\-]{8,}")


def secret_values() -> list[str]:
    values: list[str] = []
    for name in SECRET_ENV_NAMES:
        raw = os.environ.get(name)
        if raw:
            values.append(raw)
    home = str(Path.home())
    if home and home not in {"/", ""}:
        values.append(home)
    asterun_home = os.environ.get("ASTERUN_HOME")
    if asterun_home:
        values.append(asterun_home)
    return values


def redact_text(text: str, extras: list[str] | None = None) -> str:
    result = text
    for item in list(extras or []) + secret_values():
        if item and item not in {"/", ".", "~"}:
            result = result.replace(item, "[redacted]")
    result = _TOKEN_RE.sub("[redacted]", result)
    return result


def redact_ref(value: str | None) -> str | None:
    if value is None:
        return None
    if ":" in value:
        kind, _, _rest = value.partition(":")
        return f"{kind}:[redacted]"
    return "[redacted]"


def redact_path(path: Path | str | None) -> str | None:
    if path is None:
        return None
    return redact_text(str(path))


def present_secret_env() -> dict[str, str]:
    return {name: "set" if os.environ.get(name) else "unset" for name in sorted(SECRET_ENV_NAMES)}


def redact_mapping(data: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in data.items():
        lowered = key.lower()
        if any(token in lowered for token in ("token", "secret", "password", "cookie", "authorization", "api_key")):
            cleaned[key] = "[redacted]"
            continue
        if isinstance(value, dict):
            cleaned[key] = redact_mapping(value)
        elif isinstance(value, list):
            cleaned[key] = [redact_mapping(item) if isinstance(item, dict) else redact_text(str(item)) for item in value]
        elif isinstance(value, str):
            cleaned[key] = redact_text(value)
        else:
            cleaned[key] = value
    return cleaned
