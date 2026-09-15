from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from asterun import __version__
from asterun.application import Application
from asterun.contracts import Envelope
from asterun.errors import (
    AUTH_REQUIRED,
    CONFIG_REQUIRED,
    INVALID_CONFIG,
    INVALID_REQUEST,
    PATH_INVALID,
    PATH_OUT_OF_SCOPE,
    SCOPE_DENIED,
    UNKNOWN_FIELD,
    UNKNOWN_PROJECT,
    AsterunError,
)
from asterun.home import default_config_path, default_state_dir

CONFIG_EXIT = {CONFIG_REQUIRED, INVALID_CONFIG, UNKNOWN_FIELD, UNKNOWN_PROJECT, PATH_INVALID, PATH_OUT_OF_SCOPE}
AUTH_EXIT = {AUTH_REQUIRED, SCOPE_DENIED}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="asterun",
        description="Asterun CLI。配置、任务和状态路径必须显式或来自隔离的 ASTERUN_HOME，不会扫描全局账户目录。",
    )
    parser.add_argument("--config", type=Path, help="config.json 路径")
    parser.add_argument("--state-dir", type=Path, help="状态目录（SQLite 与单实例锁）")
    parser.add_argument("--home", type=Path, help="覆盖 ASTERUN_HOME")
    parser.add_argument("--connect", action="store_true", help="连接状态目录内的常驻核心，提交后快速返回")
    sub = parser.add_subparsers(dest="command", required=True)
    from asterun.operations_cli import add_parsers
    add_parsers(sub)
    sub.add_parser("plugin-list", help="列出静态注册信息，不启动插件")
    for command in ("plugin-inspect", "plugin-enable", "plugin-disable"):
        item = sub.add_parser(command, help="检查插件或设置当前实例持久启停闩")
        item.add_argument("plugin_id")
    register = sub.add_parser("plugin-register", help="注册为全新 v2 候选文件，保留原配置；参数用 JSON 文件提供")
    register.add_argument("--request", type=Path, required=True)
    setup = sub.add_parser("connection-setup", help="显示显式账户/认证引用引导，不执行登录")
    setup.add_argument("connection_ref")
    usage = sub.add_parser("usage-inspect", help="读取本地共享池账本；不代表供应商最终账单")
    usage.add_argument("--pool-ref")
    usage.add_argument("--meter")
    usage.add_argument("--window-id")
    report = sub.add_parser("usage-report", help="汇总指定工作区、会话或任务的全部运行用量；不是账单")
    report.add_argument("--workspace")
    report.add_argument("--task-id")
    report.add_argument("--conversation-id")
    report.add_argument("--include-runs", action="store_true", help="附各运行的原生用量明细")
    report.add_argument("--page-size", type=int, help="每类明细每页最多 1–100 条，默认 20；总计覆盖完整查询")
    report.add_argument("--cursor", help="沿用上页 pagination.next_cursor 和相同查询范围")
    invoke = sub.add_parser("capability-invoke", help="以结构化输入调用已注册能力，复用持久任务/预留")
    invoke.add_argument("--request", type=Path, required=True)
    sub.add_parser("capability-discovery", help="发现独立能力 profile，不启动插件")
    for name in ("capability-prepare", "capability-submit", "capability-get"):
        command = sub.add_parser(name, help="固定计划准备、标准事务受理或质量状态读取")
        command.add_argument("--request", type=Path, required=True)

    sub.add_parser("version", help="显示版本")
    sub.add_parser("control-discovery", help="发现 runner-control/v1 草案修订、摘要与已支持命令")
    sub.add_parser("control-resources", help="读取已授权资源、配置摘要和工作流/验证器")
    control_command = sub.add_parser("control-command", help="提交标准命令信封")
    control_command.add_argument("--request", type=Path, required=True)
    control_get = sub.add_parser("control-get", help="读取标准实体快照")
    control_get.add_argument("kind")
    control_get.add_argument("id")
    control_events = sub.add_parser("control-events", help="按 Task 序号读取标准持久事件")
    control_events.add_argument("task_id")
    control_events.add_argument("--after", type=int, default=0)
    control_events.add_argument("--limit", type=int, default=100)
    control_upload = sub.add_parser("control-upload", help="暂存文本产物内容，不赋予任意写路径")
    control_upload.add_argument("task_id")
    control_upload.add_argument("--request", type=Path, required=True, help="含 text 的 JSON 文件")
    control_content = sub.add_parser("control-content", help="读取已授权不可变文本产物")
    control_content.add_argument("artifact_id")
    sub.add_parser("config-validate", help="校验配置")
    migrate = sub.add_parser("config-migrate", help="只读生成 v2 候选；显式指定 output/backup/摘要才写新文件")
    migrate.add_argument("--output", type=Path)
    migrate.add_argument("--backup", type=Path)
    migrate.add_argument("--expected-source-sha256")
    apply = sub.add_parser("config-apply", help="把当前配置 revision CAS 应用到状态库，不改写配置文件")
    revisions = apply.add_mutually_exclusive_group(required=True)
    revisions.add_argument("--expected-applied-revision", type=int,
                           help="当前已应用 revision；例如已应用 1、候选文件 2 时传 1")
    revisions.add_argument("--revision", type=int,
                           help="--expected-applied-revision 的兼容旧名；不可同时指定")
    inspect = sub.add_parser("backend-inspect", help="查看后端能力，不发起真实连接")
    inspect.add_argument("--backend")
    verify = sub.add_parser("connection-verify", help="验证连接；fake 只返回模拟状态")
    verify.add_argument("--backend")

    submit = sub.add_parser("task-submit", help="提交任务")
    submit.add_argument("--workspace")
    submit.add_argument("--text")
    submit.add_argument("--input", type=Path,
                        help="工作区内提示文件；- 从 stdin 读取至多 512 KiB 严格 UTF-8 文本，与其他输入互斥")
    submit.add_argument("--script")
    submit.add_argument("--backend")
    submit.add_argument("--revision", type=int)
    submit.add_argument("--request", type=Path, help="JSON 请求文件；其中的自报授权字段会被拒绝")
    submit.add_argument("--idempotency-key", dest="idempotency_key")
    submit.add_argument("--conversation-id", dest="conversation_id")

    get_p = sub.add_parser("task-get", help="读取任务与当前运行")
    get_p.add_argument("task_id")
    get_p.add_argument("--compact", action="store_true", help="只返回有界状态、摘要和原生引用")
    watch = sub.add_parser("task-watch", help="通过 --connect 有截止时间地观察任务；只读，不审批或重派")
    watch.add_argument("--compact", action="store_true", help="仅返回状态与有界摘要，不输出原生正文")
    watch.add_argument("--no-events", action="store_true", help="只观察状态，不读取事件或推进事件游标")
    watch.add_argument("task_id")
    watch.add_argument("--timeout", type=float, default=60, help="总观察秒数，默认 60；到期返回已知状态")
    watch.add_argument("--request-timeout", type=float, default=5, help="单次请求最多秒数，且受剩余总时限裁剪")
    watch.add_argument("--interval", type=float, default=0.5, help="初始轮询间隔，空闲时退避至最多 5 秒")
    watch.add_argument("--cursor", type=int, default=0, help="已消费的运行内事件序号；非零时须同时指定 --run-id")
    watch.add_argument("--run-id", help="游标所属运行；任务切换运行时游标自动归零")
    watch.add_argument("--page-size", type=int, default=100)
    events = sub.add_parser("task-events", help="按游标读取事件")
    events.add_argument("task_id")
    events.add_argument("--cursor", type=int, default=0)
    events.add_argument("--page-size", type=int, default=100)
    events.add_argument("--compact", action="store_true", help="服务端投影有界事件页，保留运行和游标")
    cancel = sub.add_parser("task-cancel", help="请求取消，并区分受理与终止")
    cancel.add_argument("task_id")
    approval = sub.add_parser("approval-respond", help="答复匹配的待处理审批")
    approval.add_argument("approval_id")
    approval.add_argument("--decision", required=True, choices=("approve", "deny"))
    approval.add_argument("--run-id", dest="expected_run_id")
    approval.add_argument("--task-id", dest="expected_task_id")
    approval.add_argument("--target-hash", dest="expected_target_hash")
    for command in ("approval-assess", "approval-apply-policy"):
        item = sub.add_parser(command, help="评估或显式应用实例审批策略；要求绑定已有请求")
        item.add_argument("approval_id")
        item.add_argument("--run-id", dest="expected_run_id", required=True)
        item.add_argument("--task-id", dest="expected_task_id", required=True)
        item.add_argument("--target-hash", dest="expected_target_hash", required=True)
    batch = sub.add_parser("approval-batch", help="仅处理明确列出的既有 pending 审批，逐项返回结果")
    batch.add_argument("--request", type=Path, required=True, help="含 items 的绑定审批 JSON")
    resume = sub.add_parser("session-resume", help="按本地索引续接；不表示原生线程已核对")
    resume.add_argument("--conversation-id", dest="conversation_id", default="")
    resume.add_argument("--session-id", default="")
    resume.add_argument("--backend")
    resume.add_argument("--workspace")
    resume.add_argument("--revision", type=int)
    resume.add_argument("--native", action="store_true", help="核对并恢复原生线程；不产生新轮次")
    handoff = sub.add_parser("task-handoff", help="人工暂停或恢复编排")
    handoff.add_argument("task_id")
    handoff.add_argument("--action", required=True, choices=("pause", "resume"))
    handoff.add_argument("--external-change", action="store_true")
    reconcile = sub.add_parser("task-reconcile", help="对账未决运行，不重派")
    reconcile.add_argument("task_id", nargs="?")
    present = sub.add_parser("task-present", help="仅重试原生客户端项目登记与会话关联，不重派任务")
    present.add_argument("task_id")
    evaluate = sub.add_parser("workflow-evaluate", help="评价任务验收；运行成功不等于通过")
    evaluate.add_argument("task_id")
    evaluate.add_argument("--revision", type=int)
    evaluate.add_argument("--input-hash", dest="expected_input_hash")
    evaluate.add_argument("--run-id", dest="expected_run_id")
    evaluate.add_argument("--target-hash", dest="expected_target_hash")
    evaluate.add_argument("--target", action="append", dest="target_paths")
    evaluate.add_argument("--require-review", action="store_true")
    evaluate.add_argument("--review-backend", dest="review_backend")
    evaluate.add_argument("--checks", help="JSON 检查数组，例如 [{\"kind\":\"contains\",\"text\":\"x\"}]")
    evaluate.add_argument("--request", type=Path)
    repair = sub.add_parser("workflow-repair", help="按上限发起有限修复轮，不自行提高预算")
    repair.add_argument("task_id")
    repair.add_argument("--max-repairs", dest="max_repairs", type=int)
    repair.add_argument("--idempotency-key", dest="idempotency_key")
    repair.add_argument("--instructions", help="本轮修复指令；原任务目标保持不变")
    repair.add_argument("--request", type=Path, help="含修复指令及固定版本绑定的 JSON")
    repair.add_argument("--run-id", dest="expected_run_id")
    repair.add_argument("--input-hash", dest="expected_input_hash")
    repair.add_argument("--revision", type=int)
    repair.add_argument("--target-hash", dest="expected_target_hash")
    repair.add_argument("--target", action="append", dest="target_paths")
    snapshot = sub.add_parser("workflow-snapshot", help="读取终态代码目标摘要，准备绑定外部验收；不执行代码")
    snapshot.add_argument("task_id")
    snapshot.add_argument("--target", action="append", dest="target_paths", required=True)
    snapshot.add_argument("--run-id", dest="expected_run_id")
    quality = sub.add_parser("workflow-start", help="启动版本绑定的审查、修复、复核流程")
    quality.add_argument("task_id")
    quality.add_argument("--request", type=Path, required=True, help="含 idempotency_key、target_paths、checks 的 JSON")
    sub.add_parser("scheduler-status", help="查看有界队列快照；数值是候选默认值，不是已测容量")
    sub.add_parser("diagnose", help="脱敏诊断，不导出凭据或启动后端")
    backup = sub.add_parser("state-backup", help="用 SQLite backup API 导出状态库")
    backup.add_argument("--output", required=True, dest="output", help="备份归档路径（.tar.gz）")
    restore = sub.add_parser("state-restore", help="恢复状态库；不改写用户工作区或原生会话")
    restore.add_argument("--input", dest="restore_input", required=True, help="备份归档路径")
    sub.add_parser("mcp", help="以 stdio MCP 启动，与 CLI 共用同一应用服务")
    sub.add_parser("serve", help="运行本机常驻核心；其他 CLI/MCP 用 --connect 连接")
    return parser


def _load_request(args: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if getattr(args, "request", None):
        from asterun.control_protocol import loads, MAX_COMMAND_BYTES

        with Path(args.request).open("rb") as source:
            payload = loads(source.read(MAX_COMMAND_BYTES + 1))
        if not isinstance(payload, dict):
            raise AsterunError(INVALID_REQUEST, "--request 必须是 JSON 对象")
        if args.command == "control-command":
            return {"command": payload}
    if args.command == "task-submit":
        from asterun.cli_input import merge_task_input

        payload = merge_task_input(payload, text=args.text, input_path=args.input)
    mapping = {
        "plugin_id": "plugin_id", "connection_ref": "connection_ref", "pool_ref": "pool_ref",
        "meter": "meter", "window_id": "window_id",
        "workspace": "workspace",
        "text": "text",
        "script": "script",
        "backend": "backend",
        "task_id": "task_id",
        "cursor": "cursor",
        "page_size": "page_size",
        "approval_id": "approval_id",
        "decision": "decision",
        "session_id": "session_id",
        "conversation_id": "conversation_id",
        "idempotency_key": "idempotency_key",
        "action": "action",
        "expected_run_id": "expected_run_id",
        "expected_task_id": "expected_task_id",
        "expected_target_hash": "expected_target_hash",
        "expected_input_hash": "expected_input_hash",
        "review_backend": "review_backend",
        "max_repairs": "max_repairs",
        "target_paths": "target_paths", "instructions": "instructions",
        "output": "output",
        "kind": "kind", "id": "id", "after": "after", "limit": "limit", "artifact_id": "artifact_id",
    }
    for attr, key in mapping.items():
        if getattr(args, attr, None) not in (None, ""):
            payload[key] = getattr(args, attr)
    if getattr(args, "input", None) is not None and args.command != "task-submit":
        payload["path"] = str(args.input)
    if getattr(args, "revision", None) is not None:
        payload["expected_revision"] = args.revision
    if getattr(args, "expected_applied_revision", None) is not None:
        payload["expected_revision"] = args.expected_applied_revision
    if getattr(args, "page_size", None) is not None and args.command == "task-events":
        payload["page_size"] = args.page_size
    if getattr(args, "external_change", False):
        payload["external_change"] = True
    if getattr(args, "require_review", False):
        payload["require_review"] = True
    if getattr(args, "native", False):
        payload["native"] = True
    if getattr(args, "compact", False):
        payload["compact"] = True
    if getattr(args, "include_runs", False):
        payload["include_runs"] = True
    checks = getattr(args, "checks", None)
    if checks:
        parsed = json.loads(checks)
        if not isinstance(parsed, list):
            raise AsterunError(INVALID_REQUEST, "--checks 必须是 JSON 数组")
        payload["checks"] = parsed
    restore_input = getattr(args, "restore_input", None)
    if restore_input is not None:
        payload["input"] = str(restore_input)
    return payload


def _app(args: argparse.Namespace, *, entry: str = "cli") -> Application:
    config_path = args.config or default_config_path(args.home)
    state_dir = args.state_dir or default_state_dir(args.home)
    if args.connect:
        from asterun.service import LocalClient

        return LocalClient(state_dir / "asterun.sock", entry=entry)
    if args.command == "state-restore":
        from asterun.config import load_config
        from asterun.instance import InstanceLock

        # 恢复入口不能先尝试初始化可能已经损坏的旧库。
        app = Application(load_config(config_path), state_dir=state_dir, entry=entry)
        lock = InstanceLock(state_dir / "asterun.lock")
        try:
            lock.acquire()
        except BaseException:
            app.close()
            raise
        app.instance_lock = lock
        return app
    return Application.from_paths(config_path, state_dir, entry=entry,
                                  background=args.command in {"serve", "mcp"},
                                  restore_scheduler=args.command != "usage-report")


COMMAND_METHODS = {
    **{name.replace(".", "-"): name for name in (
        "plugin.list", "plugin.inspect", "plugin.register", "plugin.enable", "plugin.disable",
        "connection.setup", "usage.inspect", "capability.invoke", "capability.discovery", "capability.prepare", "capability.submit", "capability.get")},
    "approval-assess": "approval.assess",
    "approval-apply-policy": "approval.apply-policy",
    "approval-batch": "approval.batch",
    "control-discovery": "control.discovery",
    "control-resources": "control.resources",
    "control-command": "control.command",
    "control-get": "control.get",
    "control-events": "control.events",
    "control-upload": "control.upload",
    "control-content": "control.content",
    "config-validate": "config.validate",
    "config-apply": "config.apply",
    "backend-inspect": "backend.inspect",
    "connection-verify": "connection.verify",
    "task-submit": "task.submit",
    "task-get": "task.get",
    "usage-report": "usage.report",
    "task-events": "task.events",
    "task-cancel": "task.cancel",
    "approval-respond": "approval.respond",
    "session-resume": "session.resume",
    "task-handoff": "task.handoff",
    "task-reconcile": "task.reconcile",
    "task-present": "task.present",
    "workflow-evaluate": "workflow.evaluate",
    "workflow-snapshot": "workflow.snapshot",
    "workflow-repair": "workflow.repair",
    "workflow-start": "workflow.start",
    "scheduler-status": "scheduler.status",
    "diagnose": "diagnose",
    "state-backup": "state.backup",
    "state-restore": "state.restore",
}


def emit(envelope: Envelope, stream: Any = None) -> None:
    target = stream or sys.stdout
    json.dump(envelope.to_dict(), target, ensure_ascii=False, indent=2)
    target.write("\n")


def exit_code(envelope: Envelope) -> int:
    if envelope.ok:
        return 0
    code = (envelope.error or {}).get("code")
    if code in CONFIG_EXIT:
        return 2
    if code in AUTH_EXIT:
        return 3
    if code == "CAPABILITY_UNSUPPORTED":
        return 4
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "version":
        emit(Envelope(ok=True, data={"version": __version__, "package": "asterun"}))
        return 0
    app = None
    try:
        if args.command == "config-migrate":
            from asterun.config_migration import migrate_v1_to_v2, write_v2_migration

            if args.connect or args.config is None:
                raise AsterunError(INVALID_REQUEST, "config-migrate 需要显式 --config，且不能连接现有服务")
            if any((args.output, args.backup, args.expected_source_sha256)):
                if not all((args.output, args.backup, args.expected_source_sha256)):
                    raise AsterunError(INVALID_REQUEST, "写入候选需要 output、backup 和 expected-source-sha256")
                data = write_v2_migration(args.config, args.output, args.backup,
                                          expected_source_sha256=args.expected_source_sha256)
            else:
                data = migrate_v1_to_v2(args.config)
            emit(Envelope(ok=True, data=data))
            return 0
        from asterun.operations_cli import COMMANDS, handle as handle_operation
        if args.command in COMMANDS:
            envelope = handle_operation(args)
            emit(envelope)
            if args.command == "review-watch" and envelope.ok:
                return {"deadline": 124, "unknown": 1, "invalid_result": 1, "execution_failed": 1}.get(envelope.data.get("reason"), 0)
            return exit_code(envelope)
        if args.command == "task-watch":
            from asterun.observe import watch_task

            if not args.connect:
                raise AsterunError(INVALID_REQUEST, "task-watch 必须与 --connect 同用，连接已有常驻核心")
            state_dir = args.state_dir or default_state_dir(args.home)
            result = watch_task(state_dir / "asterun.sock", args.task_id, timeout=args.timeout,
                                request_timeout=args.request_timeout, interval=args.interval,
                                cursor=args.cursor, run_id=args.run_id, page_size=args.page_size,
                                compact=args.compact, include_events=not args.no_events,
                                on_events=lambda page: print(json.dumps(
                                    {"type": "events", "data": page}, ensure_ascii=False), flush=True))
            print(json.dumps({"type": "observation", **result.to_dict()}, ensure_ascii=False), flush=True)
            return {"deadline": 124, "interrupted": 130}.get(result.data.get("reason"), exit_code(result))
        payload = None
        if args.command not in {"mcp", "serve"}:
            from asterun.cli_input import check_ipc_size

            payload = _load_request(args)
            check_ipc_size(COMMAND_METHODS[args.command], payload)
        app = _app(args, entry="mcp" if args.command == "mcp" else "core" if args.command == "serve" else "cli")
        if args.command == "mcp":
            from asterun.mcp_server import serve

            serve(app)
            return 0
        if args.command == "serve":
            if args.connect:
                raise AsterunError(INVALID_REQUEST, "serve 不能与 --connect 同用")
            from asterun.service import serve
            from threading import Event
            import signal

            stop = Event()
            previous = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGTERM, signal.SIGINT)}
            try:
                serve(app, app.state_dir / "asterun.sock", stop=stop)
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
            return 0
        assert payload is not None
        if args.command in {
            "task-get",
            "task-events",
            "task-cancel",
            "task-handoff",
            "workflow-evaluate",
            "workflow-repair",
            "workflow-start",
        }:
            payload["task_id"] = args.task_id
        if args.command == "task-reconcile" and getattr(args, "task_id", None):
            payload["task_id"] = args.task_id
        if args.command in {"approval-respond", "approval-assess", "approval-apply-policy"}:
            payload["approval_id"] = args.approval_id
        envelope = app.handle(COMMAND_METHODS[args.command], payload)
    except AsterunError as error:
        envelope = Envelope(ok=False, error=error.to_dict())
    except (ValueError, OSError, UnicodeError) as error:
        envelope = Envelope(ok=False, error=AsterunError(INVALID_REQUEST, "请求文件、JSON 或路径无效").to_dict())
    finally:
        if app is not None:
            app.close()
    emit(envelope)
    if args.command == "approval-batch" and envelope.ok and not envelope.data.get("all_succeeded"):
        return 1
    return exit_code(envelope)
