"""Codex App Server 请求分类与答复形状。由旧 protocols/codex_requests.py 迁入。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# Check the installed protocol and effective session restrictions, not a CLI
# version label. This opt-in profile must not change the ordinary interactive
# backend's permission policy.
READ_SCOPE_CONFIG = {
    "features.shell_tool": False,
    "features.apps": False,
    "features.plugins": False,
    "features.remote_plugin": False,
    "features.browser_use": False,
    "features.computer_use": False,
    "features.code_mode": False,
    "features.code_mode_host": False,
    "features.multi_agent": False,
    "features.multi_agent_v2": False,
    "features.memories": False,
    "features.hooks": False,
    "features.skill_search": False,
    "features.skill_mcp_dependency_install": False,
    "features.skip_host_skill_discovery": True,
    "features.request_permissions_tool": False,
    "features.image_generation": False,
    "features.view_image": False,
    "features.tool_suggest": False,
    "features.goals": False,
    "features.shell_snapshot": False,
    "features.shell_snapshot_v2": False,
    "web_search": "disabled",
    "project_doc_max_bytes": 0,
    "skills.include_instructions": False,
    "skills.bundled.enabled": False,
}


def preflight_codex_read_scope(binary: str) -> None:
    """Check the installed protocol without touching native HOME or an account."""
    import json
    import os
    import subprocess
    import tempfile
    from pathlib import Path
    from asterun.errors import AsterunError, CAPABILITY_UNSUPPORTED

    try:
        with tempfile.TemporaryDirectory(prefix="asterun-codex-schema-") as directory:
            root = Path(directory)
            (root / "home").mkdir()
            env = {"PATH": os.defpath, "HOME": directory, "CODEX_HOME": str(root / "home")}
            subprocess.run([binary, "app-server", "generate-json-schema", "--experimental",
                "--out", str(root / "schema")], cwd=root, env=env,
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
            def schema(name):
                return json.loads((root / "schema" / name).read_text())
            start = schema("v2/ThreadStartParams.json")
            turn = schema("v2/TurnStartParams.json")
            resume = schema("v2/ThreadResumeParams.json")
            for document in (start, turn):
                if document["properties"]["environments"]["type"] != ["array", "null"]:
                    raise ValueError("environment control missing")
            if "dynamicTools" not in start["properties"] or "config" not in resume["properties"]:
                raise ValueError("dynamic tool control missing")
            call = schema("DynamicToolCallParams.json")
            if not {"arguments", "callId", "threadId", "tool", "turnId"} <= set(call["required"]):
                raise ValueError("tool binding missing")
            response = schema("DynamicToolCallResponse.json")
            if set(response["required"]) != {"contentItems", "success"}:
                raise ValueError("tool result shape changed")
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        raise AsterunError(CAPABILITY_UNSUPPORTED,
            "当前 Codex 协议缺少固定只读工具所需能力或无法完成核验，未发送模型请求",
            next_action="检查当前 App Server 的 schema 导出及固定工具协议；普通人工审批入口仍可使用") from exc


def read_scope_config(transport, cwd) -> dict[str, Any]:
    """Extract names only: config/read may contain credentials and is never logged."""
    from asterun.errors import AsterunError, CAPABILITY_UNSUPPORTED
    result = transport.request("config/read", {"cwd": str(cwd), "includeLayers": False})
    config = result.get("config")
    if not isinstance(config, dict):
        raise AsterunError(CAPABILITY_UNSUPPORTED, "原生只读配置无法核验，未发送模型请求")
    for key, expected in READ_SCOPE_CONFIG.items():
        value = config
        for part in key.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if isinstance(value, dict) and isinstance(expected, bool):
            value = value.get("enabled")
        if type(value) is not type(expected) or value != expected:
            raise AsterunError(CAPABILITY_UNSUPPORTED, "原生只读配置未生效，未发送模型请求")
    servers = config.get("mcp_servers", {})
    if not isinstance(servers, dict) or any(not isinstance(name, str) or not name for name in servers):
        raise AsterunError(CAPABILITY_UNSUPPORTED, "原生 MCP 配置无法核验，未发送模型请求")
    # An empty table is recursively merged, so it cannot disable inherited MCPs.
    return {**READ_SCOPE_CONFIG, "mcp_servers": {name: {"enabled": False} for name in servers}}


def verify_read_scope_session(transport, session) -> None:
    from pathlib import Path
    from asterun.errors import AsterunError, CAPABILITY_UNSUPPORTED
    response = session.thread
    if response.get("approvalPolicy") != "never" or response.get("sandbox", {}).get("type") != "readOnly":
        raise AsterunError(CAPABILITY_UNSUPPORTED, "原生只读权限未生效，未发送模型请求")
    sources = response.get("instructionSources")
    native_home = getattr(transport, "native_home", None)
    permitted = {native_home / name for name in ("AGENTS.md", "AGENTS.override.md")} if native_home else set()
    # User-owned global instructions remain part of the native session. Only
    # their exact native-home paths are allowed; repository sources are not.
    if not isinstance(sources, list) or any(not isinstance(source, str) or
            not Path(source).is_absolute() or ".." in Path(source).parts or
            Path(source) not in permitted for source in sources):
        raise AsterunError(CAPABILITY_UNSUPPORTED, "固定审查仍加载了外部指令文件，未发送模型请求")
    cursor, seen = None, set()
    for _ in range(100):
        params = {"threadId": session.session_id, "detail": "toolsAndAuthOnly", "limit": 100}
        if cursor is not None:
            params["cursor"] = cursor
        page = transport.request("mcpServerStatus/list", params)
        rows = page.get("data")
        if not isinstance(rows, list) or any(not isinstance(row, dict) or
                row.get("runtimeStatus") != "disabled" for row in rows):
            raise AsterunError(CAPABILITY_UNSUPPORTED, "原生 MCP 未全部禁用，未发送模型请求")
        cursor = page.get("nextCursor")
        if cursor is None:
            return
        if not isinstance(cursor, str) or cursor in seen:
            break
        seen.add(cursor)
    raise AsterunError(CAPABILITY_UNSUPPORTED, "原生 MCP 清单无法完整核验，未发送模型请求")

CODEX_TOOL_REQUEST_USER_INPUT_METHOD = "item/tool/requestUserInput"
CODEX_COMMAND_APPROVAL_METHOD = "item/commandExecution/requestApproval"
CODEX_FILE_CHANGE_APPROVAL_METHOD = "item/fileChange/requestApproval"
CODEX_PERMISSIONS_APPROVAL_METHOD = "item/permissions/requestApproval"
CODEX_DYNAMIC_TOOL_CALL_METHOD = "item/tool/call"
CODEX_MCP_ELICITATION_METHOD = "mcpServer/elicitation/request"
CODEX_LEGACY_APPLY_PATCH_APPROVAL_METHOD = "applyPatchApproval"
CODEX_LEGACY_EXEC_COMMAND_APPROVAL_METHOD = "execCommandApproval"


@dataclass(frozen=True)
class HumanInputRequest:
    request_id: int | str
    questions: list[dict[str, Any]]
    tool_call_id: str = ""


def parse_codex_user_input_request(msg: dict[str, Any]) -> HumanInputRequest | None:
    if msg.get("method") != CODEX_TOOL_REQUEST_USER_INPUT_METHOD:
        return None
    params = msg.get("params", {})
    return HumanInputRequest(
        request_id=msg.get("id", ""),
        tool_call_id=params.get("itemId", ""),
        questions=params.get("questions", []),
    )


def build_codex_user_input_answer(answers: dict[str, Any]) -> dict[str, dict[str, Any]]:
    normalized: dict[str, dict[str, list[str]]] = {}
    for question_id, answer in answers.items():
        if isinstance(answer, dict) and "answers" in answer:
            values = answer["answers"]
        elif isinstance(answer, list):
            values = answer
        else:
            values = [answer]
        normalized[question_id] = {"answers": [str(item) for item in values]}
    return {"answers": normalized}


def is_codex_approval_request(msg: dict[str, Any]) -> bool:
    return msg.get("method") in {
        CODEX_COMMAND_APPROVAL_METHOD,
        CODEX_FILE_CHANGE_APPROVAL_METHOD,
        CODEX_PERMISSIONS_APPROVAL_METHOD,
        CODEX_LEGACY_APPLY_PATCH_APPROVAL_METHOD,
        CODEX_LEGACY_EXEC_COMMAND_APPROVAL_METHOD,
    }


def build_codex_decision_answer(decision: Any) -> dict[str, Any]:
    return {"decision": decision}


def build_codex_legacy_decision_answer(decision: str) -> dict[str, str]:
    decision_map = {
        "accept": "approved",
        "acceptForSession": "approved_for_session",
        "decline": "denied",
        "cancel": "abort",
    }
    return {"decision": decision_map.get(decision, decision)}


def build_codex_permissions_answer(
    *,
    permissions: str,
    scope: str | None = None,
    strict_auto_review: bool | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"permissions": permissions}
    if scope is not None:
        result["scope"] = scope
    if strict_auto_review is not None:
        result["strictAutoReview"] = strict_auto_review
    return result


def build_codex_mcp_elicitation_answer(
    *,
    action: str = "accept",
    content: Any = None,
    meta: Any = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"action": action}
    if content is not None:
        result["content"] = content
    if meta is not None:
        result["_meta"] = meta
    return result


def build_codex_dynamic_tool_answer(
    *,
    success: bool = True,
    text: str | None = None,
    content_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    items = list(content_items or [])
    if text is not None:
        items.append({"type": "inputText", "text": text})
    return {"success": success, "contentItems": items}
