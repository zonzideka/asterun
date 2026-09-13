"""固定 SDK 的 local Agent 适配；核心拥有任务、意图和预算。"""
from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import re
import stat

SDK_VERSION = "1.0.31"
OPTIONS = {"model", "runtime", "state_root", "bridge_bin", "bridge_sha256", "execution_enabled",
           "tools", "disallowed_tools", "mcp_servers", "setting_sources", "timeout_seconds"}
TERMINAL = {"finished": "succeeded", "error": "failed", "cancelled": "cancelled"}


class Rejected(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def require(condition, reason):
    if not condition:
        raise Rejected(reason)


def reference(value, prefix):
    require(isinstance(value, str) and re.fullmatch(prefix + r"[A-Za-z0-9_-]{1,180}", value) is not None,
            "native_reference_invalid")
    return value


def private_directory(value):
    require(isinstance(value, str) and Path(value).is_absolute(), "state_root_required")
    path = Path(value)
    require(path.resolve(strict=True) == path and not path.is_symlink(), "state_root_invalid")
    info = path.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700,
            "state_root_not_private")
    return path


def preflight(context):
    options = context.get("options")
    require(isinstance(options, dict) and not set(options) - OPTIONS, "invalid_options")
    require(options.get("execution_enabled") is True, "execution_not_enabled")
    require(context.get("auth_mode") == "user_api_key", "auth_mode_mismatch")
    require(context.get("upstream_version") == SDK_VERSION, "sdk_version_mismatch")
    require(options.get("runtime") == "local", "local_runtime_required")
    require(context.get("capability") == "agent.execute", "capability_unsupported")
    for key in ("run_id", "task_id", "plan_id", "binding_id", "provider_account_ref"):
        require(isinstance(context.get(key), str) and bool(context[key]), "execution_binding_required")
    model = options.get("model")
    require(isinstance(model, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,180}", model))
            and model not in {"auto", "default", "auto-smart"}, "fixed_model_required")
    for key in ("tools", "disallowed_tools"):
        value = options.get(key)
        require(isinstance(value, list) and len(value) <= 64 and all(isinstance(v, str) and
                re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", v) for v in value) and len(set(value)) == len(value),
                "tool_snapshot_required")
    # 非空 MCP 尚未接 P12 的端点/schema/递归授权，不能作为权限旁路引入。
    require(options.get("mcp_servers") == {}, "inline_mcp_not_supported")
    require(options.get("setting_sources") == [], "ambient_settings_forbidden")
    timeout = options.get("timeout_seconds", 15)
    require(type(timeout) in {int, float} and 0 < timeout <= 20, "invalid_timeout")
    root = private_directory(options.get("state_root"))
    cwd = Path(context.get("workspace_root", ""))
    require(cwd.is_absolute() and cwd.resolve(strict=True) == cwd and cwd == Path.cwd().resolve(), "workspace_mismatch")
    # 状态根在工作区外，防止获准工作区写入的 Agent 改写自己的持久快照。
    require(not root.is_relative_to(cwd), "state_root_inside_workspace")
    binary, sha = options.get("bridge_bin"), options.get("bridge_sha256")
    require(isinstance(binary, str) and Path(binary).is_absolute() and isinstance(sha, str)
            and re.fullmatch(r"[a-f0-9]{64}", sha), "bridge_pin_required")
    path = Path(binary)
    require(path.resolve(strict=True) == path and not path.is_symlink(), "bridge_path_invalid")
    info = path.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid in {0, os.getuid()} and not info.st_mode & 0o022
            and os.access(path, os.X_OK) and info.st_size <= 512 * 1024 * 1024, "bridge_path_invalid")
    with path.open("rb") as stream:
        require(hashlib.file_digest(stream, "sha256").hexdigest() == sha, "bridge_digest_mismatch")
    require(version("cursor-sdk") == SDK_VERSION, "installed_sdk_version_mismatch")
    if context.get("provider_session_id"):
        reference(context["provider_session_id"], "agent-")
    if context.get("previous_provider_turn_id"):
        reference(context["previous_provider_turn_id"], "run-")
    return options, root, cwd, timeout


def get_run(client, context, key, *, previous=False):
    session = reference(context.get("provider_session_id"), "agent-")
    turn = reference(context.get("previous_provider_turn_id" if previous else "provider_turn_id"), "run-")
    info = client.agents.get(session, cwd=context["workspace_root"], api_key=key)
    require(info.agent_id == session and info.runtime == "local" and info.cwd == context["workspace_root"],
            "native_workspace_mismatch")
    run = client.agents.get_run(turn, {"runtime": "local", "agentId": session,
                                      "cwd": context["workspace_root"], "apiKey": key})
    require(run.id == turn and run.agent_id == session, "native_identity_mismatch")
    require(getattr(run.model, "id", None) == context["options"]["model"], "native_model_mismatch")
    if previous or context.get("resume_check"):
        latest = client.agents.list_runs(session, runtime="local", cwd=context["workspace_root"], limit=1, api_key=key)
        require(len(latest.items) == 1 and latest.items[0].id == turn and latest.items[0].agent_id == session,
                "native_latest_turn_changed")
    return run


def mapped(result, context, *, events=(), tool_error=False):
    require(result.agent_id == context["provider_session_id"] and result.id == context["provider_turn_id"],
            "native_identity_mismatch")
    require(getattr(result.model, "id", None) == context["options"]["model"], "native_model_mismatch")
    status = TERMINAL.get(result.status, "pending_reconcile")
    text = result.result
    require(isinstance(text, str) and len(text.encode()) <= 65536, "invalid_output")
    if status == "succeeded" and not text.strip():
        status = "failed"
    if tool_error and status == "succeeded":
        status = "failed"
    usage = []
    if result.usage is not None and status in {"succeeded", "failed", "cancelled"}:
        used = result.usage.total_tokens
        require(type(used) is int and 0 <= used <= 9007199254740991, "invalid_usage")
        # SDK final usage 为单个 Run 的多 turn 累计，不是整个 Agent 的累计。
        usage = [{"meter": "tokens", "used": used, "scope": "run", "scope_ref": result.id,
                  "cumulative": False, "cursor": f"{result.id}:tokens:final",
                  "observed_at": result.created_at or context.get("observation_epoch") or "1970-01-01T00:00:00Z"}]
    return {"status": status, "output": {"text": text}, "usage": usage, "events": list(events),
            "provider_session_id": result.agent_id, "provider_turn_id": result.id,
            "terminated_verified": status == "cancelled"}


def execute(method, input, context):
    refs = {k: context[k] for k in ("provider_session_id", "provider_turn_id") if context.get(k)}
    started = False
    try:
        options, state_root, cwd, timeout = preflight(context)
        key = os.environ.pop("CURSOR_API_KEY", None)
        require(isinstance(key, str) and bool(key.strip()), "credential_required")
        # bridge 的模型工具可继承其环境；账户 key 只放显式 SDK 请求，不放 bridge env。
        from cursor_sdk import CursorClient, AgentOptions, LocalAgentOptions, SendOptions
        # SDK 1.0.31 将 False 同时作为远程 bridge 的 Run RPC 门禁。这里是本机
        # 自建 bridge，且已移除环境 key；保持 SDK local 路由，账户仍只由显式参数提供。
        with ExitStack() as stack:
            client = stack.enter_context(CursorClient.launch_bridge(
                command=options["bridge_bin"], workspace=str(cwd), state_root=str(state_root), host="127.0.0.1",
                timeout=10, client_timeout=timeout, max_retries=0, allow_api_key_env_fallback=True))
            if method in {"execution.observe", "execution.reconcile", "execution.cancel"}:
                run = get_run(client, context, key)
                if method == "execution.cancel" and run.status not in TERMINAL:
                    if not run.supports("cancel"):
                        return {"status": "pending_reconcile", "supported": False, **refs}
                    run.cancel()
                    run = get_run(client, context, key)
                return mapped(run, context)
            require(isinstance(input, dict) and set(input) == {"text"} and isinstance(input["text"], str)
                    and 0 < len(input["text"]) <= 65536, "invalid_input")
            require(options["model"] in {model.id for model in client.models.list(api_key=key)}, "model_unavailable")
            snapshot = AgentOptions(model=options["model"], api_key=key,
                local=LocalAgentOptions(cwd=str(cwd), setting_sources=[]),
                tools=list(options["tools"]), disallowed_tools=list(options["disallowed_tools"]),
                mcp_servers={}, agents={})
            if context.get("provider_session_id"):
                previous = get_run(client, context, key, previous=True)
                require(previous.status in TERMINAL, "previous_run_not_terminal")
                # 同一个已持久绑定的快照再次发送，绝不依赖 SDK 留存安全配置。
                started = True
                agent = stack.enter_context(client.agents.resume(context["provider_session_id"], snapshot))
            else:
                started = True
                agent = stack.enter_context(client.agents.create(snapshot))
            reference(agent.agent_id, "agent-")
            require(not refs.get("provider_session_id") or refs["provider_session_id"] == agent.agent_id,
                    "native_identity_mismatch")
            refs["provider_session_id"] = context["provider_session_id"] = agent.agent_id
            require(getattr(agent.model, "id", None) == options["model"], "native_model_mismatch")
            run = agent.send(input["text"], SendOptions(model=options["model"], mcp_servers={}),
                             idempotency_key=context["run_id"])
            reference(run.id, "run-")
            require(run.agent_id == agent.agent_id, "native_identity_mismatch")
            refs["provider_turn_id"] = context["provider_turn_id"] = run.id
            events, tool_error = [], False
            count = 0
            for event in run.events():
                count += 1
                require(count <= 10000, "event_limit_exceeded")
                message = event.sdk_message
                if message is None:
                    continue
                require(getattr(message, "agent_id", agent.agent_id) in {"", agent.agent_id}
                        and getattr(message, "run_id", run.id) in {"", run.id}, "event_identity_mismatch")
                kind = getattr(message, "type", "unknown")
                if kind == "tool_call":
                    require(getattr(message, "name", None) in options["tools"] and
                            getattr(message, "name", None) not in options["disallowed_tools"], "unexpected_tool")
                    tool_error |= getattr(message, "status", "") == "error"
                if kind in {"status", "tool_call", "request"}:
                    require(len(events) < 120, "event_limit_exceeded")
                    # 不采集工具参数、工具原始结果、thinking 或 SDK exception 文本。
                    events.append({"provider_event_id": f"{run.id}:{event.offset or count}",
                        "category": f"cursor.{kind}", "summary": f"cursor {kind}",
                        "provider_sequence": count})
            return mapped(run.wait(), context, events=events, tool_error=tool_error)
    except Exception as error:
        reason = str(error) if isinstance(error, Rejected) else "sdk_result_unknown"
        # 凡执行 SDK create/resume/send 后异常，不凭 SDK 的 retryable 字段重派。
        status = "pending_reconcile" if started or method != "execution.start" else "failed"
        return {"status": status, "output": {"text": reason}, "usage": [], **refs,
                "events": [{"provider_event_id": context.get("run_id", "unknown") + ":" + reason,
                            "category": "cursor." + reason, "summary": reason}]}
