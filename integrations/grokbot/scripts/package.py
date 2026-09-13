"""Build the explicit GrokBot template bundle without runtime or author state."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import zipfile


class PackageError(ValueError):
    pass


def member_path(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not name or "\\" in name:
        raise PackageError("invalid release member")
    relative = PurePosixPath(name)
    if relative.is_absolute() or any(p in {"", ".", ".."} for p in name.split("/")):
        raise PackageError("release member escapes root")
    target = root
    for part in relative.parts:
        target = target / part
        if target.is_symlink():
            raise PackageError("symbolic links are excluded")
    if not target.is_file() or not stat.S_ISREG(target.stat().st_mode):
        raise PackageError("release member is missing or not a regular file")
    return target


def build(root: Path, output: Path, source_commit: str) -> dict:
    root = root.resolve(strict=True)
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise PackageError("source commit must be a full Git SHA")
    names = json.loads(member_path(root, "release-files.json").read_text(encoding="utf-8"))
    if not isinstance(names, list) or not names or any(not isinstance(n, str) for n in names):
        raise PackageError("release file list is invalid")
    if len(names) != len(set(names)) or "manifest.json" in names:
        raise PackageError("duplicate or reserved release member")
    if "release-files.json" not in names or "release-lock.json" not in names:
        raise PackageError("release list and lock must be included")
    blobs = {name: member_path(root, name).read_bytes() for name in sorted(names)}
    lock = json.loads(blobs["release-lock.json"])
    version = lock["template_version"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,63}", version):
        raise PackageError("invalid template version")
    # Public artifacts carry only reviewed source; private examples stay outside.
    deny = (
        rb"/" + rb"Users/[^/\s]+/", rb"/" + rb"home/[^/\s]+/", rb"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY",
        rb"gh[pousr]_[A-Za-z0-9]{20,}", rb"github_pat_[A-Za-z0-9_]{20,}",
        rb"(?i)Bearer\s+[A-Za-z0-9._-]{24,}",
    )
    for name, blob in blobs.items():
        if name.endswith((".sqlite", ".db", ".jsonl")) or any(re.search(p, blob) for p in deny):
            raise PackageError("release member requires privacy review: " + name)
    manifest = {
        "schema_version": "asterun-grokbot-bundle/v1",
        "template_version": version,
        "source_commit": source_commit,
        "core_version": lock["core_version"],
        "files": {name: {"sha256": hashlib.sha256(blob).hexdigest(), "bytes": len(blob)}
                  for name, blob in blobs.items()},
    }
    blobs["manifest.json"] = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    if output.is_symlink() or output.exists():
        raise PackageError("output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation also protects against concurrent builders.
    with output.open("xb") as handle:
        with zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, blob in sorted(blobs.items()):
                info = zipfile.ZipInfo(f"asterun-grokbot-{version}/{name}", (2026, 1, 1, 0, 0, 0))
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, blob)
    return {"path": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            "members": len(blobs), "source_commit": source_commit}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        # The release must be attributable to committed, reviewable source.
        names = json.loads((root / "release-files.json").read_text(encoding="utf-8"))
        paths = [str(member_path(root, name)) for name in names]
        dirty = subprocess.run(["git", "status", "--porcelain", "--", *paths], cwd=root,
                               check=True, capture_output=True, timeout=15).stdout
        if dirty:
            raise PackageError("commit template files before packaging")
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                                capture_output=True, text=True, timeout=15).stdout.strip()
        result = build(root, args.output, commit)
        print(json.dumps({"ok": True, "data": result, "error": None}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        message = str(exc) if isinstance(exc, PackageError) else "cannot build template bundle"
        print(json.dumps({"ok": False, "data": None,
                          "error": {"code": "PACKAGE_INVALID", "message": message}}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
