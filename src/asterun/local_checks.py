"""固定命令的局部检查：仅复制所选输入，在 OS 禁网沙箱中执行。"""
from __future__ import annotations

import json
import hashlib
import platform
import time
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from threading import Thread

from asterun.errors import AsterunError
from asterun.quality import digest, snapshot


def validate_checks(value):
    def invalid():
        raise AsterunError("INVALID_CONFIG", "local_checks 需要固定 argv、inputs、timeout_seconds 和可选 runtime_roots/result_file")
    if not isinstance(value, dict) or len(value) > 32:
        invalid()
    for name, spec in value.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name) or not isinstance(spec, dict):
            invalid()
        if set(spec) - {"argv", "inputs", "timeout_seconds", "runtime_roots", "result_file", "cache", "stage"}:
            invalid()
        argv, inputs = spec.get("argv"), spec.get("inputs")
        if (not isinstance(argv, list) or not 1 <= len(argv) <= 64
                or any(not isinstance(v, str) or not v or "\x00" in v or len(v) > 4000 for v in argv)
                or not Path(argv[0]).is_absolute()):
            invalid()
        if (not isinstance(inputs, list) or not 1 <= len(inputs) <= 64
                or any(not isinstance(p, str) for p in inputs) or len(inputs) != len(set(inputs))):
            invalid()
        for path in inputs + ([spec["result_file"]] if "result_file" in spec else []):
            if not isinstance(path, str) or not Path(path).parts or Path(path).is_absolute() or str(Path(path)) != path or any(p in {"..", ".git", ".asterun-check-tmp"} for p in Path(path).parts):
                invalid()
        if spec.get("result_file") in inputs:
            invalid()
        if not isinstance(spec.get("stage", "coding"), str) or spec.get("stage", "coding") not in {"coding", "consumer", "integration"}:
            invalid()
        if "cache" in spec and type(spec["cache"]) is not bool:
            invalid()
        timeout = spec.get("timeout_seconds", 30)
        if type(timeout) is not int or not 1 <= timeout <= 60:
            invalid()
        roots = spec.get("runtime_roots", [])
        if not isinstance(roots, list) or len(roots) > 16:
            invalid()
        if any(not isinstance(p, str) or not Path(p).is_absolute() or ".." in Path(p).parts or p == "/" for p in roots):
            invalid()
    return value


def runtime_paths(argv, roots):
    runtime = [Path(p).resolve() for p in roots]
    runtime += [Path(argv[0]).resolve().parent]
    runtime += [Path(p).resolve() for p in ("/usr", "/bin", "/sbin", "/System/Library", "/Library/Apple", "/lib", "/lib64") if Path(p).exists()]
    return sorted(set(runtime))


def sandbox_command(argv, scratch, roots):
    runtime = runtime_paths(argv, roots)
    if sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file():
        read = " ".join(f"(subpath {json.dumps(str(p))})" for p in [scratch, *runtime])
        profile = ("(version 1)(deny default)(allow process*)(allow sysctl-read)(allow mach-lookup)"
                   "(allow file-read-metadata)(allow file-read* (literal \"/\"))" + f"(allow file-read* {read})"
                   + f"(allow file-write* (subpath {json.dumps(str(scratch))}))"
                   + '(allow file-read* file-write* (literal "/dev/null") (literal "/dev/urandom") (literal "/dev/random"))')
        return ["/usr/bin/sandbox-exec", "-p", profile, *argv]
    bwrap = shutil.which("bwrap")
    if sys.platform.startswith("linux") and bwrap:
        cmd = [bwrap, "--unshare-all", "--die-with-parent", "--new-session", "--proc", "/proc", "--dev", "/dev"]
        for root in runtime:
            cmd.extend(["--ro-bind", str(root), str(root)])
        # On merged-/usr Linux hosts the dynamic loader and command paths still
        # use /lib*, /bin and /sbin. Preserve those system symlinks in the sandbox.
        for name in ("/bin", "/sbin", "/lib", "/lib64"):
            if Path(name).is_symlink():
                cmd.extend(["--symlink", os.readlink(name), name])
        return [*cmd, "--bind", str(scratch), str(scratch), "--chdir", str(scratch), *argv]
    raise AsterunError("CAPABILITY_UNSUPPORTED", "受控检查需要 macOS sandbox-exec 或 Linux bubblewrap；不会无隔离执行")


def execute_check(root, bound, spec, name, state_dir=None, *, stopping=None):
    for runtime_root in runtime_paths(spec["argv"], spec.get("runtime_roots", [])):
        runtime = Path(runtime_root).resolve()
        if root.resolve().is_relative_to(runtime) or runtime.is_relative_to(root.resolve()):
            raise AsterunError("INVALID_REQUEST", "运行时读取范围不能覆盖原工作区；请将检查输入纳入快照")
    target, files = snapshot(root, bound["target_paths"], state_dir=state_dir)
    if target["hash"] != bound["target_hash"]:
        raise AsterunError("REVISION_CONFLICT", "检查前源码已改变")
    if not set(spec["inputs"]).issubset(bound["target_paths"]):
        raise AsterunError("INVALID_REQUEST", "检查的全部 inputs 必须属于版本绑定的 target_paths")
    selected = [f for f in files if f["path"] in spec["inputs"]]
    if any(f.get("missing") for f in selected):
        raise AsterunError("INVALID_REQUEST", "检查输入文件缺失")
    with tempfile.TemporaryDirectory(prefix="asterun-check-") as directory:
        scratch = Path(directory).resolve()
        for item in selected:
            path = scratch / item["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(item["content"], encoding="utf-8")
            path.chmod(item["mode"] & 0o777)
        temp = scratch / ".asterun-check-tmp"
        temp.mkdir(mode=0o700)
        argv = list(spec["argv"])
        command = sandbox_command(argv, scratch, spec.get("runtime_roots", []))
        env = {"PATH": "/usr/bin:/bin", "HOME": str(temp), "TMPDIR": str(temp),
               "LANG": "en_US.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
        from asterun.backends.grok_code import CodeProcessHandle
        process = CodeProcessHandle()
        output = bytearray()
        total = 0
        timed_out = False
        cancelled = False
        try:
            proc = process.spawn(command, cwd=scratch, env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
            def read():
                nonlocal total
                while True:
                    block = proc.stdout.read(4096)
                    if not block:
                        break
                    total += len(block)
                    output.extend(block)
                    del output[:-8000]
            reader = Thread(target=read, daemon=True)
            reader.start()
            deadline = time.monotonic() + spec.get("timeout_seconds", 30)
            while proc.poll() is None:
                if stopping is not None and stopping.is_set():
                    cancelled = True
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    proc.wait(timeout=min(0.05, remaining))
                except subprocess.TimeoutExpired:
                    pass
            cancelled = cancelled or (stopping is not None and stopping.is_set())
            exit_code = proc.poll()
        finally:
            process.close()
        reader.join(timeout=1)
        if reader.is_alive():
            raise AsterunError("REMOTE_STATE_UNKNOWN", "检查进程输出未关闭，未生成有效报告")
        proc.stdout.close()
        counts = None
        counts_valid = "result_file" not in spec
        if "result_file" in spec:
            path = scratch / spec["result_file"]
            try:
                if any(p.is_symlink() for p in (path, *path.parents)) or not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > 4096:
                    raise ValueError()
                counts = json.loads(path.read_text())
                counts_valid = (isinstance(counts, dict) and set(counts) == {"passed", "failed", "skipped", "load_errors"}
                                and all(type(v) is int and v >= 0 for v in counts.values()))
            except (OSError, ValueError, UnicodeError):
                counts_valid = False
            if not counts_valid:
                counts = None
        passed = (exit_code == 0 and not timed_out and not cancelled and counts_valid
                  and (counts is None or counts["passed"] > 0 and counts["failed"] == 0 and counts["load_errors"] == 0))
        diagnostic = output.decode("utf-8", errors="replace")[-3000:]
    after, _ = snapshot(root, bound["target_paths"], state_dir=state_dir)
    if after != target:
        raise AsterunError("REVISION_CONFLICT", "检查期间源码改变，报告已作废")
    detail = f"exit={exit_code}, timeout={timed_out}, cancelled={cancelled}, counts={counts}, counts_valid={counts_valid}\n{diagnostic}"
    return {"source": "isolated_local_check", "check_name": name, "passed": passed,
            "exit_code": exit_code, "timed_out": timed_out, "cancelled": cancelled, "counts": counts,
            "output_truncated": total > len(diagnostic.encode("utf-8")), "diagnostic": diagnostic,
            "output_bytes": total, "retained_diagnostic_bytes": len(diagnostic.encode("utf-8")),
            "diagnostic_sha256": hashlib.sha256(diagnostic.encode("utf-8")).hexdigest(),
            "before_hash": target["hash"], "after_hash": after["hash"], "check_sha256": digest(spec),
            "report": {"task_id": bound["task_id"], "run_id": bound["run_id"],
                       "input_hash": bound["input_hash"], "target_hash": bound["target_hash"],
                       "checks": [{"name": name, "passed": passed, "detail": detail[:4000]}]},
            "acceptance_unchanged": True, "independent_review": False}


def runtime_fingerprint(spec):
    """覆盖沙箱可读取运行时的有界元数据清单；无法完整枚举就禁用缓存。

    ctime/inode/device/mode/size/mtime 一并绑定，避免只改内容再恢复 mtime
    命中旧缓存。文件系统不支持可靠的这些元数据时应保持 cache=false。
    指纹只留摘要，不持久化运行时目录内容或清单。
    """
    if not spec.get("cache", False):
        return None
    hasher = hashlib.sha256()
    hasher.update(repr((platform.platform(), platform.version(), sys.version)).encode())
    seen = set()
    deadline = time.monotonic() + 2
    pending = list(runtime_paths(spec["argv"], spec.get("runtime_roots", [])))
    pending += [Path(spec["argv"][0]), Path("/usr/bin/sandbox-exec") if sys.platform == "darwin"
                else Path(shutil.which("bwrap") or "/nonexistent-bwrap")]
    try:
        for source in (Path(__file__), Path(__file__).with_name("check_runtime.py")):
            hasher.update(source.read_bytes())
        while pending:
            path = pending.pop()
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            if len(seen) > 100000 or time.monotonic() > deadline:
                return None
            st = path.lstat()
            hasher.update(repr((key, st.st_dev, st.st_ino, st.st_mode, st.st_size,
                                st.st_mtime_ns, st.st_ctime_ns)).encode())
            if stat.S_ISLNK(st.st_mode):
                hasher.update(os.readlink(path).encode())
                pending.append(path.resolve(strict=True))
            elif stat.S_ISDIR(st.st_mode):
                pending.extend(sorted(path.iterdir()))
            elif not stat.S_ISREG(st.st_mode):
                return None
        # 执行文件同时绑定实际字节；不把原始字节放入状态库。
        with Path(spec["argv"][0]).open("rb") as stream:
            for block in iter(lambda: stream.read(65536), b""):
                if time.monotonic() > deadline:
                    return None
                hasher.update(block)
    except (OSError, RuntimeError):
        return None
    return hasher.hexdigest()
