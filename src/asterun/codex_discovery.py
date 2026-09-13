"""安装层的 Codex 候选发现；不会把应用路径注入通用后端默认值。"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Iterable


def discover_codex_candidates(*, explicit: str | None = None,
                              application_dirs: Iterable[Path] | None = None,
                              probe_version: bool = False) -> dict:
    """默认只检查文件；显式 probe 才执行候选的 --version（不调用模型）。"""
    directories = ([Path("/Applications"), Path.home() / "Applications"]
                   if application_dirs is None else [Path(p) for p in application_dirs])
    candidates: list[dict] = []
    seen: set[str] = set()

    def add(path: Path, source: str) -> None:
        absolute = path.expanduser().absolute()
        key = str(absolute)
        if key in seen:
            return
        seen.add(key)
        executable = absolute.is_file() and os.access(absolute, os.X_OK)
        row = {"path": key, "source": source, "executable": executable,
               "version": None, "version_probe": "not_requested"}
        if probe_version and executable:
            # 不继承令牌；只输出明确识别的版本，原始 stdout/stderr 永不返回。
            try:
                result = subprocess.run([key, "--version"], capture_output=True,
                                        timeout=5, check=False,
                                        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
                match = re.fullmatch(rb"(?:codex(?:-cli)?\s+)?(\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?)(?:\s+\([A-Fa-f0-9]{7,40}\))?\s*",
                                     result.stdout[:256]) if result.returncode == 0 else None
                row["version"] = match[1].decode("ascii") if match else None
                row["version_probe"] = "verified" if match else "unrecognized"
            except (OSError, subprocess.SubprocessError):
                row["version_probe"] = "failed"
        candidates.append(row)

    if explicit is not None:
        add(Path(explicit), "explicit")
    configured = os.environ.get("CODEX_BIN")
    if configured is not None:
        add(Path(configured), "CODEX_BIN")
    located = shutil.which("codex")
    if located:
        add(Path(located), "PATH")
    for directory in directories:
        # 有界扫描应用目录直接子项，不扫描账户/配置/会话。
        bundles = {directory / "ChatGPT.app", directory / "Codex.app"}
        if directory.is_dir():
            bundles.update(directory.glob("*.app"))
        for bundle in sorted(bundles):
            executable = bundle / "Contents" / "Resources" / "codex"
            if executable.exists():
                add(executable, "application_bundle")
    chosen_source = "explicit" if explicit is not None else "CODEX_BIN" if configured is not None else None
    eligible = [row for row in candidates if row["executable"]
                and (chosen_source is None or row["source"] == chosen_source)]
    return {"candidates": candidates, "selected": eligible[0]["path"] if eligible else None,
            "selection_requires_configuration": True, "model_called": False,
            "message": "选定候选后将绝对路径写入实例配置；显式无效选择不会自动回退。"}
