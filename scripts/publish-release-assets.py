#!/usr/bin/env python3
"""按草稿续传 GitHub 预发布。已发布且摘要一致时直接成功，摘要不同则拒绝覆盖。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


class ReleaseRejected(RuntimeError):
    """已有资产与本次构建不一致，或已发布的发行不完整。"""


def plan_release(*, exists: bool, draft: bool, remote: dict[str, str], local: dict[str, str]) -> dict[str, object]:
    if not local:
        raise ReleaseRejected("没有本地发行资产")
    extra = sorted(set(remote) - set(local))
    if extra:
        raise ReleaseRejected("远端含有本地构建没有的资产，拒绝删除或覆盖：" + ", ".join(extra))
    mismatched = sorted(name for name, digest in remote.items() if local.get(name) != digest)
    if mismatched:
        raise ReleaseRejected("已有资产与本次构建不一致，拒绝覆盖：" + ", ".join(mismatched))
    missing = sorted(name for name in local if name not in remote)
    if not exists:
        return {"action": "create", "upload": sorted(local)}
    if not draft:
        if missing:
            raise ReleaseRejected("已发布的发行缺少资产，拒绝补传到已发布版本：" + ", ".join(missing))
        return {"action": "noop", "upload": []}
    if missing:
        return {"action": "resume", "upload": missing}
    return {"action": "publish", "upload": []}


def local_digests(directory: Path) -> dict[str, str]:
    files = [path for path in directory.iterdir() if path.is_file()]
    if not files:
        raise ReleaseRejected("发行目录没有文件")
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files}


def _project_version(root: Path) -> str:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'(?m)^version = "([^"]+)"$', text)
    if not match:
        raise ReleaseRejected("pyproject.toml 缺少 version")
    return match.group(1)


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=False, capture_output=True, text=True)


def _digest(raw: object) -> str:
    text = str(raw or "")
    if text.startswith("sha256:"):
        text = text.split(":", 1)[1]
    if re.fullmatch(r"[0-9a-fA-F]{64}", text):
        return text.lower()
    return ""


def view_release(tag: str) -> dict[str, object] | None:
    result = _run(["gh", "release", "view", tag, "--json", "isDraft,assets"])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if detail.lower() == "release not found":
            return None
        raise ReleaseRejected(detail or "无法读取发行")
    payload = json.loads(result.stdout)
    remote: dict[str, str] = {}
    missing = []
    for item in payload.get("assets") or []:
        digest = _digest(item.get("digest"))
        remote[str(item["name"])] = digest
        if not digest:
            missing.append(str(item["name"]))
    if missing:
        raise ReleaseRejected("发行资产缺少 sha256 digest：" + ", ".join(missing))
    return {"draft": bool(payload.get("isDraft")), "assets": remote}


def _plan(state: dict[str, object] | None, local: dict[str, str]) -> dict[str, object]:
    if state is None:
        return plan_release(exists=False, draft=False, remote={}, local=local)
    remote = state["assets"]
    assert isinstance(remote, dict)
    return plan_release(exists=True, draft=bool(state["draft"]), remote=remote, local=local)


def _notes(tag: str, commit: str) -> str:
    return (
        f"本预发布由标签 {tag} 的提交 {commit} 构建。\n\n"
        "资产为核心 wheel、源码包、标准 skill zip、各外部插件 wheel 与源码包，以及 SHA256SUMS。"
        "SHA256SUMS 每行是小写 SHA-256、两个空格和文件名，不含清单自身。"
        "来源证明由 actions/attest-build-provenance 写入本仓库，可用 gh attestation verify 核对。"
        "安装前先核对 SHA256SUMS。本工作流只创建 GitHub 预发布，不需要额外密钥。\n\n"
        f"说明：https://github.com/zonzideka/asterun/blob/{commit}/docs/release-v0.1.md\n"
    )


def _write_output(mode: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"mode={mode}\n")


def _head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
    ).stdout.strip()


def gate(directory: Path, tag: str) -> dict[str, object]:
    plan = _plan(view_release(tag), local_digests(directory))
    mode = "noop" if plan["action"] == "noop" else "publish"
    _write_output(mode)
    return {"mode": mode, "action": plan["action"], "upload": plan["upload"]}


def apply_release(directory: Path, tag: str, root: Path) -> dict[str, object]:
    local = local_digests(directory)
    state = view_release(tag)
    plan = _plan(state, local)
    if plan["action"] == "noop":
        return {"ok": True, "action": "noop", "upload": []}
    notes = Path(tempfile.mkdtemp(prefix="asterun-release-notes-")) / "notes.md"
    notes.write_text(_notes(tag, _head()), encoding="utf-8")
    title = f"Asterun {_project_version(root)}"
    if plan["action"] == "create":
        created = _run([
            "gh", "release", "create", tag, "--draft", "--prerelease",
            "--title", title, "--notes-file", str(notes),
        ])
        if created.returncode != 0:
            state = view_release(tag)
            if state is None:
                raise ReleaseRejected(created.stderr.strip() or "无法创建草稿发行")
            plan = _plan(state, local)
            if plan["action"] == "noop":
                return {"ok": True, "action": "noop", "upload": []}
    else:
        edited = _run(["gh", "release", "edit", tag, "--notes-file", str(notes)])
        if edited.returncode != 0:
            raise ReleaseRejected(edited.stderr.strip() or "无法更新草稿说明")
    upload = [str(directory / name) for name in plan["upload"]]
    if upload:
        uploaded = _run(["gh", "release", "upload", tag, *upload])
        if uploaded.returncode != 0:
            raise ReleaseRejected(uploaded.stderr.strip() or "上传草稿资产失败")
    state = view_release(tag)
    plan = _plan(state, local)
    if plan["upload"]:
        raise ReleaseRejected("上传后仍有缺失资产：" + ", ".join(plan["upload"]))
    if plan["action"] == "publish":
        published = _run(["gh", "release", "edit", tag, "--draft=false", "--prerelease"])
        if published.returncode != 0:
            raise ReleaseRejected(published.stderr.strip() or "无法把草稿标为预发布")
    elif plan["action"] != "noop":
        raise ReleaseRejected(f"上传后的发行状态无法发布：{plan['action']}")
    final = _plan(view_release(tag), local)
    if final["action"] != "noop":
        raise ReleaseRejected("发布后的资产与本地构建不一致")
    return {"ok": True, "action": "published", "upload": upload}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--phase", choices=("gate", "apply"), required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        report = gate(args.assets, args.tag) if args.phase == "gate" else apply_release(args.assets, args.tag, root)
    except ReleaseRejected as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
