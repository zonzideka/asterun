#!/usr/bin/env python3
"""标准 skill 的一次性 CLI 调用器；权威状态始终由 Asterun 核心保存。"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tempfile

MAX_FILE_BYTES = 1024 * 1024
MAX_REPLY_BYTES = 12 * 1024
MUTATIONS = {"submit", "evaluate", "repair", "cancel"}
BINDING = {"task_id": "task_id", "run_id": "expected_run_id", "revision": "expected_revision",
           "input_hash": "expected_input_hash", "target_hash": "expected_target_hash", "target_paths": "target_paths"}


class InputError(ValueError):
    pass


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
    if command == "submit":
        argv = ["task-submit", "--workspace=" + identifier(args.workspace),
                "--backend=" + identifier(args.backend), "--input=" + relative_file(args.input),
                "--idempotency-key=" + identifier(args.idempotency_key)]
        if args.conversation_id:
            argv.append("--conversation-id=" + identifier(args.conversation_id))
        return argv
    if command in {"status", "inspect", "wait", "snapshot", "cancel", "usage"}:
        task_id = identifier(args.task_id)
        if command == "usage":
            return ["usage-report", "--task-id=" + task_id, "--page-size", "1"]
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
                                  "enabled_in_config", "execution_enabled", "is_real_connection")) for row in backends[:32]]
    if command == "usage":
        result["usage"] = {"totals": data.get("totals"), "coverage": data.get("coverage"),
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
        return "根据用户授权选择实际后端名称与工作区"
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


def execute(args):
    if not math.isfinite(args.cli_timeout) or not 0 < args.cli_timeout <= 60:
        raise InputError("CLI 时限必须大于零且不超过 60 秒")
    binary = shutil.which(args.asterun_bin)
    if binary is None:
        raise InputError("找不到已安装的 Asterun CLI")
    directory = operation_directory(args)
    argv = [binary, "--state-dir", str(args.state_dir.absolute()), "--connect",
            *request_args(args, directory)]
    timeout = max(args.cli_timeout, args.timeout + 5) if args.command == "wait" else args.cli_timeout
    private_write(directory / "intent.json", dump({"schema_version": "asterun-skill-call/v1",
                  "command": args.command, "argv": argv, "mutation": args.command in MUTATIONS,
                  "authoritative_state": "asterun_core", "automatic_retries": 0}))
    response_path, stderr_path = directory / "response.json", directory / "stderr.txt"
    timed_out = False
    interrupted = False
    # 只终止调用客户端；--connect 后的后台任务由已有核心继续管理。
    with os.fdopen(os.open(response_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out, \
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
    artifacts = {"directory": str(directory), "response": str(response_path), "stderr": str(stderr_path),
                 "intent": str(directory / "intent.json")}
    try:
        if timed_out or interrupted:
            raise InputError("客户端执行未确认")
        envelope = decode(regular_bytes(response_path))
        if not isinstance(envelope, dict) or type(envelope.get("ok")) is not bool:
            raise InputError("响应缺少有效信封")
        result = summarize(envelope, args.command)
        if envelope["ok"] and code != 0 and not (args.command == "wait" and code == 124 and
                                                  result.get("reason") == "deadline"):
            raise InputError("CLI 退出码与响应不一致")
        if not envelope["ok"] and code == 0:
            code = 1
    except (InputError, OSError, TypeError, KeyError, ValueError):
        result = {"ok": False, "error": {"code": "CLIENT_INTERRUPTED" if interrupted else
                  "CLIENT_TIMEOUT" if timed_out else "INVALID_CLI_RESPONSE"},
                  "outcome": "unknown" if args.command in MUTATIONS else "unobserved",
                  "next_action": "读取已保存的原请求与响应，核对原任务；不换键重派"}
        code = 130 if interrupted else 124 if timed_out else 1
    result.update(schema_version="asterun-skill-result/v1", command=args.command, cli_exit_code=code,
                  artifacts=artifacts, metrics={"response_bytes": response_path.stat().st_size})
    raw = dump(result)
    if len(raw) > MAX_REPLY_BYTES:
        for name in ("usage", "backends", "native", "check_sources"):
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
