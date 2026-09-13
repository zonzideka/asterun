#!/usr/bin/env python3
"""检查 wheel/sdist 不含参考快照、凭据或作者路径。"""

from __future__ import annotations

import tarfile
import tempfile
import zipfile
import posixpath
import re
import hashlib
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

FORBIDDEN_SUBSTR = (
    "references/legacy-agent-runner",
    "credentials/",
    ".env",
    "ANTHROPIC_API_KEY=",
    "OPENAI_API_KEY=",
    "GROK_API_KEY=",
)

# 用平台目录结构识别本机路径，示例可使用 /path/to/workspace。
PRIVATE_CONTENT = (
    ("作者家目录", re.compile(rb"(?<![A-Za-z0-9_.-])/(?:Users|home)/[^/\s\"'`<>]+", re.IGNORECASE)),
    ("Windows 用户目录", re.compile(rb"[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/][^\\/\s\"'<>]+", re.IGNORECASE)),
    ("本机卷目录", re.compile(rb"/(?:Volumes|media)/[^/\s\"'<>]+/")),
)
PRIVATE_MEMBERS = (
    "docs/reviews/", "docs/handoffs/", "docs/migrations/",
    "docs/environment-baseline.md", "docs/legacy-source-manifest.json",
    "docs/en/reviews/", "docs/en/handoffs/", "docs/en/migrations/",
    "docs/en/environment-baseline.md",
)

PUBLIC_GUIDES = (
    "user-manual", "install", "antigravity", "approval-policy", "pr-review",
    "fixed-read-scope", "codex-projects", "plugins", "capability-profile",
    "review-consumer", "release-v0.1",
)
PLUGIN_NAMES = ("antigravity", "cursor", "jules")

# Apache 官方原文：https://www.apache.org/licenses/LICENSE-2.0.txt
APACHE_LICENSE_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"


def _names_from_archive(path: Path) -> list[str]:
    if path.suffix == ".whl" or path.name.endswith(".whl"):
        with zipfile.ZipFile(path) as archive:
            return archive.namelist()
    with tarfile.open(path, "r:*") as archive:
        return archive.getnames()


def inspect_archive(path: Path) -> list[str]:
    names = _names_from_archive(path)
    problems: list[str] = []
    joined = "\n".join(names)
    for needle in FORBIDDEN_SUBSTR:
        if needle in joined:
            problems.append(f"{path.name} 含有禁止片段 {needle}")
    for description, pattern in PRIVATE_CONTENT:
        if pattern.search(joined.encode()):
            problems.append(f"{path.name} 的成员名含有{description}")
    if any(needle in name for name in names for needle in PRIVATE_MEMBERS):
        problems.append(f"{path.name} 含有本机历史或开发交接材料")
    if any(name.rsplit("/", 1)[-1] in {"STATUS.md", "STATUS.en.md", "AGENTS.md"} for name in names):
        problems.append(f"{path.name} 含有仓库状态或开发者指令")
    if any(item.endswith(".sqlite") for item in names):
        problems.append(f"{path.name} 含有 sqlite 状态文件")
    if not any("asterun" in item and item.endswith(".py") for item in names):
        problems.append(f"{path.name} 缺少 asterun 包文件")
    files = _archive_files(path)
    problems.extend(inspect_license(files, wheel=path.suffix == ".whl"))
    for name, content in files.items():
        for description, pattern in PRIVATE_CONTENT:
            if pattern.search(content):
                # 只报告文件与类别，不把可能包含身份或凭据的原文打到 stdout。
                problems.append(f"{path.name} 的 {name} 含有{description}")
    if path.suffix != ".whl":
        problems.extend(inspect_bilingual_documents(files))
        problems.extend(inspect_document_links(files))
    return problems


def inspect_license(files: dict[str, bytes], *, wheel: bool) -> list[str]:
    """检查每个独立发行包的许可声明和随包原文。"""
    metadata_paths = [name for name in files if (
        name.endswith(".dist-info/METADATA") if wheel
        else name.endswith("/PKG-INFO") and name.count("/") == 1
    )]
    if len(metadata_paths) != 1:
        return ["发行包缺少唯一的许可元数据"]
    name = metadata_paths[0]
    metadata = BytesParser().parsebytes(files[name])
    license_name = metadata.get("License-Expression") or metadata.get("License")
    problems = []
    if license_name != "Apache-2.0":
        problems.append(f"{name} 的许可声明须为 Apache-2.0")
    parent = posixpath.dirname(name)
    paths = [parent + "/LICENSE"]
    if wheel:
        paths.append(parent + "/licenses/LICENSE")
    if not any(hashlib.sha256(files[path]).hexdigest() == APACHE_LICENSE_SHA256
               for path in paths if path in files):
        problems.append(f"{parent} 缺少完整的 Apache-2.0 LICENSE 原文")
    return problems


def _archive_files(path: Path) -> dict[str, bytes]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}
    with tarfile.open(path, "r:*") as archive:
        return {member.name: archive.extractfile(member).read()
                for member in archive if member.isfile()}


def inspect_bilingual_documents(files: dict[str, bytes]) -> list[str]:
    """核心与独立插件的源码包同时包含中英文操作文档。"""
    metadata_paths = [name for name in files if name.endswith("/PKG-INFO") and name.count("/") == 1]
    if len(metadata_paths) != 1:
        return []  # 许可检查已报告缺失的包元数据。
    metadata_path = metadata_paths[0]
    name = BytesParser().parsebytes(files[metadata_path]).get("Name")
    required = []
    if name == "asterun":
        required = ["README.md", "README.en.md", "examples/non-code/README.md", "examples/non-code/README.en.md",
                    "docs/protocol/IMPLEMENTATION.md", "docs/en/protocol/IMPLEMENTATION.md"]
        required.extend(f"docs/{prefix}{guide}.md" for guide in PUBLIC_GUIDES for prefix in ("", "en/"))
        required.extend(f"packages/asterun-plugin-{plugin}/{readme}"
                        for plugin in PLUGIN_NAMES for readme in ("README.md", "README.en.md"))
    elif name in {f"asterun-plugin-{plugin}" for plugin in PLUGIN_NAMES}:
        required = ["README.md", "README.en.md"]
    prefix = posixpath.dirname(metadata_path)
    return [f"{name} 源码包缺少双语文档：{path}" for path in required if f"{prefix}/{path}" not in files]


def inspect_document_links(files: dict[str, bytes]) -> list[str]:
    """发行手册的相对文件链接必须在同一个 sdist 中可用。"""
    problems = []
    for name, content in files.items():
        if not name.endswith(".md"):
            continue
        for target in re.findall(r"\]\(([^)\s]+)\)", content.decode("utf-8")):
            parsed = urlsplit(target.strip("<>"))
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            linked = posixpath.normpath(posixpath.join(posixpath.dirname(name), unquote(parsed.path)))
            if linked not in files and not any(item.startswith(linked.rstrip("/") + "/") for item in files):
                problems.append(f"{name} 的相对文档链接不在发行包内：{target}")
    return problems


def inspect_installed_package() -> list[str]:
    import asterun

    root = Path(asterun.__file__).resolve().parent
    problems: list[str] = []
    for path in root.rglob("*"):
        text = str(path)
        if "references/legacy-agent-runner" in text:
            problems.append(f"已安装包含参考快照：{path}")
        if path.suffix == ".sqlite":
            problems.append(f"已安装包含状态库：{path}")
    return problems


def compare_runtime_files(archive_path: Path, root: Path) -> list[str]:
    """以实际源码集合校验发布字节，避免秘密过滤通配符误删凭据解析模块。"""
    source = root / "src"
    expected = {str(path.relative_to(source)): path.read_bytes()
                for path in (source / "asterun").rglob("*")
                if path.is_file() and (path.suffix == ".py" or path.parent.name == "schemas" and path.suffix == ".json")}
    if archive_path.suffix == ".whl":
        with zipfile.ZipFile(archive_path) as archive:
            actual = {name: archive.read(name) for name in archive.namelist() if name in expected}
    else:
        actual = {}
        with tarfile.open(archive_path, "r:*") as archive:
            for item in archive:
                if not item.isfile() or "/src/" not in item.name:
                    continue
                name = item.name.split("/src/", 1)[1]
                if name in expected:
                    with archive.extractfile(item) as stream:
                        actual[name] = stream.read()
    return [f"{archive_path.name} 运行文件缺失或字节不一致：{name}" for name, content in expected.items()
            if actual.get(name) != content]


def build_and_check(root: Path) -> dict[str, object]:
    import os

    problems = inspect_installed_package()
    try:
        from setuptools.build_meta import build_sdist, build_wheel
    except ImportError:
        problems.append("缺少构建依赖，未完成发布包检查")
        return {
            "sdist": None,
            "wheel": None,
            "problems": problems,
            "ok": False,
            "built": False,
        }

    import warnings

    previous = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="asterun-release-") as raw:
        out = Path(raw)
        os.chdir(root)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                sdist = out / build_sdist(str(out))
                wheel = out / build_wheel(str(out))
        finally:
            os.chdir(previous)
        problems.extend(inspect_archive(sdist))
        problems.extend(inspect_archive(wheel))
        problems.extend(compare_runtime_files(sdist, root))
        problems.extend(compare_runtime_files(wheel, root))
        return {
            "sdist": str(sdist),
            "wheel": str(wheel),
            "problems": problems,
            "ok": not problems,
            "built": True,
        }


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    report = build_and_check(root)
    print(report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
