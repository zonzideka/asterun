"""插件副本相对基线有变化时，插件版本与 manifest 必须按 PEP 440 严格升高。"""
from pathlib import Path
import json
import os
import re
import subprocess
import sys

from packaging.version import InvalidVersion, Version

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = "packages/asterun-plugin-antigravity"
WATCH = (
    f"{PLUGIN}/src/asterun_plugin_antigravity/_vendor",
    f"{PLUGIN}/src/asterun_plugin_antigravity/vendor-source.json",
)
PYPROJECT = f"{PLUGIN}/pyproject.toml"
MANIFEST = f"{PLUGIN}/src/asterun_plugin_antigravity/manifest.json"
FAILURE = "插件副本内容已变化，但插件版本未升高"


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def git(repo: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=timeout,
    )


def has_commit(repo: Path, ref: str) -> bool:
    return git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode == 0


def resolve_base(repo: Path) -> str | None:
    requested = os.environ.get("ASTERUN_PLUGIN_BASE", "origin/main")
    if has_commit(repo, requested):
        return requested
    if requested == "origin/main":
        git(repo, "fetch", "origin", "main", timeout=120)
        if has_commit(repo, requested):
            return requested
    if requested != "origin/main" or os.environ.get("GITHUB_ACTIONS") == "true":
        fail(f"无法解析插件版本基线 {requested}")
    print("跳过插件副本版本检查：没有可比较的基线", file=sys.stderr)
    return None


def show(repo: Path, base: str, path: str) -> str:
    result = git(repo, "show", f"{base}:{path}")
    if result.returncode != 0:
        fail(result.stderr.strip() or f"基线中缺少 {path}")
    return result.stdout


def pyproject_version(text: str) -> str:
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"\s*$', text)
    if match is None:
        fail("无法读取插件版本")
    return match.group(1)


def manifest_version(text: str) -> str:
    try:
        value = json.loads(text).get("plugin_version")
    except json.JSONDecodeError:
        value = None
    if not isinstance(value, str) or not value:
        fail("无法读取 manifest 插件版本")
    return value


def strictly_newer(current: str, baseline: str) -> bool:
    try:
        return Version(current) > Version(baseline)
    except InvalidVersion:
        return False


def changed_paths(repo: Path, base: str) -> list[str]:
    diff = git(repo, "diff", "--name-only", base, "--", *WATCH)
    if diff.returncode != 0:
        fail(diff.stderr.strip() or "无法比较插件副本")
    status = git(repo, "status", "--porcelain", "--untracked-files=all", "--", *WATCH)
    if status.returncode != 0:
        fail(status.stderr.strip() or "无法读取插件副本状态")
    names = {line.strip() for line in diff.stdout.splitlines() if line.strip()}
    for line in status.stdout.splitlines():
        path = line[3:].strip() if len(line) > 3 else ""
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path:
            names.add(path)
    return sorted(names)


def main() -> None:
    repo = Path(sys.argv[1]).resolve() if len(sys.argv) == 2 else ROOT
    base = resolve_base(repo)
    if base is None:
        return
    changed = changed_paths(repo, base)
    if not changed:
        print("插件副本版本检查通过：内容相对基线未变化")
        return
    current_version = pyproject_version((repo / PYPROJECT).read_text())
    current_manifest = manifest_version((repo / MANIFEST).read_text())
    base_version = pyproject_version(show(repo, base, PYPROJECT))
    base_manifest = manifest_version(show(repo, base, MANIFEST))
    if (current_version != current_manifest or not strictly_newer(current_version, base_version)
            or not strictly_newer(current_manifest, base_manifest)):
        fail(FAILURE)
    print(f"插件副本版本检查通过：{base_version} -> {current_version}")


if __name__ == "__main__":
    main()
