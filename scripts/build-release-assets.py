#!/usr/bin/env python3
"""从当前检出构建与手工预发布相同形状的发行资产。"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile

PLUGINS = ("antigravity", "cursor", "jules")
SETUPTOOLS_VERSION = "84.0.0"
WHEEL_VERSION = "0.48.0"
SMOKE_MODULES = (
    ("asterun-plugin-antigravity", "asterun_plugin_antigravity.worker", "asterun-antigravity-worker", "asterun-antigravity-plugin"),
    ("asterun-plugin-cursor", "asterun_plugin_cursor.worker", "asterun-cursor-plugin", ""),
    ("asterun-plugin-jules", "asterun_plugin_jules.worker", "asterun-jules-plugin", ""),
)
_BUILD_SNIPPET = (
    "import sys, warnings\n"
    "warnings.simplefilter('ignore')\n"
    "from setuptools.build_meta import build_sdist, build_wheel\n"
    "destination = sys.argv[1]\n"
    "build_sdist(destination)\n"
    "build_wheel(destination)\n"
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
    """标签检出的提交时间。环境里已有的 SOURCE_DATE_EPOCH 不参与发行构建。"""
    stamp = subprocess.run(
        ["git", "log", "-1", "--format=%ct"], cwd=root, check=True, capture_output=True, text=True,
    ).stdout.strip()
    if not re.fullmatch(r"[0-9]+", stamp):
        raise RuntimeError("无法读取提交时间")
    return stamp


def _clean_build_residue(source: Path) -> None:
    for path in list(source.glob("*.egg-info")) + list(source.glob("src/*.egg-info")):
        if path.is_dir():
            shutil.rmtree(path)
    build_dir = source / "build"
    if build_dir.is_dir():
        shutil.rmtree(build_dir)


def _builder_python() -> tuple[str, tempfile.TemporaryDirectory[str] | None]:
    probe = subprocess.run(
        [sys.executable, "-c", "import setuptools, wheel; print(setuptools.__version__); print(wheel.__version__)"],
        capture_output=True, text=True,
    )
    if probe.returncode == 0 and probe.stdout.splitlines() == [SETUPTOOLS_VERSION, WHEEL_VERSION]:
        return sys.executable, None
    holder = tempfile.TemporaryDirectory(prefix="asterun-release-build-")
    venv = Path(holder.name) / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, timeout=120)
    subprocess.run(
        [str(venv / "bin" / "pip"), "install", "--disable-pip-version-check",
         f"setuptools=={SETUPTOOLS_VERSION}", f"wheel=={WHEEL_VERSION}"],
        check=True, timeout=180,
    )
    return str(venv / "bin" / "python"), holder


def _build_project(python: str, source: Path, output: Path, epoch: str) -> None:
    _clean_build_residue(source)
    env = os.environ.copy()
    env["SOURCE_DATE_EPOCH"] = epoch
    env["PYTHONHASHSEED"] = "0"
    try:
        subprocess.run(
            [python, "-c", _BUILD_SNIPPET, str(output)], cwd=source, env=env, check=True, timeout=180,
        )
    finally:
        _clean_build_residue(source)


def _zip_date(epoch: int) -> tuple[int, int, int, int, int, int]:
    stamp = time.gmtime(max(int(epoch), 315532800))
    second = stamp.tm_sec - (stamp.tm_sec % 2)
    return (stamp.tm_year, stamp.tm_mon, stamp.tm_mday, stamp.tm_hour, stamp.tm_min, second)


def _normalize_tar_gz(path: Path, epoch: int) -> None:
    entries: list[tuple[str, bool, bytes]] = []
    with tarfile.open(path, "r:gz") as archive:
        for member in archive.getmembers():
            if member.isdir():
                name = member.name if member.name.endswith("/") else member.name + "/"
                entries.append((name, True, b""))
                continue
            if not member.isreg():
                raise RuntimeError(f"{path.name} 含有非常规成员 {member.name}")
            extracted = archive.extractfile(member)
            if extracted is None:
                raise RuntimeError(f"{path.name} 无法读取 {member.name}")
            entries.append((member.name, False, extracted.read()))
    entries.sort(key=lambda item: item[0])
    names = [item[0] for item in entries]
    if len(names) != len(set(names)):
        raise RuntimeError(f"{path.name} 含有重复成员")
    temporary = path.with_name(path.name + ".norm")
    with temporary.open("wb") as handle:
        with gzip.GzipFile(filename="", mode="wb", fileobj=handle, mtime=epoch) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                for name, is_dir, payload in entries:
                    info = tarfile.TarInfo(name)
                    info.mtime = epoch
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    info.mode = 0o755 if is_dir else 0o644
                    info.type = tarfile.DIRTYPE if is_dir else tarfile.REGTYPE
                    if is_dir:
                        archive.addfile(info)
                    else:
                        info.size = len(payload)
                        archive.addfile(info, io.BytesIO(payload))
    temporary.replace(path)


def _normalize_zip(path: Path, epoch: int) -> None:
    date_time = _zip_date(epoch)
    with zipfile.ZipFile(path) as archive:
        items = [(info.filename, archive.read(info.filename)) for info in archive.infolist() if not info.is_dir()]
    items.sort(key=lambda item: (item[0].endswith("/RECORD") or item[0].endswith(".dist-info/RECORD"), item[0]))
    if len(items) != len({name for name, _payload in items}):
        raise RuntimeError(f"{path.name} 含有重复成员")
    temporary = path.with_name(path.name + ".norm")
    with zipfile.ZipFile(temporary, "w") as archive:
        for name, payload in items:
            info = zipfile.ZipInfo(filename=name, date_time=date_time)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.flag_bits = 0
            archive.writestr(info, payload)
    temporary.replace(path)


def normalize_archive(path: Path, epoch: int) -> None:
    if path.name.endswith(".tar.gz"):
        _normalize_tar_gz(path, epoch)
    elif path.suffix in {".whl", ".zip"}:
        _normalize_zip(path, epoch)
    else:
        raise RuntimeError(f"无法归一归档：{path.name}")


def _check_skill(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        if "asterun/SKILL.md" not in names or "asterun/manifest.json" not in names:
            return [f"{path.name} 缺少 skill 成员"]
        manifest = json.loads(archive.read("asterun/manifest.json"))
    if not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("source_commit", ""))):
        return [f"{path.name} 的 source_commit 无效"]
    return []


def smoke_install_arguments(pip: Path, wheels: list[Path]) -> list[str]:
    return [str(pip), "install", "--disable-pip-version-check", *[str(path) for path in wheels]]


def is_extra_requirement(spec: str) -> bool:
    marker = spec.split(";", 1)[1] if ";" in spec else ""
    return re.search(r"\bextra\b", marker) is not None


def requirement_project(spec: str) -> str:
    body = spec.split(";", 1)[0].strip()
    name = []
    for char in body:
        if char.isalnum() or char in "._-":
            name.append(char)
        else:
            break
    project = "".join(name)
    if not project:
        raise ValueError(f"无法解析依赖：{spec}")
    return project


def smoke_probe_source() -> str:
    return r"""
import importlib, importlib.metadata as metadata, json, subprocess, sys, tempfile
from pathlib import Path

def is_extra_requirement(spec):
    marker = spec.split(";", 1)[1] if ";" in spec else ""
    return " extra " in f" {marker} " or marker.strip().startswith("extra")

def requirement_project(spec):
    body = spec.split(";", 1)[0].strip()
    name = []
    for char in body:
        if char.isalnum() or char in "._-":
            name.append(char)
        else:
            break
    project = "".join(name)
    if not project:
        raise AssertionError(spec)
    return project

def import_distribution(project):
    dist = metadata.distribution(project)
    names = []
    tops = [path for path in (dist.files or []) if path.name == "top_level.txt"]
    if tops:
        text = dist.locate_file(tops[0]).read_text(encoding="utf-8")
        names = [line.strip() for line in text.splitlines() if line.strip()]
    if not names:
        names = [project.replace("-", "_")]
    imported = False
    for module_name in names:
        if module_name.isidentifier():
            importlib.import_module(module_name)
            imported = True
    if not imported:
        raise AssertionError(project)
    return dist

def run_entry(script, args, stdin=""):
    binary = Path(sys.executable).with_name(script)
    completed = subprocess.run([str(binary), *args], input=stdin, text=True, capture_output=True)
    if completed.returncode != 0:
        raise AssertionError(script + " " + " ".join(args) + " -> " + str(completed.returncode) + ": " + completed.stderr)
    return completed.stdout

expected = sys.argv[1]
import asterun
assert asterun.__version__ == expected, asterun.__version__
core = metadata.distribution("asterun")
for req in core.requires or []:
    if not is_extra_requirement(req):
        import_distribution(requirement_project(req))
version_payload = json.loads(run_entry("asterun", ["version"]))
assert version_payload.get("ok") is True, version_payload
assert version_payload.get("data", {}).get("version") == expected, version_payload

describe = json.dumps({
    "jsonrpc": "2.0",
    "id": "smoke",
    "method": "plugin.describe",
    "params": {"protocol_version": "asterun-worker/v1", "input": {}, "context": {}},
}) + "\n"

for project, module, worker, profile in json.loads(sys.argv[2]):
    dist = metadata.distribution(project)
    for req in dist.requires or []:
        if not is_extra_requirement(req):
            import_distribution(requirement_project(req))
    importlib.import_module(module)
    response = json.loads(run_entry(worker, [], describe))
    result = response["result"]
    assert result["protocol_version"] == "asterun-worker/v1", project
    manifest = result["manifest"]
    assert manifest["schema_version"] == "asterun-plugin/v1", project
    assert manifest["plugin_api_major"] == 1, project
    assert isinstance(manifest["plugin_version"], str) and manifest["plugin_version"], project
    if profile:
        home = tempfile.mkdtemp(prefix="asterun-smoke-home-")
        prepared = json.loads(run_entry(profile, ["prepare", "--home", home]))
        assert prepared.get("created") is True, prepared
print("smoke-ok")
"""


def run_release_smoke(output: Path, core_version: str) -> None:
    wheels = sorted(output.glob("*.whl"))
    core = [path for path in wheels if path.name.startswith("asterun-")]
    plugins = [path for path in wheels if path.name.startswith("asterun_plugin_")]
    if len(core) != 1 or len(plugins) != len(PLUGINS):
        raise RuntimeError("冒烟安装需要 1 个核心 wheel 和每个外部插件的 wheel")
    with tempfile.TemporaryDirectory(prefix="asterun-release-smoke-") as raw:
        venv = Path(raw) / "venv"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, timeout=120)
        pip = venv / "bin" / "pip"
        python = venv / "bin" / "python"
        subprocess.run(smoke_install_arguments(pip, [core[0], *plugins]), check=True, timeout=300)
        completed = subprocess.run(
            [str(python), "-c", smoke_probe_source(), core_version, json.dumps(SMOKE_MODULES)],
            capture_output=True, text=True, timeout=180,
        )
        if completed.returncode != 0 or "smoke-ok" not in completed.stdout:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(detail or "冒烟入口没有成功")


def _smoke(output: Path, core_version: str) -> None:
    run_release_smoke(output, core_version)


def build_release_assets(root: Path, output: Path, *, smoke: bool = True) -> dict[str, object]:
    root = root.resolve()
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise RuntimeError("发行输出目录必须为空")
    epoch = _source_date(root)
    problems: list[str] = []
    python, holder = _builder_python()
    try:
        _build_project(python, root, output, epoch)
        for plugin in PLUGINS:
            _build_project(python, root / "packages" / f"asterun-plugin-{plugin}", output, epoch)
        core = project_version(root / "pyproject.toml")
        subprocess.run(
            [sys.executable, str(root / "scripts/package-standard-skill.py"),
             "--output", str(output / f"asterun-skill-{core}.zip")],
            cwd=root, check=True, timeout=60,
        )
        for path in sorted(output.iterdir()):
            if path.is_file() and (path.name.endswith(".tar.gz") or path.suffix in {".whl", ".zip"}):
                normalize_archive(path, int(epoch))
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
            run_release_smoke(output, core)
    finally:
        if holder is not None:
            holder.cleanup()
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
