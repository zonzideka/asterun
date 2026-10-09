#!/usr/bin/env python3
"""从当前检出构建与手工预发布相同形状的发行资产。"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import zipfile

PLUGINS = ("antigravity", "cursor", "jules")
SMOKE_MODULES = (
    ("asterun-plugin-antigravity", "asterun_plugin_antigravity.worker", "asterun-antigravity-plugin"),
    ("asterun-plugin-cursor", "asterun_plugin_cursor.worker", "asterun-cursor-plugin"),
    ("asterun-plugin-jules", "asterun_plugin_jules.worker", "asterun-jules-plugin"),
)


def project_version(path: Path) -> str:
    match = re.search(r'(?m)^version = "([^"]+)"$', path.read_text(encoding="utf-8"))
    if not match:
        raise ValueError(f"无法读取版本：{path}")
    return match.group(1)


def expected_asset_names(root: Path) -> list[str]:
    core = project_version(root / "pyproject.toml")
    names = [
        f"asterun-{core}-py3-none-any.whl",
        f"asterun-{core}.tar.gz",
        f"asterun-skill-{core}.zip",
    ]
    for plugin in PLUGINS:
        version = project_version(root / "packages" / f"asterun-plugin-{plugin}" / "pyproject.toml")
        dist = f"asterun_plugin_{plugin}"
        names.append(f"{dist}-{version}-py3-none-any.whl")
        names.append(f"{dist}-{version}.tar.gz")
    return sorted(names)


def write_checksums(directory: Path) -> Path:
    files = sorted(path for path in directory.iterdir() if path.is_file() and path.name != "SHA256SUMS")
    lines = [f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}" for path in files]
    target = directory / "SHA256SUMS"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def _checker():
    path = Path(__file__).resolve().parent / "check-release-package.py"
    spec = importlib.util.spec_from_file_location("check_release_package", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载发行检查脚本")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source_date(root: Path) -> str:
    current = os.environ.get("SOURCE_DATE_EPOCH")
    if current:
        return current
    return subprocess.run(
        ["git", "log", "-1", "--format=%ct"], cwd=root, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _build_project(source: Path, output: Path) -> None:
    import warnings
    from setuptools.build_meta import build_sdist, build_wheel

    previous = Path.cwd()
    os.chdir(source)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            build_sdist(str(output))
            build_wheel(str(output))
    finally:
        os.chdir(previous)


def _check_skill(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        if "asterun/SKILL.md" not in names or "asterun/manifest.json" not in names:
            return [f"{path.name} 缺少 skill 成员"]
        manifest = json.loads(archive.read("asterun/manifest.json"))
    if not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("source_commit", ""))):
        return [f"{path.name} 的 source_commit 无效"]
    return []


def _smoke(output: Path, core_version: str) -> None:
    wheels = sorted(output.glob("*.whl"))
    core = [path for path in wheels if path.name.startswith("asterun-")]
    plugins = [path for path in wheels if path.name.startswith("asterun_plugin_")]
    if len(core) != 1 or len(plugins) != len(PLUGINS):
        raise RuntimeError("冒烟安装需要 1 个核心 wheel 和每个外部插件的 wheel")
    with tempfile.TemporaryDirectory(prefix="asterun-release-smoke-") as raw:
        venv = Path(raw) / "venv"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        pip = venv / "bin" / "pip"
        python = venv / "bin" / "python"
        subprocess.run([str(pip), "install", "--disable-pip-version-check", str(core[0])], check=True)
        subprocess.run([str(pip), "install", "--disable-pip-version-check", "--no-deps", *map(str, plugins)], check=True)
        probe = r"""
import importlib, importlib.metadata as metadata, json, sys
expected = sys.argv[1]
import asterun
assert asterun.__version__ == expected, asterun.__version__
scripts = {item.name for item in metadata.distribution("asterun").entry_points if item.group == "console_scripts"}
assert "asterun" in scripts
providers = {item.name for item in metadata.entry_points(group="asterun.providers.v1")}
assert "antigravity" in providers
for project, module, script in json.loads(sys.argv[2]):
    dist = metadata.distribution(project)
    manifests = [dist.locate_file(path) for path in dist.files if path.name == "manifest.json"]
    assert len(manifests) == 1, project
    body = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert body["schema_version"] == "asterun-plugin/v1", project
    assert body["plugin_api_major"] == 1, project
    assert isinstance(body["plugin_version"], str) and body["plugin_version"], project
    importlib.import_module(module)
    names = {item.name for item in dist.entry_points if item.group == "console_scripts"}
    assert script in names, project
print("smoke-ok")
"""
        subprocess.run(
            [str(python), "-c", probe, core_version, json.dumps(SMOKE_MODULES)],
            check=True,
        )
        completed = subprocess.run([str(venv / "bin" / "asterun"), "version"], check=True, capture_output=True, text=True)
        payload = json.loads(completed.stdout)
        if payload.get("data", {}).get("version") != core_version or payload.get("ok") is not True:
            raise RuntimeError("安装后的 asterun version 与核心版本不一致")


def build_release_assets(root: Path, output: Path, *, smoke: bool = True) -> dict[str, object]:
    root = root.resolve()
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise RuntimeError("发行输出目录必须为空")
    epoch = _source_date(root)
    previous_epoch = os.environ.get("SOURCE_DATE_EPOCH")
    os.environ["SOURCE_DATE_EPOCH"] = epoch
    problems: list[str] = []
    try:
        _build_project(root, output)
        for plugin in PLUGINS:
            _build_project(root / "packages" / f"asterun-plugin-{plugin}", output)
        core = project_version(root / "pyproject.toml")
        subprocess.run(
            [sys.executable, str(root / "scripts/package-standard-skill.py"),
             "--output", str(output / f"asterun-skill-{core}.zip")],
            cwd=root, check=True,
        )
        expected = expected_asset_names(root)
        missing = [name for name in expected if not (output / name).is_file()]
        extra = sorted(
            path.name for path in output.iterdir()
            if path.is_file() and path.name not in expected and path.name != "SHA256SUMS"
        )
        if missing or extra:
            problems.append(f"资产名单不一致，缺少 {missing}，多出 {extra}")
        checker = _checker()
        for name in expected:
            path = output / name
            if not path.is_file():
                continue
            if name.endswith(".zip"):
                problems.extend(_check_skill(path))
                continue
            problems.extend(checker.inspect_archive(path))
            if name.startswith("asterun-") and not name.startswith("asterun-skill-"):
                problems.extend(checker.compare_runtime_files(path, root))
        write_checksums(output)
        if smoke and not problems:
            _smoke(output, core)
    finally:
        if previous_epoch is None:
            os.environ.pop("SOURCE_DATE_EPOCH", None)
        else:
            os.environ["SOURCE_DATE_EPOCH"] = previous_epoch
    assets = sorted(path.name for path in output.iterdir() if path.is_file())
    return {"ok": not problems, "problems": problems, "assets": assets}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--smoke", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    report = build_release_assets(root, args.output, smoke=args.smoke)
    print(json.dumps({"ok": report["ok"], "problems": report["problems"], "assets": report["assets"]}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
