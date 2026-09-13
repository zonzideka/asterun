"""有界 macOS 实例启用事务。计划准备不运行进程，应用不提交模型任务。

仅管理现有 Asterun LaunchAgent 的 Codex 开关、固定 binary 和工作区别名。
不提供任意环境编辑、shell、凭据复制或任务重派。原始子进程输出永不返回。
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import sqlite3
import stat
import subprocess
import sys
import sysconfig
import tempfile
import time
from typing import Callable

from asterun.config import load_config
from asterun.errors import AsterunError, BACKEND_UNAVAILABLE, INSTANCE_LOCKED, INVALID_CONFIG, INVALID_REQUEST, REVISION_CONFLICT
from asterun.instance import InstanceLock
from asterun.service import LocalClient

SAFE_ENV = frozenset({"HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "LC_CTYPE",
                      "ASTERUN_HOME", "ASTERUN_ACCOUNT_REF", "ASTERUN_RUNTIME_REF",
                      "ASTERUN_RUN_CODEX", "ASTERUN_RUN_GROK", "ASTERUN_RUN_CLAUDE", "ASTERUN_RUN_ANTIGRAVITY",
                      "CODEX_BIN", "CODEX_HOME", "PYTHONNOUSERSITE", "PYTHONDONTWRITEBYTECODE"})
PLIST_KEYS = frozenset({"Label", "ProgramArguments", "EnvironmentVariables", "RunAtLoad",
                        "KeepAlive", "ThrottleInterval", "ProcessType", "Umask", "WorkingDirectory",
                        "StandardOutPath", "StandardErrorPath", "ExitTimeOut"})


def _fail(message: str, code: str = INVALID_REQUEST, **details):
    raise AsterunError(code, message, details=details)


def _json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file(path: Path, *, source: bool = False) -> dict:
    info = path.lstat()
    owners = {os.getuid(), 0} if source else {os.getuid()}
    if not stat.S_ISREG(info.st_mode) or info.st_uid not in owners or info.st_mode & 0o022:
        _fail("文件必须为当前用户拥有且不可由其它用户写入的普通文件", path=str(path))
    return {"path": str(path), "sha256": _sha(path.read_bytes()), "mode": stat.S_IMODE(info.st_mode),
            "device": info.st_dev, "inode": info.st_ino, "owner": info.st_uid, "source": source}


def _directory(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        _fail("目录必须由当前用户拥有且不可由其它用户写入", path=str(path))


def _absolute(path: Path) -> Path:
    path = Path(path).expanduser().absolute()
    if path.is_symlink():
        _fail("服务管理目标不能是符号链接", path=str(path))
    return path.resolve()


def _write_new(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _replace(path: Path, data: bytes, expected: dict, mode: int = 0o600) -> dict:
    _check_file(expected)
    fd, temporary = tempfile.mkstemp(prefix=".asterun-service-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _check_file(expected)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return _file(path)


def _check_file(expected: dict, *, identity: bool = True) -> None:
    actual = _file(Path(expected["path"]), source=expected.get("source", False))
    keys = ("sha256", "mode", "device", "inode") if identity else ("sha256", "mode")
    if any(actual[key] != expected[key] for key in keys):
        _fail("文件已变化；拒绝覆盖，请重新准备计划", REVISION_CONFLICT, path=expected["path"])


def _current(path: Path | None) -> dict | None:
    if path is None:
        return None
    path = Path(path).expanduser().absolute()
    info = path.lstat()
    if not stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
        _fail("current 必须是当前用户拥有的符号链接", path=str(path))
    return {"path": str(path), "target": os.readlink(path), "resolved": str(path.resolve(strict=True)),
            "device": info.st_dev, "inode": info.st_ino}


def _installation_sources(executable: Path) -> set[Path]:
    """不执行解释器：只接受当前安装的标准 console script 与可确定导入来源。"""
    lines = executable.read_bytes().splitlines()
    if not lines:
        _fail("服务 console script 为空；请修复当前安装后重新准备计划", INVALID_CONFIG)
    line = lines[0].decode("utf-8", errors="strict")
    if not line.startswith("#!/") or any(char.isspace() for char in line[2:]):
        _fail("无法静态核验服务解释器；仅支持同一安装的标准 console script", INVALID_CONFIG)
    interpreter = Path(line[2:])
    current_interpreter = Path(sys.executable)
    if (interpreter.parent.resolve() != current_interpreter.parent.resolve()
            or interpreter.resolve(strict=True) != current_interpreter.resolve(strict=True)
            or executable.parent.resolve() != current_interpreter.parent.resolve()):
        _fail("服务与管理 CLI 不属于同一安装；请用服务安装内的 asterun 准备变更", INVALID_CONFIG)
    package = Path(__file__).resolve().parent
    purelib = Path(sysconfig.get_path("purelib")).resolve()
    pth = set(purelib.glob("*.pth"))
    if package != purelib / "asterun":
        # 仅接受单行路径型 editable；不执行 finder 或未知 .pth 来推断路径。
        linked = [path for path in pth if path.read_text(encoding="utf-8").strip() == str(package.parent)]
        if len(linked) != 1:
            _fail("当前源码与服务安装的导入路径无法静态对应；请使用普通 wheel 安装", INVALID_CONFIG)
    paths = {interpreter.resolve(strict=True), *pth}
    for path in (interpreter.parent.parent / "pyvenv.cfg", purelib / "sitecustomize.py"):
        if path.exists():
            paths.add(path)
    return paths


def _runtime_sources(executable: Path, binary: Path) -> list[dict]:
    package = Path(__file__).resolve().parent
    paths = set(package.rglob("*.py")) | set((package / "schemas").glob("*.json")) | {executable, binary} | _installation_sources(executable)
    return [_file(path, source=True) for path in sorted(paths)]


def _snapshot(conn: sqlite3.Connection, *, normalize_revision: int | None = None) -> dict:
    tables = sorted(row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
    required = {"meta", "runs", "operations", "control_entities", "control_outbox"}
    if not required.issubset(tables):
        _fail("状态库缺少所需表；不自动迁移或创建状态", INVALID_CONFIG)
    revision = conn.execute("SELECT value FROM meta WHERE key='applied_config_revision'").fetchone()
    if revision is None:
        _fail("状态库没有已应用 revision", REVISION_CONFLICT)
    blockers = []
    for row in conn.execute("SELECT id,payload FROM runs"):
        if json.loads(row[1]).get("status") not in {"succeeded", "failed", "cancelled"}:
            blockers.append({"kind": "legacy_run", "id": row[0]})
    for row in conn.execute("SELECT id,status FROM operations"):
        if row[1] not in {"completed", "failed"}:
            blockers.append({"kind": "legacy_operation", "id": row[0]})
    for row in conn.execute("SELECT kind,id,payload FROM control_entities WHERE kind IN ('Run','Action','Operation')"):
        allowed = {"Run": {"SUCCEEDED", "FAILED", "CANCELED"},
                   "Action": {"SUCCEEDED", "FAILED", "CANCELED", "DENIED"},
                   "Operation": {"SUCCEEDED", "FAILED", "CANCELED"}}[row[0]]
        if json.loads(row[2]).get("status") not in allowed:
            blockers.append({"kind": row[0], "id": row[1]})
    for row in conn.execute("SELECT action_id,status FROM control_outbox WHERE status != 'DONE'"):
        blockers.append({"kind": "outbox", "id": row[0]})
    digest = hashlib.sha256()
    for name in tables:
        # SQL identifier 源自 SQLite，但仍严格限于标准标识符。
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            _fail("状态库含未知表名", INVALID_CONFIG)
        rows = conn.execute(f'SELECT * FROM "{name}"').fetchall()
        if name == "meta" and normalize_revision is not None:
            rows = [(key, str(normalize_revision) if key == "applied_config_revision" else value)
                    for key, value in rows]
        encoded = sorted(_json([{"blob_sha256": _sha(v)} if isinstance(v, bytes) else v for v in row]) for row in rows)
        digest.update(_json(name))
        for row in encoded:
            digest.update(row)
    return {"applied_revision": int(revision[0]), "sha256": digest.hexdigest(), "blockers": blockers}


@contextmanager
def _database(path: Path, *, write_lock: bool = False):
    _file(path)
    conn = sqlite3.connect(path.as_uri() + ("?mode=rw" if write_lock else "?mode=ro"), uri=True,
                           isolation_level=None, timeout=1)
    try:
        conn.execute("BEGIN IMMEDIATE" if write_lock else "BEGIN")
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _candidate_config(raw: dict, request: dict, config_path: Path) -> dict:
    result = deepcopy(raw)
    result["revision"] += 1
    current = result.setdefault("backends", {}).get("codex", {})
    if current and current.get("kind") != "codex":
        _fail("名为 codex 的后端已绑定其它 kind；拒绝覆盖", INVALID_CONFIG)
    result["backends"]["codex"] = {"kind": "codex", "enabled": True, "bin": request["codex_bin"]}
    alias = request.get("workspace_alias")
    root = request.get("workspace_root")
    if bool(alias) != bool(root):
        _fail("workspace_alias 与 workspace_root 必须同时提供")
    if alias:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", alias):
            _fail("工作区别名格式无效")
        resolved = Path(root).resolve(strict=True)
        if not resolved.is_dir():
            _fail("工作区必须为现有目录")
        old = result.setdefault("workspaces", {}).get(alias)
        if old:
            old_path = Path(old["root"])
            old_root = (config_path.parent / old_path).resolve()
            if old_root != resolved:
                _fail("已有工作区别名指向另一目录；拒绝改绑", INVALID_CONFIG, workspace=alias)
        else:
            result["workspaces"][alias] = {"root": str(resolved), "allow_non_git": True}
    return result


def _candidate_plist(raw: dict, config_path: Path, state_dir: Path, binary: Path) -> tuple[dict, Path, list[str]]:
    if set(raw) - PLIST_KEYS or not re.fullmatch(r"com\.asterun\.[A-Za-z0-9_.-]+", raw.get("Label", "")):
        _fail("仅管理已知字段的 Asterun LaunchAgent", INVALID_CONFIG)
    argv = raw.get("ProgramArguments")
    env = dict(raw.get("EnvironmentVariables", {}))
    if not isinstance(argv, list) or not argv or any(not isinstance(v, str) for v in argv):
        _fail("LaunchAgent ProgramArguments 无效", INVALID_CONFIG)
    if argv[:2] == ["/usr/bin/env", "-i"]:
        offset = 2
        while offset < len(argv) and "=" in argv[offset]:
            key, value = argv[offset].split("=", 1)
            env[key] = value
            offset += 1
        argv = argv[offset:]
    if len(argv) != 6 or Path(argv[0]).name != "asterun" or argv[-1] != "serve":
        _fail("仅支持显式 asterun --config/--state-dir serve 入口", INVALID_CONFIG)
    options = dict(zip(argv[1:-1:2], argv[2:-1:2]))
    if set(options) != {"--config", "--state-dir"} or Path(options["--config"]).resolve() != config_path or Path(options["--state-dir"]).resolve() != state_dir:
        _fail("启动入口与所选配置/状态目录不一致", INVALID_CONFIG)
    executable = Path(argv[0]).resolve(strict=True)
    if not executable.is_file() or not os.access(executable, os.X_OK):
        _fail("Asterun 服务入口不可执行", INVALID_CONFIG)
    dropped = sorted(str(k) for k in env if k not in SAFE_ENV)
    clean = {k: v for k, v in env.items() if k in SAFE_ENV}
    if any(not isinstance(v, str) or "\x00" in v or "\n" in v or "\r" in v for v in clean.values()):
        _fail("已知环境变量包含无效值", INVALID_CONFIG)
    if "HOME" not in clean or not Path(clean["HOME"]).is_absolute():
        _fail("启动配置必须已有显式绝对 HOME；不推断或复制账户", INVALID_CONFIG)
    clean.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    if any(not Path(v).is_absolute() for v in clean["PATH"].split(":")):
        _fail("启动 PATH 必须只含绝对目录", INVALID_CONFIG)
    clean.update(ASTERUN_RUN_CODEX="1", CODEX_BIN=str(binary), PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
    result = deepcopy(raw)
    result.pop("EnvironmentVariables", None)
    result["ProgramArguments"] = ["/usr/bin/env", "-i"] + [f"{k}={clean[k]}" for k in sorted(clean)] + [str(executable), "--config", str(config_path), "--state-dir", str(state_dir), "serve"]
    result["Umask"] = 0o077
    return result, executable, dropped


def prepare_service_plan(*, config_path: Path, state_dir: Path, plist_path: Path,
                         plan_dir: Path, codex_bin: Path, workspace_alias: str | None = None,
                         workspace_root: Path | None = None, current_path: Path | None = None) -> dict:
    """只准备私有候选/备份。已有在途项会记录为 blocker，apply 必须重新检查。"""
    config_path, state_dir, plist_path = map(_absolute, (config_path, state_dir, plist_path))
    binary = Path(codex_bin).expanduser().resolve(strict=True)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        _fail("所选 Codex binary 不可执行", INVALID_CONFIG)
    for directory in {config_path.parent, state_dir, plist_path.parent}:
        _directory(directory)
    validated = load_config(config_path)
    original_config, original_plist = config_path.read_bytes(), plist_path.read_bytes()
    request = {"codex_bin": str(binary), "workspace_alias": workspace_alias,
               "workspace_root": str(Path(workspace_root).expanduser().resolve(strict=True)) if workspace_root else None}
    candidate_config = _candidate_config(json.loads(original_config), request, config_path)
    candidate_plist, executable, dropped = _candidate_plist(plistlib.loads(original_plist), config_path, state_dir, binary)
    with _database(state_dir / "asterun.sqlite") as conn:
        snapshot = _snapshot(conn)
    if snapshot["applied_revision"] != validated.revision:
        _fail("磁盘候选与已应用 revision 不同；先处理现有配置变更", REVISION_CONFLICT,
              applied=snapshot["applied_revision"], disk=validated.revision)
    sources = _runtime_sources(executable, binary)
    folder = Path(plan_dir).expanduser().absolute()
    _directory(folder.parent)
    folder.mkdir(mode=0o700, exist_ok=False)
    folder = folder.resolve()
    for name, data in (("config.before.json", original_config), ("launchagent.before.plist", original_plist),
                       ("config.candidate.json", _json(candidate_config)),
                       ("launchagent.candidate.plist", plistlib.dumps(candidate_plist, sort_keys=True))):
        _write_new(folder / name, data)
    plan = {"schema_version": 1, "uid": os.getuid(), "label": candidate_plist["Label"],
            "config_path": str(config_path), "state_dir": str(state_dir), "plist_path": str(plist_path),
            "request": request, "expected_applied_revision": validated.revision,
            "target_revision": candidate_config["revision"], "state": snapshot,
            "current": _current(current_path), "original": {"config": _file(config_path), "plist": _file(plist_path)},
            "sources": sources,
            "artifacts": {name: _file(folder / name) for name in ("config.before.json", "launchagent.before.plist", "config.candidate.json", "launchagent.candidate.plist")},
            "dropped_environment_names": dropped, "model_called": False}
    path = folder / "plan.json"
    _write_new(path, _json(plan))
    summary = {"plan_path": str(path), "plan_sha256": _sha(path.read_bytes()),
               "expected_applied_revision": validated.revision, "target_revision": candidate_config["revision"],
               "codex_bin": str(binary), "workspace_alias": workspace_alias,
               "blockers": snapshot["blockers"], "dropped_environment_names": dropped,
               "model_called": False, "service_changed": False}
    _write_new(folder / "summary.json", _json(summary))
    return summary


class LaunchctlRunner:
    """只执行固定 launchctl 操作；不返回 stdout/stderr/异常文本。"""
    def __call__(self, argv: list[str]) -> None:
        if sys.platform != "darwin":
            _fail("服务应用仅支持 macOS", INVALID_REQUEST)
        try:
            result = subprocess.run(["/bin/launchctl", *argv], capture_output=True, check=False,
                                    timeout=10, env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})
        except (OSError, subprocess.SubprocessError):
            _fail("launchctl 操作未确认完成；请查安全步骤记录，不自动重试", BACKEND_UNAVAILABLE)
        if result.returncode:
            _fail("launchctl 操作未成功；原始输出已丢弃", BACKEND_UNAVAILABLE, returncode=result.returncode)

    def is_loaded(self, target: str) -> bool:
        """原始 print 只停留在捕获缓冲区；仅返回精确的存在/不存在事实。"""
        match = re.fullmatch(r"gui/(\d+)/(com\.asterun\.[A-Za-z0-9_.-]+)", target)
        if sys.platform != "darwin" or not match or int(match[1]) != os.getuid():
            _fail("只能探测当前用户的 Asterun GUI 服务")
        try:
            result = subprocess.run(["/bin/launchctl", "print", target], capture_output=True,
                                    timeout=5, check=False, env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})
        except (OSError, subprocess.SubprocessError):
            _fail("未取得可信服务状态；原始输出已丢弃", BACKEND_UNAVAILABLE)
        if result.returncode == 0:
            return True
        # macOS launchctl 对不存在的指定 job 返回 113；其它失败不能冒充不存在。
        expected = f'Could not find service "{match[2]}" in domain for user gui: {match[1]}'
        error = result.stderr.decode("utf-8", errors="replace").strip()
        if result.returncode == 113 and error in {expected, "Bad request.\n" + expected}:
            return False
        _fail("服务状态不明确；拒绝把权限或其它错误当作未加载", BACKEND_UNAVAILABLE)


def _read_plan(path: Path, expected_sha256: str | None) -> dict:
    path = _absolute(path)
    _directory(path.parent)
    _file(path)
    if path.stat().st_mode & 0o077 or path.parent.stat().st_mode & 0o077:
        _fail("计划与备份必须保持 0700/0600 私有权限")
    data = path.read_bytes()
    if expected_sha256 is not None and _sha(data) != expected_sha256:
        _fail("计划哈希与已审阅版本不一致", REVISION_CONFLICT)
    plan = json.loads(data)
    if plan["schema_version"] != 1 or plan["uid"] != os.getuid():
        _fail("计划版本或用户不匹配")
    for name, item in plan["artifacts"].items():
        if Path(item["path"]) != path.parent / name:
            _fail("计划备份路径不匹配")
        _check_file(item)
    expected = _candidate_config(json.loads((path.parent / "config.before.json").read_bytes()), plan["request"], Path(plan["config_path"]))
    candidate, executable, _ = _candidate_plist(plistlib.loads((path.parent / "launchagent.before.plist").read_bytes()), Path(plan["config_path"]), Path(plan["state_dir"]), Path(plan["request"]["codex_bin"]))
    if _json(expected) != (path.parent / "config.candidate.json").read_bytes() or plistlib.dumps(candidate, sort_keys=True) != (path.parent / "launchagent.candidate.plist").read_bytes():
        _fail("候选内容不等于允许范围内的确定性变更")
    if plan["label"] != candidate["Label"] or plan["expected_applied_revision"] != expected["revision"] - 1 or plan["target_revision"] != expected["revision"]:
        _fail("计划 revision 或服务身份不一致")
    if {item["path"] for item in plan["sources"]} != {item["path"] for item in _runtime_sources(executable, Path(plan["request"]["codex_bin"]))}:
        _fail("运行源码文件集合已变化；请重新准备计划", REVISION_CONFLICT)
    return plan


def _check_baseline(plan: dict, conn: sqlite3.Connection) -> None:
    for item in [*plan["original"].values(), *plan["sources"]]:
        _check_file(item)
    if plan["current"] and plan["current"] != _current(Path(plan["current"]["path"])):
        _fail("current 链接已变化", REVISION_CONFLICT)
    current = _snapshot(conn)
    if current["blockers"]:
        _fail("存在在途、未决、UNKNOWN 或未完成 outbox；拒绝重启", INVALID_REQUEST, blockers=current["blockers"])
    if current != plan["state"]:
        _fail("状态或已应用 revision 在准备后变化；请重新准备计划", REVISION_CONFLICT)


def _safe_request(client, method: str, payload: dict) -> dict:
    try:
        result = client.handle(method, payload)
    except Exception:
        _fail("核心响应不明确；没有自动重试变更", BACKEND_UNAVAILABLE)
    if not result.ok:
        code = (result.error or {}).get("code")
        _fail("核心操作未确认成功；没有自动重试变更", REVISION_CONFLICT if code == REVISION_CONFLICT else BACKEND_UNAVAILABLE)
    return result.data


def _wait_ready(client, target: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            state = _safe_request(client, "config.validate", {})
            if state.get("config", {}).get("revision") == target:
                return
        except AsterunError:
            pass
        if time.monotonic() >= deadline:
            _fail("未等到目标 revision 的核心就绪；不会重派任务", BACKEND_UNAVAILABLE)
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))


@contextmanager
def _management_lock(state_dir: Path):
    # 与常驻核心的执行锁分开；SQLite 写锁用于关闭停止前的新提交窗口。
    _directory(state_dir)
    path = state_dir / "service-management.lock"
    if path.exists() or path.is_symlink():
        _file(path)
    lock = InstanceLock(path)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


def _record(folder: Path, name: str, result: dict, step: str | None = None) -> None:
    if step is not None:
        result["steps"].append(step)
    path = folder / name
    _replace(path, _json(result), _file(path))


def _acquire_stopped_lock(lock: InstanceLock) -> None:
    """bootout 可早于进程退出返回；仅在固定 10 秒窗口内等待执行锁。"""
    deadline = time.monotonic() + 10
    pending = None
    while True:
        if pending is not None and time.monotonic() >= deadline:
            raise pending
        # 等待期间也不跟随被替换的锁文件或接受其权限变化。
        if lock.path.exists() or lock.path.is_symlink():
            _file(lock.path)
        try:
            lock.acquire()
            return
        except AsterunError as error:
            if error.code != INSTANCE_LOCKED:
                raise
            pending = error
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise pending
        time.sleep(min(0.05, remaining))


def apply_service_plan(plan_path: Path, *, expected_plan_sha256: str | None = None,
                       verify_connection: bool = False, runner: Callable | None = None,
                       client_factory: Callable | None = None, ready_timeout: float = 10) -> dict:
    """调用本函数才变更服务。verify_connection 显式打开无 prompt 的账户握手。"""
    plan_path = _absolute(plan_path)
    plan = _read_plan(plan_path, expected_plan_sha256)
    state_dir, folder = Path(plan["state_dir"]), plan_path.parent
    if not 0 < ready_timeout <= 30:
        _fail("就绪超时必须在 0 到 30 秒之间")
    if (folder / "apply-result.json").exists():
        _fail("计划已有应用记录；请检查结果，不自动重试", REVISION_CONFLICT)
    run = runner or LaunchctlRunner()
    result = {"status": "in_progress", "steps": [], "model_called": False,
              "connection_verification": "not_requested", "changed_files": {}, "plan_sha256": _sha(plan_path.read_bytes())}
    db = state_dir / "asterun.sqlite"
    try:
        with _management_lock(state_dir):
            with _database(db, write_lock=True) as conn:
                _check_baseline(plan, conn)
                _write_new(folder / "apply-result.json", _json(result))
                _record(folder, "apply-result.json", result, "core_stop_requested")
                run(["bootout", f"gui/{plan['uid']}/{plan['label']}"])
                # bootout 成功仍须确认旧进程已经释放执行锁。
                if (state_dir / "asterun.lock").exists() or (state_dir / "asterun.lock").is_symlink():
                    _file(state_dir / "asterun.lock")
                stopped_lock = InstanceLock(state_dir / "asterun.lock")
                _acquire_stopped_lock(stopped_lock)
                try:
                    _record(folder, "apply-result.json", result, "core_stopped")
                    _check_baseline(plan, conn)
                    for name, artifact, key in (("config", "config.candidate.json", "config_path"),
                                                 ("plist", "launchagent.candidate.plist", "plist_path")):
                        _record(folder, "apply-result.json", result, f"{name}_write_requested")
                        result["changed_files"][name] = _replace(Path(plan[key]), (folder / artifact).read_bytes(), plan["original"][name])
                        _record(folder, "apply-result.json", result, f"{name}_written")
                finally:
                    stopped_lock.release()
            _record(folder, "apply-result.json", result, "core_start_requested")
            run(["bootstrap", f"gui/{plan['uid']}", plan["plist_path"]])
            _record(folder, "apply-result.json", result, "core_started")
            client = (client_factory or (lambda path: LocalClient(path, timeout=1)))(state_dir / "asterun.sock")
            try:
                _wait_ready(client, plan["target_revision"], ready_timeout)
                _record(folder, "apply-result.json", result, "target_revision_loaded")
                for item in result["changed_files"].values():
                    _check_file(item)
                _record(folder, "apply-result.json", result, "config_apply_requested")
                response = _safe_request(client, "config.apply", {"expected_revision": plan["expected_applied_revision"]})
                if response.get("applied_config_revision") != plan["target_revision"]:
                    _fail("CAS 未返回目标 revision", REVISION_CONFLICT)
                _record(folder, "apply-result.json", result, "config_applied")
                if verify_connection:
                    verified = _safe_request(client, "connection.verify", {"backend": "codex"})
                    result["connection_verification"] = "verified" if verified.get("verified") and verified.get("is_real_connection") else "not_verified"
                    if result["connection_verification"] != "verified":
                        _fail("服务已应用，但 Codex 连接尚未验证", BACKEND_UNAVAILABLE)
                    _record(folder, "apply-result.json", result, "connection_verified_without_prompt")
                result["status"] = "applied"
            finally:
                client.close()
    except Exception as error:
        # 不保存异常文本、环境、原始子进程输出或账户响应。
        result["status"] = "partial" if result["steps"] else "refused"
        result["error_code"] = error.code if isinstance(error, AsterunError) else BACKEND_UNAVAILABLE
        result["message"] = "未自动重试、重派或回退；检查步骤后使用原计划的安全回退入口。"
    if (folder / "apply-result.json").exists():
        try:
            with _database(db) as conn:
                result["state_after"] = _snapshot(conn)
        except Exception:
            result["state_after"] = None
        _record(folder, "apply-result.json", result)
    return result


def rollback_service_plan(plan_path: Path, *, expected_plan_sha256: str | None = None,
                          runner: Callable | None = None, job_probe: Callable | None = None) -> dict:
    """精确回退本计划的文件与 revision。保留运行数据且不启动旧服务。"""
    plan_path = _absolute(plan_path)
    plan = _read_plan(plan_path, expected_plan_sha256)
    folder, state_dir = plan_path.parent, Path(plan["state_dir"])
    applied = json.loads((folder / "apply-result.json").read_bytes())
    if applied.get("status") not in {"applied", "partial", "in_progress"}:
        _fail("没有可安全回退的应用状态；请人工检查步骤记录")
    if (folder / "rollback-result.json").exists():
        _fail("已有回退记录；不会重试", REVISION_CONFLICT)
    result = {"status": "in_progress", "steps": [], "model_called": False, "old_service_started": False}
    run = runner or LaunchctlRunner()
    probe = job_probe or getattr(run, "is_loaded", None)
    try:
        with _management_lock(state_dir), _database(state_dir / "asterun.sqlite", write_lock=True) as conn:
            snapshot = _snapshot(conn)
            interrupted = not applied.get("state_after") or applied.get("status") == "in_progress"
            if interrupted:
                # 崩溃后的唯一容许账本变化是本计划 CAS 的 revision；不猜测其它变化。
                normalized = _snapshot(conn, normalize_revision=plan["expected_applied_revision"])
                valid_state = normalized["sha256"] == plan["state"]["sha256"] and snapshot["applied_revision"] in {
                    plan["expected_applied_revision"], plan["target_revision"]}
                if snapshot["applied_revision"] == plan["target_revision"] and "config_apply_requested" not in applied["steps"]:
                    valid_state = False
            else:
                valid_state = snapshot == applied["state_after"]
            if not valid_state or snapshot["blockers"]:
                _fail("运行状态已变化或存在未决项；拒绝回退", REVISION_CONFLICT)
            # rename 完成但 journal 尚未落盘时，只接受有写前意图且精确匹配候选的文件。
            changed = {}
            for key, item in plan["original"].items():
                actual = _file(Path(item["path"]))
                recorded = applied.get("changed_files", {}).get(key)
                if recorded:
                    _check_file(recorded)
                    changed[key] = recorded
                elif actual == item:
                    continue
                else:
                    candidate_name = "config.candidate.json" if key == "config" else "launchagent.candidate.plist"
                    candidate = plan["artifacts"][candidate_name]
                    if f"{key}_write_requested" not in applied["steps"] or actual["sha256"] != candidate["sha256"] or actual["mode"] != 0o600:
                        _fail("中断后的文件不匹配原版本或已授权候选；拒绝覆盖", REVISION_CONFLICT, path=item["path"])
                    changed[key] = actual
            result["recovered_interrupted_apply"] = interrupted
            for item in plan["sources"]:
                _check_file(item)
            if plan["current"] and _current(Path(plan["current"]["path"])) != plan["current"]:
                _fail("current 已变化；拒绝回退", REVISION_CONFLICT)
            _write_new(folder / "rollback-result.json", _json(result))
            if "core_stop_requested" in applied["steps"]:
                if probe is None:
                    _fail("注入 runner 的回退需要可信 job_probe，不能猜测服务状态")
                target = f"gui/{plan['uid']}/{plan['label']}"
                loaded = probe(target)
                if type(loaded) is not bool:
                    _fail("服务状态不明确；拒绝自动回退", BACKEND_UNAVAILABLE)
                if loaded:
                    _record(folder, "rollback-result.json", result, "core_stop_requested")
                    run(["bootout", target])
                else:
                    _record(folder, "rollback-result.json", result, "core_already_unloaded")
            lock = InstanceLock(state_dir / "asterun.lock")
            if (state_dir / "asterun.lock").exists() or (state_dir / "asterun.lock").is_symlink():
                _file(state_dir / "asterun.lock")
            _acquire_stopped_lock(lock)
            try:
                if "core_stop_requested" in result["steps"]:
                    _record(folder, "rollback-result.json", result, "core_stopped")
                _record(folder, "rollback-result.json", result, "execution_lock_acquired")
                for name, artifact in (("config", "config.before.json"), ("plist", "launchagent.before.plist")):
                    if name in changed:
                        original = plan["original"][name]
                        _record(folder, "rollback-result.json", result, f"{name}_restore_requested")
                        _replace(Path(original["path"]), (folder / artifact).read_bytes(), changed[name], original["mode"])
                        _record(folder, "rollback-result.json", result, f"{name}_restored")
                old, current = plan["expected_applied_revision"], snapshot["applied_revision"]
                if current not in {old, plan["target_revision"]}:
                    _fail("已应用 revision 与本计划不匹配", REVISION_CONFLICT)
                conn.execute("UPDATE meta SET value=? WHERE key='applied_config_revision'", (str(old),))
                conn.commit()
                _record(folder, "rollback-result.json", result, "revision_restored")
            finally:
                lock.release()
            result["status"] = "rolled_back_service_stopped"
    except Exception as error:
        result["status"] = "partial" if result["steps"] else "refused"
        result["error_code"] = error.code if isinstance(error, AsterunError) else BACKEND_UNAVAILABLE
    if (folder / "rollback-result.json").exists():
        _record(folder, "rollback-result.json", result)
    return result
