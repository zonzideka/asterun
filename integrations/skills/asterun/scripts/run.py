#!/usr/bin/env python3
"""标准 skill 的一次性 CLI 调用器；权威状态始终由 Asterun 核心保存。

标准发行版需要 CLI 与常驻核心 >= 0.1.0a14；公开 a13 不兼容。
仍标为 a13 的历史源码构建仅在 CLI 与常驻接口探测都通过时兼容。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile

MAX_FILE_BYTES = 1024 * 1024
# CLI 会把 IPC 响应重新排版；私有完整对象可大于 IPC 的单行 JSON。
MAX_INSPECT_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_REPLY_BYTES = 12 * 1024
MIN_CORE_VERSION = "0.1.0a14"
PROBE_TIMEOUT = 3.0
MUTATIONS = {"submit", "evaluate", "repair", "cancel"}
BINDING = {"task_id": "task_id", "run_id": "expected_run_id", "revision": "expected_revision",
           "input_hash": "expected_input_hash", "target_hash": "expected_target_hash", "target_paths": "target_paths"}
_PRE_LETTER = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "c": 2, "rc": 2, "pre": 2, "preview": 2}
_PEP440 = re.compile(
    r"^(?:(?P<epoch>[0-9]+)!)?(?P<release>[0-9]+(?:\.[0-9]+)*)"
    r"(?:[-._]?(?P<pre_l>alpha|beta|preview|pre|rc|a|b|c)[-._]?(?P<pre_n>[0-9]+)?)?"
    r"(?:[-._]?post[-._]?(?P<post>[0-9]+))?(?:[-._]?dev[-._]?(?P<dev>[0-9]+))?"
    r"(?:\+(?P<local>[a-z0-9]+(?:[-._][a-z0-9]+)*))?$",
    re.IGNORECASE,
)
_PROBE_CACHE = {}


class InputError(ValueError):
    pass


class ResponseLimitError(InputError):
    pass


class GateError(Exception):
    def __init__(self, code, message, *, current=None, source=None, exit_code=2, next_action=""):
        self.code = code
        self.message = message
        self.current = current
        self.source = source
        self.exit_code = exit_code
        self.next_action = next_action


def parse_version(value):
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        from packaging.version import InvalidVersion, Version
        try:
            return Version(text)
        except InvalidVersion:
            return None
    except ImportError:
        return _parse_pep440(text)


def _parse_pep440(value):
    match = _PEP440.fullmatch(value)
    if not match:
        return None
    epoch = int(match["epoch"] or 0)
    release = tuple(int(part) for part in match["release"].split("."))
    while len(release) > 1 and release[-1] == 0:
        release = release[:-1]
    inf, ninf = 10 ** 12, -10 ** 12
    pre_l, pre_n, post, dev = match["pre_l"], match["pre_n"], match["post"], match["dev"]
    if pre_l:
        pre_key = (1, _PRE_LETTER[pre_l.lower()], int(pre_n or 0))
    elif post is None and dev is not None:
        pre_key = (0,)
    else:
        pre_key = (2,)
    post_key = ninf if post is None else int(post)
    dev_key = inf if dev is None else int(dev)
    local_key = inf if match["local"] else ninf
    return (epoch, release, pre_key, post_key, dev_key, local_key)


def version_supported(value, minimum=MIN_CORE_VERSION):
    # PEP 440：0.1.0a14+local 高于 a14；0.1.0a14.dev1 低于 a14，不视为已发布 a14。
    actual, needed = parse_version(value), parse_version(minimum)
    return actual is not None and needed is not None and actual >= needed


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError("JSON 存在重复字段")
        result[key] = value
    return result


def decode(raw):
    def invalid_constant(_):
        raise InputError("JSON 包含非有限数值")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
                          parse_constant=invalid_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise InputError("文件不是有效 UTF-8 JSON") from exc


def regular_bytes(path, limit=MAX_FILE_BYTES):
    path = Path(path).absolute()
    if path.is_symlink():
        raise InputError("输入文件不能是符号链接")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise InputError("输入必须是普通文件")
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise InputError("输入超过文件大小上限")
    return raw


def scoped_file(root, relative):
    root = root.resolve(strict=True)
    path = root
    for part in PurePosixPath(relative_file(relative)).parts:
        path = path / part
        if path.is_symlink():
            raise InputError("报告路径不能经过工作区内符号链接")
    return path


def private_write(path, raw):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def dump(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n").encode()


def identifier(value):
    if not isinstance(value, str) or not value or value.startswith("-") or len(value.encode()) > 1024:
        raise InputError("标识缺失或超限")
    return value


def relative_file(value):
    if not isinstance(value, str) or "\\" in value:
        raise InputError("需要工作区内的规范相对文件路径")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise InputError("需要工作区内的规范相对文件路径")
    return value


def snapshot_binding(path):
    envelope = decode(regular_bytes(path))
    if not isinstance(envelope, dict) or envelope.get("ok") is not True:
        raise InputError("需要成功的 workflow-snapshot 原始响应文件")
    data = envelope.get("data")
    if not isinstance(data, dict) or set(BINDING) - data.keys():
        raise InputError("快照缺少完整版本绑定")
    for name in ("task_id", "run_id", "input_hash", "target_hash"):
        identifier(data[name])
    if type(data["revision"]) is not int or data["revision"] < 1:
        raise InputError("快照 revision 无效")
    paths = data["target_paths"]
    if not isinstance(paths, list) or not 1 <= len(paths) <= 64 or len(set(paths)) != len(paths):
        raise InputError("快照目标文件无效")
    for target in paths:
        relative_file(target)
    return {destination: data[source] for source, destination in BINDING.items()}


def request_args(args, operation_dir):
    command = args.command
    if command == "discover":
        return ["backend-inspect"]
    if command == "resources":
        return ["control-resources"]
    if command == "submit":
        if args.input == "-":
            raise InputError("本技能不读取 stdin；--input 必须是已配置工作区内的提示文件")
        argv = ["task-submit", "--workspace=" + identifier(args.workspace),
                "--backend=" + identifier(args.backend), "--input=" + relative_file(args.input),
                "--idempotency-key=" + identifier(args.idempotency_key)]
        if args.conversation_id:
            argv.append("--conversation-id=" + identifier(args.conversation_id))
        return argv
    if command in {"status", "inspect", "wait", "snapshot", "cancel", "usage"}:
        task_id = identifier(args.task_id)
        if command == "usage":
            if not 1 <= args.page_size <= 100:
                raise InputError("用量明细每页条数必须为 1–100")
            argv = ["usage-report", "--task-id=" + task_id, "--page-size", str(args.page_size)]
            if args.include_runs:
                argv.append("--include-runs")
            if args.include_observations:
                argv.append("--include-observations")
            if args.cursor:
                argv.append("--cursor=" + identifier(args.cursor))
            return argv
        if command == "cancel":
            return ["task-cancel", task_id]
        if command in {"status", "inspect"}:
            return ["task-get", task_id, *(["--compact"] if command == "status" else [])]
        if command == "snapshot":
            return ["workflow-snapshot", task_id, "--run-id=" + identifier(args.run_id),
                    *["--target=" + relative_file(path) for path in args.target]]
        if not math.isfinite(args.timeout) or not 0 < args.timeout <= 55:
            raise InputError("等待时限必须大于零且不超过 55 秒")
        if args.cursor < 0 or args.cursor and not args.run_id:
            raise InputError("非零游标必须指定所属运行")
        argv = ["task-watch", task_id, "--compact", "--no-events", "--timeout", str(args.timeout),
                "--cursor", str(args.cursor)]
        if args.run_id:
            argv.append("--run-id=" + identifier(args.run_id))
        return argv
    binding = snapshot_binding(args.snapshot_file)
    request = dict(binding)
    if command == "evaluate":
        report_path = relative_file(args.report)
        raw = regular_bytes(scoped_file(args.workspace_root, report_path), 256 * 1024)
        report = decode(raw)
        expected = {"task_id": binding["task_id"], "run_id": binding["expected_run_id"],
                    "input_hash": binding["expected_input_hash"], "target_hash": binding["expected_target_hash"]}
        if not isinstance(report, dict) or any(report.get(key) != value for key, value in expected.items()):
            raise InputError("真实检查报告与快照绑定不一致")
        request["checks"] = [{"kind": "external_report", "path": report_path,
                               "sha256": hashlib.sha256(raw).hexdigest()}]
        if args.require_review:
            request["require_review"] = True
    else:
        if not 1 <= args.max_repairs <= 100:
            raise InputError("需要显式指定 1–100 的任务累计修复上限")
        try:
            instructions = regular_bytes(args.instructions_file, 256 * 1024).decode("utf-8")
        except UnicodeError as exc:
            raise InputError("修复指令必须是 UTF-8 文本") from exc
        if not instructions.strip():
            raise InputError("修复指令不能为空")
        request.update(instructions=instructions, max_repairs=args.max_repairs,
                       idempotency_key=identifier(args.idempotency_key))
    request_file = operation_dir / "request.json"
    private_write(request_file, dump(request))
    return ["workflow-" + command, binding["task_id"], "--request", str(request_file)]


def small_fields(value, names):
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in names if key in value and
            (value[key] is None or type(value[key]) in {bool, int} or
             isinstance(value[key], str) and len(value[key].encode()) <= 1024)}


def summarize(envelope, command):
    data = envelope.get("data") or {}
    if not isinstance(data, dict):
        raise InputError("响应 data 必须是对象")
    snapshot = data.get("last_snapshot") if command == "wait" else data
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    result = {"ok": envelope["ok"], "ids": small_fields(envelope.get("ids"),
              ("task_id", "run_id", "conversation_id", "approval_id", "operation_id")),
              "error": small_fields(envelope.get("error"), ("code",)),
              "task": small_fields(snapshot.get("task"), ("id", "acceptance", "review_status", "evidence_stale",
                      "paused", "run_count", "repair_count", "external_require_review", "orchestration_owner")),
              "run": small_fields(snapshot.get("run"), ("id", "status", "cancel_requested", "terminated", "error_code")),
              "approval": small_fields(snapshot.get("approval"), ("id", "task_id", "run_id", "state", "target_hash"))}
    result.update(small_fields(data, ("reason", "run_id", "cursor", "requests", "acceptance", "review_status",
                                     "evidence_stale", "cancel_requested", "terminated")))
    native = (snapshot.get("run") or {}).get("native") if isinstance(snapshot.get("run"), dict) else {}
    result["native"] = small_fields(native, ("session_id", "thread_id", "turn_id", "backend_session_id",
                                            "backend_turn_id", "native_resumed", "usage_scope"))
    if command == "inspect":
        result["response_compact"] = data.get("compact") is True
    if command == "snapshot":
        result["binding"] = small_fields(data, ("task_id", "run_id", "revision", "input_hash", "target_hash"))
        result["target_count"] = len(data.get("target_paths", []))
    if command == "discover":
        backends = data.get("backends", {})
        if isinstance(backends, dict):
            result["backends"] = [small_fields({"name": name, **row}, ("name", "kind", "enabled"))
                                  for name, row in list(backends.items())[:32] if isinstance(row, dict)]
        elif isinstance(backends, list):
            result["backends"] = [small_fields(row, ("id", "backend", "name", "kind", "enabled",
                                  "enabled_in_config", "execution_enabled", "is_real_connection",
                                  "execution_profile", "transport", "model", "binary_found")) for row in backends[:32]]
        if isinstance(backends, (dict, list)):
            result["backend_count"] = len(backends)
            result["backends_truncated"] = len(backends) > 32
    if command == "resources":
        resources = data.get("resources", [])
        if not isinstance(resources, list):
            raise InputError("资源响应必须包含数组")
        result["resources"] = [small_fields(row, ("resource_id", "workspace", "location", "enforcement"))
                               for row in resources[:32]]
        result["resource_count"] = len(resources)
        result["resources_truncated"] = len(resources) > 32
    if command == "usage":
        coverage = small_fields(data.get("coverage"), ("controller", "native_requests", "runs",
                    "subscription_quota_effect", "session_observations_added_to_totals"))
        coverage.update(small_fields(data, ("source", "readonly", "task_status_source", "acceptance_revalidated")))
        coverage.update(small_fields(data.get("totals"), ("run_count", "task_count")))
        coverage["run_details_included"] = isinstance(data.get("runs"), list)
        coverage["session_observations_included"] = isinstance(data.get("observations"), list)
        result["usage"] = {"totals": data.get("totals"), "coverage": coverage,
                           "scope": small_fields(data.get("scope"), ("task_id", "workspace", "conversation_id")),
                           "pagination": small_fields(data.get("pagination"),
                               ("page_size", "snapshot", "next_cursor", "has_more", "totals_scope")),
                           "billed_usage_verified": data.get("billed_usage_verified"),
                           "subscription_quota_effect": "unknown"}
    if isinstance(data.get("checks"), list):
        result["check_count"] = len(data["checks"])
        result["check_sources"] = sorted({row["source"] for row in data["checks"]
            if isinstance(row, dict) and isinstance(row.get("source"), str) and len(row["source"]) <= 64})[:16]
    result["next_action"] = next_action(result, command)
    return result


def next_action(result, command):
    if not result["ok"]:
        return "读取保存的响应核对原请求；结果不明时对账，不换键重派"
    status = result["run"].get("status")
    reason = result.get("reason")
    if result["task"].get("paused"):
        return "核对暂停与审查要求，由主控决定是否恢复原任务"
    if result["approval"].get("state") in {"pending", "forwarding"} or reason == "waiting_input" or status == "waiting_input":
        return "读取完整审批与授权范围，由主控处理原审批接口"
    if status in {"pending_reconcile", "paused", "waiting_auth"} or reason in {"unknown", "paused", "waiting_auth"}:
        return "核对原运行、认证或暂停原因后再决定后续步骤"
    if status is not None and status not in {"queued", "dispatching", "running", "succeeded", "failed", "cancelled"}:
        return "运行状态无法识别；读取完整证据并核对原运行，停止自动推进"
    if status in {"queued", "dispatching", "running"} or reason == "deadline":
        return "复用 task_id、run_id 和 cursor 继续等待；观察到期不取消任务"
    if status == "failed":
        return "按需读取失败证据；确认缺陷后取得新快照并在原任务上有限修复"
    if status == "cancelled":
        return "核对 terminated 与取消回执，保留原任务证据"
    if command == "cancel":
        return "继续观察并核对原生终止事实；受理取消不等于终止"
    if command == "discover":
        return "用 resources 核对工作区，并按执行配置选择后端；fake 只模拟契约，不证明实际产出"
    if command == "resources":
        return "按 workspace 与 location 核对当前项目；只使用已授权工作区，不自动创建或扩大范围"
    if command == "snapshot":
        return "在该固定版本与所选范围上执行已授权检查并生成真实报告"
    if command == "usage":
        return "按需读取用量证据，累计观察和未知计费不能当作实际节省"
    if result.get("acceptance", result["task"].get("acceptance")) == "passed":
        return "验收仅适用于所绑定版本与检查范围；按用户目标交付"
    return "运行结束后读取所需产物，并在固定版本上完成真实验收"


def operation_directory(args):
    root = args.artifacts_dir.absolute()
    if root.is_symlink():
        raise InputError("证据目录不能是符号链接")
    root = root.parent.resolve() / root.name
    state = args.state_dir.resolve()
    if root == state or state in root.parents:
        raise InputError("派生调用记录必须放在核心状态目录之外")
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = root.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise InputError("证据目录必须由当前用户持有且不能被其他用户写入")
    return Path(tempfile.mkdtemp(prefix=args.command + "-", dir=root))


def run_cli(argv, stdout_path, stderr_path, timeout):
    timed_out = False
    interrupted = False
    # 只终止调用客户端；--connect 后的后台任务由已有核心继续管理。
    with os.fdopen(os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out, \
         os.fdopen(os.open(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as err:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err)
        try:
            code = process.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            timed_out = isinstance(exc, subprocess.TimeoutExpired)
            interrupted = not timed_out
            process.kill()
            process.wait()
            code = 130 if interrupted else 124
    return code, timed_out, interrupted


def envelope_version(path):
    try:
        envelope = decode(regular_bytes(path))
    except (InputError, OSError):
        return None
    data = envelope.get("data") if isinstance(envelope, dict) and envelope.get("ok") is True else None
    version = data.get("version") if isinstance(data, dict) else None
    return version if isinstance(version, str) and version else None


def clear_probe_cache():
    # 保留调用兼容；常驻核心可能在两次调用间切换，不能缓存版本或能力。
    _PROBE_CACHE.clear()


def unsupported_message(current, source):
    return (f"标准 skill 需要 Asterun 核心 {MIN_CORE_VERSION} 或更新；当前{source}为 {current}。"
            "已发布的 0.1.0a13 不含 task-watch --compact/--no-events 与 workflow-snapshot。"
            "请安装 0.1.0a14 或当前源码构建，重启 asterun serve，再用 asterun version 与 "
            "asterun diagnose 核对应答版本后重试。")


def gate_error(code, message, *, current=None, source=None, exit_code=2, next_action=""):
    return GateError(code, message, current=current, source=source, exit_code=exit_code,
                     next_action=next_action)


def probe_cli_features(args, directory, binary, timeout, current_version):
    for command, options in (("task-get", ("--compact",)),
                             ("task-watch", ("--compact", "--no-events")),
                             ("workflow-snapshot", ())):
        path = directory / ("probe-" + command + "-help.txt")
        code, timed_out, interrupted = run_cli(
            [binary, command, "--help"], path,
            directory / ("probe-" + command + "-help-stderr.txt"), timeout)
        if timed_out or interrupted:
            raise gate_error("CLIENT_TIMEOUT" if timed_out else "CLIENT_INTERRUPTED",
                             "CLI 能力探测未完成；业务命令尚未发出", source="CLI",
                             exit_code=124 if timed_out else 130,
                             next_action="核对保存的 help 探测记录后重试；不要当作核心过旧去升级")
        try:
            help_text = regular_bytes(path).decode("utf-8")
        except (InputError, OSError, UnicodeError):
            raise gate_error("INVALID_CLI_RESPONSE", "无法读取 CLI 能力探测响应", source="CLI",
                             next_action="核对保存的 help 探测记录；业务命令尚未发出")
        if code != 0 or not help_text.strip() or any(
                re.search(r"(?<![\w-])" + re.escape(option) + r"(?![\w-])", help_text) is None
                for option in options):
            raise gate_error("CORE_VERSION_UNSUPPORTED",
                             "当前 CLI 缺少标准 skill 所需的 " + command + " 能力；业务命令尚未发出",
                             current=current_version, source="CLI",
                             next_action="安装 0.1.0a14 或更新的 CLI 与核心；历史自编 a13 必须通过全部能力探测")


def probe_core_features(args, directory, binary, timeout):
    import uuid

    task_id = "tsk_skill_probe_" + uuid.uuid4().hex
    private_write(directory / "probe-binding.json", dump({"task_id": task_id,
                  "purpose": "compatibility_only", "creates_task": False}))
    for command in (["task-get", task_id, "--compact"],
                    ["workflow-snapshot", task_id, "--run-id", "run_skill_probe",
                     "--target", ".asterun-skill-probe"]):
        path = directory / ("probe-" + command[0] + ".json")
        code, timed_out, interrupted = run_cli(
            [binary, "--state-dir", str(args.state_dir.absolute()), "--connect", *command],
            path, directory / ("probe-" + command[0] + "-stderr.txt"), timeout)
        if timed_out or interrupted:
            raise gate_error("CORE_UNREACHABLE" if timed_out else "CLIENT_INTERRUPTED",
                             "常驻核心能力探测未完成；业务命令尚未发出", source="常驻核心",
                             exit_code=124 if timed_out else 130,
                             next_action="核对现有实例与保存的探测记录后重试；不要当作核心过旧去升级")
        try:
            envelope = decode(regular_bytes(path))
        except (InputError, OSError):
            envelope = None
        error = envelope.get("error") if isinstance(envelope, dict) else None
        error = error if isinstance(error, dict) else {}
        if (isinstance(envelope, dict) and envelope.get("ok") is False and code == 1
                and error.get("code") == "NOT_FOUND" and isinstance(error.get("details"), dict)
                and error["details"].get("id") == task_id):
            continue
        if error.get("code") == "BACKEND_UNAVAILABLE":
            raise gate_error("CORE_UNREACHABLE", "未取得常驻核心的能力探测应答", source="常驻核心",
                             next_action="确认该 state-dir 的已有核心可达后重试；业务命令尚未发出")
        if error.get("code") in {"INVALID_REQUEST", "UNKNOWN_FIELD", "METHOD_NOT_FOUND"}:
            raise gate_error("CORE_VERSION_UNSUPPORTED",
                             "当前核心不接受标准 skill 所需的 " + command[0] + " 请求；业务命令尚未发出",
                             current="0.1.0a13", source="常驻核心",
                             next_action="将 CLI 与常驻核心升级至 0.1.0a14 或更新；不回退到完整事件轮询")
        raise gate_error("CORE_CAPABILITY_UNKNOWN",
                         "核心能力探测未返回绑定该探测任务的 NOT_FOUND；业务命令尚未发出",
                         source="常驻核心", next_action="读取保存的探测响应核对原因；不要跳过门禁或重派业务任务")


def probe_core_versions(args, directory, binary):
    # 只读与变更都要求核验常驻核心版本；核验失败不把业务命令发给可能过旧的实例。
    timeout = min(PROBE_TIMEOUT, args.cli_timeout)
    version_path, version_err = directory / "version.json", directory / "version-stderr.txt"
    code, timed_out, interrupted = run_cli([binary, "version"], version_path, version_err, timeout)
    if timed_out:
        raise gate_error("CLIENT_TIMEOUT", "asterun version 在探测时限内未返回", source="CLI",
                         exit_code=124, next_action="检查 CLI 是否可执行后重试；这不是核心过旧")
    if interrupted:
        raise gate_error("CLIENT_INTERRUPTED", "asterun version 探测被中断", source="CLI",
                         exit_code=130, next_action="核对已保存探测记录后重试；变更命令尚未发出")
    cli_version = envelope_version(version_path) if code == 0 else None
    if cli_version is None or parse_version(cli_version) is None:
        raise gate_error("INVALID_CLI_RESPONSE", "无法从 asterun version 读取版本", source="CLI",
                         exit_code=1, next_action="读取 version 探测的 stdout/stderr 后重试；不要当作核心过旧去升级")
    cli_legacy = cli_version == "0.1.0a13"
    if cli_legacy:
        probe_cli_features(args, directory, binary, timeout, cli_version)
    elif not version_supported(cli_version):
        raise gate_error("CORE_VERSION_UNSUPPORTED", unsupported_message(cli_version, "CLI"),
                         current=cli_version, source="CLI",
                         next_action="升级已安装 CLI 与常驻核心到 0.1.0a14 或更新并重启 serve；"
                                     "用 asterun version 与 asterun diagnose 核对后再重试，不要改用完整事件轮询绕过")
    diagnose_path, diagnose_err = directory / "diagnose.json", directory / "diagnose-stderr.txt"
    code, timed_out, interrupted = run_cli(
        [binary, "--state-dir", str(args.state_dir.absolute()), "--connect", "diagnose"],
        diagnose_path, diagnose_err, timeout)
    if timed_out:
        raise gate_error("CORE_UNREACHABLE",
                         "asterun diagnose 在探测时限内未返回；变更与只读命令均未发出",
                         source="常驻核心", exit_code=124,
                         next_action="确认该 state-dir 上 asterun serve 正在运行后重试；不要把探测超时当成核心过旧去升级")
    if interrupted:
        raise gate_error("CLIENT_INTERRUPTED", "asterun diagnose 探测被中断", source="常驻核心",
                         exit_code=130, next_action="核对已保存探测记录后重试；变更命令尚未发出")
    if code != 0:
        raise gate_error("CORE_UNREACHABLE",
                         "asterun diagnose 无法核对应答核心；变更与只读命令均未发出",
                         source="常驻核心",
                         next_action="确认该 state-dir 上 asterun serve 正在运行后重试；不要把探测失败当成核心过旧去升级")
    core_version = envelope_version(diagnose_path)
    if core_version is None or parse_version(core_version) is None:
        raise gate_error("CORE_VERSION_UNKNOWN",
                         "应答核心的 diagnose 未返回可解析版本；变更与只读命令均未发出",
                         source="常驻核心",
                         next_action="运行 asterun --connect diagnose 核对后再重试；不要把缺版本当成核心过旧去升级")
    core_legacy = core_version == "0.1.0a13"
    if core_legacy:
        if not cli_legacy:
            probe_cli_features(args, directory, binary, timeout, cli_version)
        probe_core_features(args, directory, binary, timeout)
    elif not version_supported(core_version):
        raise gate_error("CORE_VERSION_UNSUPPORTED", unsupported_message(core_version, "常驻核心"),
                         current=core_version, source="常驻核心",
                         next_action="升级已安装 CLI 与常驻核心到 0.1.0a14 或更新并重启 serve；"
                                     "用 asterun version 与 asterun diagnose 核对后再重试，不要改用完整事件轮询绕过")
    proven = (cli_version, core_version)
    private_write(directory / "compatibility.json", dump({"cli_version": cli_version,
                  "core_version": core_version, "minimum_release_version": MIN_CORE_VERSION,
                  "mode": "historical_a13_capabilities" if cli_legacy or core_legacy else "release_version",
                  "cached": False}))
    return proven


def gate_result(exc, args, directory):
    result = {"ok": False, "error": {"code": exc.code, "message": exc.message},
              "next_action": exc.next_action, "checked": exc.source}
    if exc.current is not None:
        result["current_version"] = exc.current
        result["minimum_version"] = MIN_CORE_VERSION
    artifacts = {"directory": str(directory)}
    for key, name in (("version", "version.json"), ("version_stderr", "version-stderr.txt"),
                      ("diagnose", "diagnose.json"), ("diagnose_stderr", "diagnose-stderr.txt")):
        path = directory / name
        if path.is_file():
            artifacts[key] = str(path)
    result.update(schema_version="asterun-skill-result/v1", command=args.command,
                  cli_exit_code=exc.exit_code, artifacts=artifacts)
    private_write(directory / "result.json", dump(result))
    return result, exc.exit_code


def execute(args):
    if not math.isfinite(args.cli_timeout) or not 0 < args.cli_timeout <= 60:
        raise InputError("CLI 时限必须大于零且不超过 60 秒")
    binary = shutil.which(args.asterun_bin)
    if binary is None:
        raise InputError("找不到已安装的 Asterun CLI")
    directory = operation_directory(args)
    try:
        probe_core_versions(args, directory, binary)
    except GateError as exc:
        return gate_result(exc, args, directory)
    argv = [binary, "--state-dir", str(args.state_dir.absolute()), "--connect",
            *request_args(args, directory)]
    timeout = max(args.cli_timeout, args.timeout + 5) if args.command == "wait" else args.cli_timeout
    private_write(directory / "intent.json", dump({"schema_version": "asterun-skill-call/v1",
                  "command": args.command, "argv": argv, "mutation": args.command in MUTATIONS,
                  "authoritative_state": "asterun_core", "automatic_retries": 0,
                  "minimum_core_version": MIN_CORE_VERSION}))
    response_path, stderr_path = directory / "response.json", directory / "stderr.txt"
    code, timed_out, interrupted = run_cli(argv, response_path, stderr_path, timeout)
    artifacts = {"directory": str(directory), "response": str(response_path), "stderr": str(stderr_path),
                 "intent": str(directory / "intent.json")}
    try:
        if timed_out or interrupted:
            raise InputError("客户端执行未确认")
        response_limit = MAX_INSPECT_RESPONSE_BYTES if args.command == "inspect" else MAX_FILE_BYTES
        if response_path.stat().st_size > response_limit:
            raise ResponseLimitError("CLI 响应超过本命令的解析上限")
        envelope = decode(regular_bytes(response_path, response_limit))
        if not isinstance(envelope, dict) or type(envelope.get("ok")) is not bool:
            raise InputError("响应缺少有效信封")
        result = summarize(envelope, args.command)
        if envelope["ok"] and code != 0 and not (args.command == "wait" and code == 124 and
                                                  result.get("reason") == "deadline"):
            raise InputError("CLI 退出码与响应不一致")
        if not envelope["ok"] and code == 0:
            code = 1
    except ResponseLimitError:
        result = {"ok": False, "error": {"code": "CLI_RESPONSE_TOO_LARGE", "limit_bytes": response_limit},
                  "outcome": "unknown" if args.command in MUTATIONS else "unobserved",
                  "next_action": "完整原始响应已保存在私有文件；按需局部读取证据，不重派任务"}
        code = 1
    except (InputError, OSError, TypeError, KeyError, ValueError):
        result = {"ok": False, "error": {"code": "CLIENT_INTERRUPTED" if interrupted else
                  "CLIENT_TIMEOUT" if timed_out else "INVALID_CLI_RESPONSE"},
                  "outcome": "unknown" if args.command in MUTATIONS else "unobserved",
                  "next_action": "读取已保存的原请求与响应，核对原任务；不换键重派"}
        code = 130 if interrupted else 124 if timed_out else 1
    compatibility_path = directory / "compatibility.json"
    if compatibility_path.is_file():
        artifacts["compatibility"] = str(compatibility_path)
        result["compatibility"] = decode(regular_bytes(compatibility_path))
    result.update(schema_version="asterun-skill-result/v1", command=args.command, cli_exit_code=code,
                  artifacts=artifacts, metrics={"response_bytes": response_path.stat().st_size})
    raw = dump(result)
    if len(raw) > MAX_REPLY_BYTES:
        for name in ("usage", "backends", "resources", "native", "check_sources"):
            if name in result:
                result.pop(name)
                result.setdefault("omitted_fields", []).append(name)
    if len(dump(result)) > MAX_REPLY_BYTES:
        result = {"schema_version": "asterun-skill-result/v1", "ok": False,
                  "error": {"code": "SUMMARY_LIMIT"}, "artifacts": {"directory": str(directory)},
                  "next_action": "摘要超过上限，请按需读取证据文件"}
        code = 1
    private_write(directory / "result.json", dump(result))
    return result, code


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--asterun-bin", default="asterun")
    root.add_argument("--state-dir", type=Path, required=True)
    root.add_argument("--artifacts-dir", type=Path, required=True)
    root.add_argument("--cli-timeout", type=float, default=20)
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("discover", help="只读发现实际后端名称")
    commands.add_parser("resources", help="只读发现已授权工作区名称与根路径")
    submit = commands.add_parser("submit", help="提交一次；必须提供稳定幂等键")
    for name in ("workspace", "backend", "input", "idempotency-key"):
        submit.add_argument("--" + name, required=True)
    submit.add_argument("--conversation-id")
    for name in ("status", "inspect", "wait", "snapshot", "cancel", "usage"):
        command = commands.add_parser(name)
        command.add_argument("--task-id", required=True)
        if name == "wait":
            command.add_argument("--timeout", type=float, default=30)
            command.add_argument("--run-id")
            command.add_argument("--cursor", type=int, default=0)
        if name == "snapshot":
            command.add_argument("--run-id", required=True)
            command.add_argument("--target", action="append", required=True)
        if name == "usage":
            command.add_argument("--page-size", type=int, default=1)
            command.add_argument("--cursor")
            command.add_argument("--include-runs", action="store_true")
            command.add_argument("--include-observations", action="store_true")
    for name in ("evaluate", "repair"):
        command = commands.add_parser(name)
        command.add_argument("--snapshot-file", type=Path, required=True)
        if name == "evaluate":
            command.add_argument("--workspace-root", type=Path, required=True)
            command.add_argument("--report", required=True)
            command.add_argument("--require-review", action="store_true")
        else:
            command.add_argument("--instructions-file", type=Path, required=True)
            command.add_argument("--max-repairs", type=int, required=True)
            command.add_argument("--idempotency-key", required=True)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result, code = execute(args)
    except (InputError, OSError, ValueError, TypeError) as exc:
        # 不向聊天回显可能含账户或原生内容的异常文本。
        result, code = {"ok": False, "error": {"code": "SKILL_INPUT_INVALID",
                        "message": str(exc) if isinstance(exc, InputError) else "无法读取输入或调用 CLI"}}, 2
    print(dump(result).decode(), end="")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
