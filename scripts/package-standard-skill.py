#!/usr/bin/env python3
"""从固定源码清单构建可独立安装的标准 skill 包。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import stat
import subprocess
import zipfile

MEMBERS = ("SKILL.md", "agents/openai.yaml", "scripts/run.py", "references/workflow.md", "LICENSE")


def build(source, output, source_commit):
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValueError("需要完整源码提交 SHA")
    source = Path(source)
    blobs = {}
    for name in MEMBERS:
        path = source / name
        current = source
        for part in Path(name).parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("skill 成员不能经过符号链接")
        if not path.is_file():
            raise ValueError("缺少 skill 成员：" + name)
        blob = path.read_bytes()
        deny = (rb"/(?:Users|home)/[^/\s]+/", rb"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY",
                rb"github_pat_[A-Za-z0-9_]{20,}", rb"(?i)Bearer\s+[A-Za-z0-9._-]{24,}")
        if any(re.search(pattern, blob) for pattern in deny):
            raise ValueError("skill 成员含本机身份或秘密模式：" + name)
        blobs[name] = blob
    match = re.search(br'^MIN_CORE_VERSION = "([^"]+)"$', blobs["scripts/run.py"], re.M)
    if not match:
        raise ValueError("skill 脚本缺少 MIN_CORE_VERSION")
    minimum = match.group(1).decode("ascii")
    if not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:(a|b|rc)(0|[1-9]\d*))?", minimum):
        raise ValueError("最低核心版本无效")
    manifest = {"schema_version": "asterun-standard-skill/v1", "source_commit": source_commit,
                "minimum_core_version": minimum,
                "legacy_compatibility": {"version": "0.1.0a13", "requires": "cli_and_resident_feature_probes"},
                "core_interface": "asterun CLI --connect >= " + minimum + "; compact observation; bound external workflow",
                "files": {name: {"bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest()}
                          for name, blob in blobs.items()}}
    blobs["manifest.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as handle, zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, blob in sorted(blobs.items()):
            info = zipfile.ZipInfo("asterun/" + name, date_time=(2026, 9, 30, 0, 0, 0))
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, blob)
    return {"path": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            "source_commit": source_commit, "members": len(blobs)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    source = root / "integrations/skills/asterun"
    dirty = subprocess.run(["git", "status", "--porcelain", "--", str(source)], cwd=root,
                           capture_output=True, check=True, timeout=15).stdout
    if dirty:
        parser.error("提交 skill 源码后再构建发布包")
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                            text=True, check=True, timeout=15).stdout.strip()
    print(json.dumps(build(source, args.output, commit), ensure_ascii=False))


if __name__ == "__main__":
    main()
