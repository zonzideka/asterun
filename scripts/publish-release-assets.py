#!/usr/bin/env python3
"""按草稿续传 GitHub 预发布。已有资产以来源证明指向的那次构建为准，不覆盖已发布文件。"""
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
    """已有资产与原构建不一致，或已发布的发行不完整。"""


RELEASE_ARTIFACT = "asterun-dist"


def plan_release(*, exists: bool, draft: bool, remote: dict[str, str], local: dict[str, str]) -> dict[str, object]:
    if not local:
        raise ReleaseRejected("没有本地发行资产")
    extra = sorted(set(remote) - set(local))
    if extra:
        raise ReleaseRejected("远端含有构建产物没有的资产，拒绝删除或覆盖：" + ", ".join(extra))
    mismatched = sorted(name for name, digest in remote.items() if local.get(name) != digest)
    if mismatched:
        raise ReleaseRejected("已有资产与构建产物不一致，拒绝覆盖：" + ", ".join(mismatched))
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


def choose_resume_files(*, exists: bool, draft: bool, remote: dict[str, str], artifact: dict[str, str], rebuilt: dict[str, str] | None = None) -> dict[str, object]:
    """artifact 来自来源证明指向的 workflow artifact。发行已经存在时忽略 rebuilt。"""
    if exists:
        rebuilt = None
    elif rebuilt not in (None, artifact):
        raise ReleaseRejected("首次发布的目录与构建产物不一致")
    return plan_release(exists=exists, draft=draft, remote=remote, local=artifact)


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


def _gh(command: str, *args: str) -> subprocess.CompletedProcess[str]:
    return _run([*command.split(), *args])


def _digest(raw: object) -> str:
    text = str(raw or "")
    if text.startswith("sha256:"):
        text = text.split(":", 1)[1]
    if re.fullmatch(r"[0-9a-fA-F]{64}", text):
        return text.lower()
    return ""


def workflow_run_id(payload: object, repo: str = "") -> str:
    found: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            invocation = node.get("invocationId")
            if isinstance(invocation, str) and (not repo or f"/{repo}/actions/runs/" in invocation):
                match = re.search(r"/actions/runs/(\d+)", invocation)
                if match:
                    found.append(match.group(1))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    if not found:
        raise ReleaseRejected("来源证明缺少构建运行编号")
    return min(found, key=int)


def view_release(tag: str) -> dict[str, object] | None:
    result = _gh("gh release view", tag, "--json", "isDraft,assets")
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


def _artifact_root(directory: Path) -> Path:
    entries = list(directory.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return directory


def fetch_original_artifact(tag: str, state: dict[str, object], repo: str) -> Path:
    assets = state.get("assets")
    if not isinstance(assets, dict) or not assets:
        raise ReleaseRejected("没有可核对的已上传资产")
    work = Path(tempfile.mkdtemp(prefix="asterun-origin-"))
    run_ids: list[str] = []
    for name in sorted(assets):
        asset_dir = work / "assets" / name
        asset_dir.mkdir(parents=True)
        downloaded = _gh("gh release download", tag, "--repo", repo, "--pattern", name, "--dir", str(asset_dir))
        if downloaded.returncode != 0:
            raise ReleaseRejected((downloaded.stderr or downloaded.stdout).strip() or f"无法下载已上传资产 {name}")
        path = asset_dir / name
        if not path.is_file():
            found = [item for item in asset_dir.iterdir() if item.is_file()]
            if len(found) != 1:
                raise ReleaseRejected(f"无法定位已上传资产 {name}")
            path = found[0]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != assets[name]:
            raise ReleaseRejected(f"已上传资产 {name} 与发行摘要不一致")
        verified = _gh("gh attestation verify", str(path), "--repo", repo, "--format", "json")
        if verified.returncode != 0:
            raise ReleaseRejected((verified.stderr or verified.stdout).strip() or f"无法核验 {name} 的来源证明")
        try:
            payload = json.loads(verified.stdout)
        except json.JSONDecodeError as error:
            raise ReleaseRejected(f"来源证明输出无法解析：{name}") from error
        run_ids.append(workflow_run_id(payload, repo))
    if len(set(run_ids)) != 1:
        raise ReleaseRejected("已上传资产的来源证明不是同一次构建")
    artifact_dir = work / "artifact"
    artifact_dir.mkdir()
    fetched = _gh(
        "gh run download", run_ids[0], "--repo", repo, "--name", RELEASE_ARTIFACT, "--dir", str(artifact_dir),
    )
    if fetched.returncode != 0:
        raise ReleaseRejected((fetched.stderr or fetched.stdout).strip() or "无法取得同一次构建的 workflow artifact")
    return _artifact_root(artifact_dir)


def publish_source(*, state: dict[str, object] | None, current: Path | None, tag: str, repo: str) -> Path:
    assets = state.get("assets") if state else None
    if state is None or not assets:
        if current is None or not current.is_dir() or not any(current.iterdir()):
            raise ReleaseRejected("首次发布需要本次构建产物")
        return current
    return fetch_original_artifact(tag, state, repo)


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


def _write_outputs(values: dict[str, str]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def _head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
    ).stdout.strip()


def _repository() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        raise ReleaseRejected("缺少 GITHUB_REPOSITORY")
    return repo


def gate(tag: str) -> dict[str, object]:
    state = view_release(tag)
    if state is not None and not state["draft"] and not state["assets"]:
        raise ReleaseRejected("已发布的发行没有资产，拒绝补传")
    if state is None or not state["assets"]:
        report = {"mode": "create", "run_id": ""}
    else:
        report = {"mode": "resume", "run_id": ""}
    _write_outputs({"mode": str(report["mode"]), "run_id": str(report["run_id"])})
    return report


def _select(state: dict[str, object] | None, source: Path, current: Path | None) -> dict[str, object]:
    remote = state["assets"] if state else {}
    assert isinstance(remote, dict)
    rebuilt = local_digests(current) if state and state["assets"] and current is not None else None
    return choose_resume_files(
        exists=state is not None,
        draft=bool(state["draft"]) if state else False,
        remote=remote,
        artifact=local_digests(source),
        rebuilt=rebuilt,
    )


def apply_release(directory: Path | None, tag: str, root: Path) -> dict[str, object]:
    repo = _repository()
    state = view_release(tag)
    current = directory if directory is not None and directory.is_dir() and any(directory.iterdir()) else None
    source = publish_source(state=state, current=current, tag=tag, repo=repo)
    plan = _select(state, source, current)
    if plan["action"] == "noop":
        return {"ok": True, "action": "noop", "upload": []}
    notes = Path(tempfile.mkdtemp(prefix="asterun-release-notes-")) / "notes.md"
    notes.write_text(_notes(tag, _head()), encoding="utf-8")
    title = f"Asterun {_project_version(root)}"
    if plan["action"] == "create":
        created = _gh(
            "gh release create", tag, "--repo", repo, "--draft", "--prerelease",
            "--title", title, "--notes-file", str(notes),
        )
        if created.returncode != 0:
            state = view_release(tag)
            if state is None:
                raise ReleaseRejected(created.stderr.strip() or "无法创建草稿发行")
            source = publish_source(state=state, current=current, tag=tag, repo=repo)
            plan = _select(state, source, current)
            if plan["action"] == "noop":
                return {"ok": True, "action": "noop", "upload": []}
    else:
        edited = _gh("gh release edit", tag, "--repo", repo, "--notes-file", str(notes))
        if edited.returncode != 0:
            raise ReleaseRejected(edited.stderr.strip() or "无法更新草稿说明")
    upload = [str(source / name) for name in plan["upload"]]
    if upload:
        uploaded = _gh("gh release upload", tag, "--repo", repo, *upload)
        if uploaded.returncode != 0:
            raise ReleaseRejected(uploaded.stderr.strip() or "上传草稿资产失败")
    state = view_release(tag)
    if state is None:
        raise ReleaseRejected("上传后找不到发行")
    confirmed = local_digests(source)
    plan = choose_resume_files(
        exists=True, draft=bool(state["draft"]), remote=state["assets"], artifact=confirmed, rebuilt=None,
    )
    if plan["upload"]:
        raise ReleaseRejected("上传后仍有缺失资产：" + ", ".join(plan["upload"]))
    if plan["action"] == "publish":
        published = _gh("gh release edit", tag, "--repo", repo, "--draft=false", "--prerelease")
        if published.returncode != 0:
            raise ReleaseRejected(published.stderr.strip() or "无法把草稿标为预发布")
    elif plan["action"] != "noop":
        raise ReleaseRejected(f"上传后的发行状态无法发布：{plan['action']}")
    final_state = view_release(tag)
    if final_state is None:
        raise ReleaseRejected("发布后找不到发行")
    final = choose_resume_files(
        exists=True, draft=bool(final_state["draft"]), remote=final_state["assets"],
        artifact=confirmed, rebuilt=None,
    )
    if final["action"] != "noop":
        raise ReleaseRejected("发布后的资产与原构建不一致")
    return {"ok": True, "action": "published", "upload": upload}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--phase", choices=("gate", "apply"), required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        if args.phase == "gate":
            report = gate(args.tag)
        else:
            report = apply_release(args.assets, args.tag, root)
    except ReleaseRejected as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
