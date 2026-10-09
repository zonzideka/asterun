#!/usr/bin/env python3
"""确认发行标签等于核心版本，且当前提交已经在 main 上。"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess


def project_version(path: Path) -> str:
    match = re.search(r'(?m)^version = "([^"]+)"$', path.read_text(encoding="utf-8"))
    if not match:
        raise SystemExit(f"无法读取版本：{path}")
    return match.group(1)


def _ref(repo: Path, name: str) -> str:
    for candidate in (f"origin/{name}", name):
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", candidate],
            cwd=repo, capture_output=True, text=True,
        )
        if result.returncode == 0:
            return candidate
    raise SystemExit(f"找不到分支 {name}")


def ensure_tag_commit(repo: Path, tag: str, *, branch: str = "main") -> str:
    repo = repo.resolve()
    version = project_version(repo / "pyproject.toml")
    if tag != f"v{version}":
        raise SystemExit(f"标签 {tag} 与核心版本 v{version} 不一致")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()
    ref = _ref(repo, branch)
    contained = subprocess.run(["git", "merge-base", "--is-ancestor", head, ref], cwd=repo)
    if contained.returncode != 0:
        raise SystemExit(f"标签提交 {head} 不在 {ref} 上")
    return head


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--branch", default="main")
    args = parser.parse_args()
    print(ensure_tag_commit(Path.cwd(), args.tag, branch=args.branch))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
