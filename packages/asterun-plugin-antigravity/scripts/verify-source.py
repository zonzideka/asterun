"""核验 Antigravity 插件副本。失败时直接退出，不使用 assert。"""
from pathlib import Path
import ast
import hashlib
import json
import subprocess
import sys

SOURCE_COMMIT = "9610e99f95a5614f6f8e1b0f10bf87d9d4f67f71"
PROOF_SHA256 = "3011594370b7b1d899cecfad99713efa8f496777472a5a35f5c8a67798515267"


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def vendor_files(vendor: Path) -> set[str]:
    present = set()
    if not vendor.is_dir():
        fail("缺少 _vendor 目录")
    for path in vendor.rglob("*"):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        present.add(f"_vendor/{path.relative_to(vendor).as_posix()}")
    return present


def git_bytes(repo: Path, spec: str) -> bytes:
    shown = subprocess.run(
        ["git", "-C", str(repo), "show", spec],
        capture_output=True,
    )
    if shown.returncode != 0:
        detail = shown.stderr.decode("utf-8", errors="replace").strip()
        fail(detail or f"无法读取来源对象 {spec}")
    return shown.stdout


def transformed(row: dict, raw: bytes) -> bytes:
    text = raw.decode("utf-8")
    extract = row.get("extract")
    if extract:
        node = next((item for item in ast.parse(text).body
                     if isinstance(item, ast.FunctionDef) and item.name == extract), None)
        if node is None:
            fail(f"来源中找不到函数 {extract}")
        segment = ast.get_source_segment(text, node)
        if not segment:
            fail(f"无法提取函数 {extract}")
        text = (row.get("prefix") or "") + segment + "\n"
    for old, new in row.get("replacements") or []:
        if old not in text:
            fail(f"机械替换未命中：{row.get('target')}")
        text = text.replace(old, new)
    return text.encode("utf-8")


def main() -> None:
    package = Path(__file__).resolve().parents[1]
    repo = Path(sys.argv[1]).resolve() if len(sys.argv) == 2 else package.parents[1]
    base = package / "src/asterun_plugin_antigravity"
    proof_bytes = (base / "vendor-source.json").read_bytes()
    digest = sha256(proof_bytes)
    if digest != PROOF_SHA256:
        fail("来源清单摘要不匹配")
    proof = json.loads(proof_bytes)
    if proof.get("source_commit") != SOURCE_COMMIT:
        fail("来源提交标识不匹配")
    rows = proof.get("files")
    if not isinstance(rows, list) or not rows:
        fail("来源清单缺少文件")
    listed = []
    for row in rows:
        target = row.get("target")
        if not isinstance(target, str) or not target.startswith("_vendor/") or target in listed:
            fail("清单目标无效或重复")
        listed.append(target)
    present = vendor_files(base / "_vendor")
    if set(listed) != present:
        fail("清单与 _vendor 不一致：缺少 "
             + ",".join(sorted(present - set(listed)))
             + "；多余 " + ",".join(sorted(set(listed) - present)))
    source_git_object_checked = False
    if (repo / ".git").exists():
        probe = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-e", f"{SOURCE_COMMIT}^{{commit}}"],
            capture_output=True, text=True,
        )
        if probe.returncode != 0:
            fail("来源提交不在历史中")
        source_git_object_checked = True
    for row in rows:
        target = row["target"]
        vendored = (base / target).read_bytes()
        if sha256(vendored) != row.get("vendored_sha256"):
            fail(f"副本摘要不匹配：{target}")
        source = row.get("source")
        if source is None:
            if row.get("source_sha256") is not None or row.get("extract") or row.get("replacements"):
                fail(f"本地副本不应声明来源变换：{target}")
            continue
        if source_git_object_checked:
            committed = git_bytes(repo, f"{SOURCE_COMMIT}:{source}")
            if sha256(committed) != row.get("source_sha256"):
                fail(f"来源提交中的摘要不匹配：{source}")
        work = (repo / source).read_bytes()
        if sha256(work) != row.get("source_sha256"):
            fail(f"工作区来源与钉定摘要不一致：{source}")
        produced = transformed(row, work)
        if produced != vendored or sha256(produced) != row.get("vendored_sha256"):
            fail(f"副本与机械变换不一致：{target}")
    print(json.dumps({
        "source_commit": SOURCE_COMMIT,
        "verified_files": len(rows),
        "original_sources_unchanged": True,
        "source_git_object_checked": source_git_object_checked,
        "verification_basis": "pinned-file-sha256-mechanical-transform-and-git-object"
        if source_git_object_checked else "pinned-file-sha256-and-mechanical-transform",
        "manifest_sha256": digest,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
