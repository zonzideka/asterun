"""本机管理及可选审查配方的 CLI 边界，不通过 MCP 暴露服务管理权限。"""
from pathlib import Path

from asterun.contracts import Envelope
from asterun.errors import AsterunError, INVALID_REQUEST
from asterun.home import default_config_path, default_state_dir

COMMANDS = {"codex-discover", "service-plan", "service-apply", "service-rollback", "review-prepare",
            "review-submit", "review-status", "review-publish-plan", "review-publish",
            "review-watch", "review-inbox", "review-ack"}


def add_parsers(sub):
    discover = sub.add_parser("codex-discover", help="列出本机 Codex 候选；默认不启动候选程序")
    discover.add_argument("--explicit")
    discover.add_argument("--applications-dir", type=Path, action="append")
    discover.add_argument("--probe-version", action="store_true", help="显式运行候选的版本查询")
    plan = sub.add_parser("service-plan", help="准备现有 macOS 实例的 Codex 启用计划；不重启服务")
    plan.add_argument("--plist", type=Path, required=True)
    plan.add_argument("--plan-dir", type=Path, required=True)
    plan.add_argument("--codex-bin", type=Path, required=True)
    plan.add_argument("--workspace")
    plan.add_argument("--workspace-root", type=Path)
    plan.add_argument("--current-path", type=Path)
    for command, description in (("service-apply", "应用已审阅启用计划"),
                                 ("service-rollback", "恢复原配置和启动文件；不自动启动旧服务")):
        item = sub.add_parser(command, help=description)
        item.add_argument("plan", type=Path)
        item.add_argument("--plan-sha256", required=True, help="service-plan 返回的固定计划摘要")
        if command == "service-apply":
            item.add_argument("--verify-connection", action="store_true", help="启动后执行无 prompt 连接核验")
    prepare = sub.add_parser("review-prepare", help="在独立快照目录或项目 worktree 获取固定 PR 快照；不调用模型")
    prepare.add_argument("--workspace", required=True)
    prepare.add_argument("--backend", required=True)
    prepare.add_argument("--repo", required=True)
    prepare.add_argument("--pr", required=True, type=int)
    prepare.add_argument("--attempt", required=True, help="一次逻辑审查的稳定标识；重试时保持相同")
    prepare.add_argument("--head", help="期望 PR head SHA")
    prepare.add_argument("--project-workspace", help="真实项目的已配置别名；--workspace 必须为该项目的独立 detached worktree")
    prepare.add_argument("--read-mode", choices=("auto", "native", "manual"), default="auto",
                         help="新 Codex 审查默认使用固定读取工具；manual 显式保留逐条原生审批，已有 attempt 不自动迁移")
    prepare.add_argument("--previous-review", help="同一 PR 上轮有效评审引用；固定 findings 和旧 SHA")
    prepare.add_argument("--max-rounds", type=int, default=3, help="关联审查链最多轮数，默认 3；到限后交回人工")
    inbox = sub.add_parser("review-inbox", help="读取尚未确认交付的评审结论和阻塞；读取不确认")
    inbox.add_argument("--consumer-id", required=True)
    inbox.add_argument("--after")
    inbox.add_argument("--limit", type=int, default=20)
    ack = sub.add_parser("review-ack", help="发起端交付后按确切通知摘要确认")
    ack.add_argument("review_id")
    ack.add_argument("--consumer-id", required=True)
    ack.add_argument("--notification-sha256", required=True)
    watch = sub.add_parser("review-watch", help="有界观察原审查并返回门禁、原生引用和下一步；不发布、不修复")
    watch.add_argument("review_id")
    watch.add_argument("--timeout", type=float, default=60)
    watch.add_argument("--interval", type=float, default=1)
    watch.add_argument("--apply-policy", action="store_true", help="显式应用已有配置中的审批规则；不匹配即交回人工")
    watch.add_argument("--json", dest="json_output", action="store_true",
                       help="stdout 仅返回一个最终 JSON 信封，供机器读取；保留原退出码与继续观察引用")
    for command, description in (("review-submit", "用原幂等键提交已准备审查；已提交则只回读"),
                                 ("review-status", "读取审查引用、原生定位及当前任务；不提交新请求"),
                                 ("review-publish-plan", "读取结构化评审结果并准备固定发布正文"),
                                 ("review-publish", "发布已明确批准的评审计划；未知结果不重发")):
        item = sub.add_parser(command, help=description)
        item.add_argument("review_id")
        if command == "review-publish":
            item.add_argument("--plan-sha256", required=True, help="明确批准的完整发布计划摘要")


def handle(args):
    try:
        return _handle(args)
    except (KeyError, TypeError, AttributeError, RecursionError, EOFError):
        # 清单和外部响应均是输入数据；结构错误不应输出 traceback 或其中的原文。
        raise AsterunError(INVALID_REQUEST, "管理请求、清单或响应结构无效；未输出原始内容") from None


def _handle(args):
    command = args.command
    state_dir = args.state_dir or default_state_dir(args.home)
    config_path = args.config or default_config_path(args.home)
    local_only = {"codex-discover", "service-plan", "service-apply", "service-rollback", "review-publish"}
    if command in local_only and args.connect:
        raise AsterunError(INVALID_REQUEST, "该命令是本机管理操作，不通过 --connect 转发")
    if command.startswith("review-") and command != "review-publish" and not args.connect:
        raise AsterunError(INVALID_REQUEST, "审查配方要求 --connect 使用已运行核心")
    if command == "codex-discover":
        from asterun.codex_discovery import discover_codex_candidates
        data = discover_codex_candidates(explicit=args.explicit, application_dirs=args.applications_dir,
                                         probe_version=args.probe_version)
    elif command == "service-plan":
        from asterun.service_management import prepare_service_plan
        if bool(args.workspace) != bool(args.workspace_root):
            raise AsterunError(INVALID_REQUEST, "--workspace 和 --workspace-root 必须同时提供")
        data = prepare_service_plan(config_path=config_path, state_dir=state_dir, plist_path=args.plist,
                                    plan_dir=args.plan_dir, codex_bin=args.codex_bin, workspace_alias=args.workspace,
                                    workspace_root=args.workspace_root, current_path=args.current_path)
    elif command in {"service-apply", "service-rollback"}:
        from asterun.service_management import apply_service_plan, rollback_service_plan
        if command == "service-apply":
            data = apply_service_plan(args.plan, expected_plan_sha256=args.plan_sha256,
                                      verify_connection=args.verify_connection)
        else:
            data = rollback_service_plan(args.plan, expected_plan_sha256=args.plan_sha256)
        if data.get("status") not in {"applied", "rolled_back_service_stopped"}:
            return Envelope(ok=False, data=data, error=AsterunError(INVALID_REQUEST,
                            "服务操作未完成；按返回步骤检查，不自动重跑").to_dict())
    else:
        from asterun.review_recipe import prepare_review, submit_review, read_review, prepare_publication, publish_review
        if command == "review-publish":
            data = publish_review(state_dir, args.review_id, approved_plan_sha256=args.plan_sha256)
        else:
            from asterun.service import LocalClient
            client = LocalClient(state_dir / "asterun.sock")
            try:
                if command == "review-prepare":
                    data = prepare_review(client, state_dir, workspace=args.workspace, backend=args.backend,
                                          repo=args.repo, pr=args.pr, attempt=args.attempt, head=args.head,
                                          previous_review=args.previous_review, max_rounds=args.max_rounds,
                                          project_workspace=args.project_workspace, read_mode=args.read_mode)
                elif command == "review-submit":
                    data = submit_review(client, state_dir, args.review_id)
                elif command == "review-status":
                    data = read_review(client, state_dir, args.review_id)
                elif command in {"review-inbox", "review-ack"}:
                    from asterun.review_recipe import _view
                    payload = {"consumer_id": args.consumer_id}
                    if command == "review-inbox":
                        payload["limit"] = args.limit
                        if args.after:
                            payload["after"] = args.after
                    else:
                        payload.update(review_id=args.review_id, notification_sha256=args.notification_sha256)
                    data = _view(client, command.replace("-", ".", 1), payload)
                elif command == "review-watch":
                    import json
                    from asterun.review_follow import watch_review
                    on_change = None if args.json_output else lambda value: print(json.dumps(
                        {"type": "review_update", "data": value}, ensure_ascii=False), flush=True)
                    data = watch_review(client, state_dir, args.review_id, timeout=args.timeout, interval=args.interval,
                        apply_policy=args.apply_policy, on_change=on_change)
                else:
                    data = prepare_publication(client, state_dir, args.review_id)
            finally:
                client.close()
    return Envelope(ok=True, data=data)
