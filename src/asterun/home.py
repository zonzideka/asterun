from __future__ import annotations

import os
from pathlib import Path


def asterun_home(explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit
    env = os.environ.get("ASTERUN_HOME")
    if env:
        return Path(env)
    return Path.home() / ".config" / "asterun"


def default_config_path(explicit_home: Path | None = None) -> Path | None:
    env = os.environ.get("ASTERUN_CONFIG")
    if env:
        return Path(env)
    candidate = asterun_home(explicit_home) / "config.json"
    if candidate.exists():
        return candidate
    return None


def default_state_dir(explicit_home: Path | None = None) -> Path:
    return asterun_home(explicit_home) / "state"
