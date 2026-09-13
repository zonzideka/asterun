"""Download the release-locked Asterun wheel for a separate virtual environment."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.request


LIMIT = 16 * 1024 * 1024


def fetch(lock: dict, directory: Path, *, opener=urllib.request.urlopen) -> dict:
    wheel = lock["core_wheel"]
    filename, digest, version = wheel["filename"], wheel["sha256"], lock["core_version"]
    if not re.fullmatch(r"[0-9A-Za-z.]+", version):
        raise ValueError("invalid release version")
    if filename != f"asterun-{version}-py3-none-any.whl" or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("invalid wheel lock")
    url = f"https://github.com/zonzideka/asterun/releases/download/v{version}/{filename}"
    if wheel["url"] != url:
        raise ValueError("unexpected release URL")
    if not directory.is_absolute() or any(p.is_symlink() for p in (directory, *directory.parents)):
        raise ValueError("download directory must be absolute without symbolic links")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / filename
    if target.is_symlink():
        raise ValueError("wheel path is a symbolic link")
    if target.exists():
        if target.is_file() and target.stat().st_size <= LIMIT and hashlib.sha256(target.read_bytes()).hexdigest() == digest:
            return {"path": str(target), "sha256": digest, "reused": True, "installed": False}
        raise ValueError("existing wheel differs from the release lock")
    with opener(url, timeout=30) as response:
        data = response.read(LIMIT + 1)
    if len(data) > LIMIT or hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("download does not match the release lock")
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return {"path": str(target), "sha256": digest, "reused": False, "installed": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        lock = json.loads((Path(__file__).resolve().parents[1] / "release-lock.json").read_text(encoding="utf-8"))
        result = fetch(lock, args.output_dir)
        print(json.dumps({"ok": True, "data": result, "error": None}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"ok": False, "data": None, "error": {
            "code": "RUNTIME_DOWNLOAD_FAILED", "message": "无法取得匹配发行摘要的运行包；已有文件保持原状。"}}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
