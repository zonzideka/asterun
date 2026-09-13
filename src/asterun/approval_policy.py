"""用户实例配置提供的有界审批策略；原生请求和模型描述均不是授权来源。"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
from datetime import datetime
from pathlib import Path
from typing import Any

from asterun.errors import AsterunError, INVALID_CONFIG
from asterun.policy import Grant

RULE_REQUIRED = {"id", "workspace", "backend", "cwd", "expires_at", "input_hash", "argv", "executable_sha256"}
RULE_OPTIONAL = {"read_roots", "host", "repository", "pr", "head_sha"}
PARAM_KEYS = {"command", "cwd", "threadId", "turnId", "itemId", "reason", "commandActions", "availableDecisions"}
HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def validate_policy(raw: object, workspaces: dict, backends: dict) -> dict:
    if raw is None:
        return {"mode": "manual", "rules": []}
    if not isinstance(raw, dict) or set(raw) - {"mode", "rules"}:
        raise AsterunError(INVALID_CONFIG, "approval_policy 只接受 mode 和 rules")
    mode, rules = raw.get("mode", "manual"), raw.get("rules", [])
    if mode not in {"manual", "bounded"} or not isinstance(rules, list) or len(rules) > 100:
        raise AsterunError(INVALID_CONFIG, "approval_policy 需要 manual/bounded 及至多 100 条规则")
    seen = set()
    for rule in rules:
        if (not isinstance(rule, dict) or not RULE_REQUIRED <= set(rule)
                or set(rule) - RULE_REQUIRED - RULE_OPTIONAL):
            raise AsterunError(INVALID_CONFIG, "approval_policy.rules 字段不完整或含未知字段")
        for key in RULE_REQUIRED - {"argv"}:
            if not isinstance(rule[key], str) or not rule[key] or "\x00" in rule[key]:
                raise AsterunError(INVALID_CONFIG, f"approval_policy.rules.{key} 必须为非空字符串")
        if rule["id"] in seen:
            raise AsterunError(INVALID_CONFIG, "approval_policy 规则 id 不得重复")
        seen.add(rule["id"])
        if rule["workspace"] not in workspaces or rule["backend"] not in backends:
            raise AsterunError(INVALID_CONFIG, "approval_policy 必须引用已配置工作区及后端")
        if backends[rule["backend"]].kind != "codex":
            raise AsterunError(INVALID_CONFIG, "首版有界审批策略仅适用于 Codex 原生请求")
        argv = rule["argv"]
        if (not isinstance(argv, list) or not argv or len(argv) > 128
                or any(not isinstance(a, str) or not a or "\x00" in a for a in argv)
                or not Path(argv[0]).is_absolute()):
            raise AsterunError(INVALID_CONFIG, "approval_policy.argv 需要完整 argv，首项为绝对可执行文件路径")
        if not HEX64.fullmatch(rule["input_hash"]) or not HEX64.fullmatch(rule["executable_sha256"]):
            raise AsterunError(INVALID_CONFIG, "approval_policy 输入和可执行文件摘要必须为 SHA-256")
        try:
            expiry = datetime.fromisoformat(rule["expires_at"].replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                raise ValueError("timezone required")
        except ValueError as exc:
            raise AsterunError(INVALID_CONFIG, "approval_policy.expires_at 必须含时区") from exc
        roots = rule.get("read_roots", [])
        if (not isinstance(roots, list) or any(not isinstance(p, str) or not p or "\x00" in p
                or not Path(p).is_absolute() for p in roots) or not Path(rule["cwd"]).is_absolute()):
            raise AsterunError(INVALID_CONFIG, "approval_policy cwd/read_roots 需要显式绝对路径")
        if not _within(Path(rule["cwd"]), [workspaces[rule["workspace"]].root, *map(Path, roots)]):
            raise AsterunError(INVALID_CONFIG, "approval_policy.cwd 必须位于工作区或专用 read_roots 内")
        repo_fields = {"host", "repository", "pr", "head_sha"} & set(rule)
        if repo_fields and (repo_fields != {"host", "repository", "pr", "head_sha"}
                or rule["host"] != "github.com"
                or not isinstance(rule["repository"], str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", rule["repository"])
                or type(rule["pr"]) is not int or rule["pr"] < 1
                or not isinstance(rule["head_sha"], str) or not re.fullmatch(r"[0-9a-f]{40}", rule["head_sha"])):
            raise AsterunError(INVALID_CONFIG, "approval_policy 的 host=github.com、repository/pr/head_sha 必须完整且明确")
    # 保留用户输入的绝对路径，每次评估重新解析，不能缓存掉符号链接变化。
    import copy
    return {"mode": mode, "rules": copy.deepcopy(rules)}


def _within(path: Path, roots: list[Path]) -> bool:
    try:
        resolved = path.resolve(strict=True)
        return any(resolved.is_relative_to(root.resolve(strict=True)) for root in roots)
    except (OSError, ValueError, RuntimeError):
        return False


def _argv(command: Any) -> list[str] | None:
    if isinstance(command, list):
        return command if command and all(isinstance(a, str) and a and "\x00" not in a for a in command) else None
    if not isinstance(command, str) or any(c in command for c in "$`;&|<>(){}\n\r\x00*?~"):
        return None
    try:
        args = shlex.split(command, posix=True)
    except ValueError:
        return None
    # shell 包装可读取 BASH_ENV/ZDOTDIR 等启动配置。首版不推测其环境或启动脚本。
    if args and Path(args[0]).name in {"sh", "bash", "zsh", "dash", "fish"}:
        return None
    return args or None


def _read_command(rule: dict, workspace_root: Path) -> bool:
    argv = rule["argv"]
    binary, args = Path(argv[0]).name, argv[1:]
    roots = [workspace_root, *map(Path, rule.get("read_roots", []))]
    if binary == "pwd":
        return not args or args == ["-P"]
    if binary == "cat":
        paths = args[1:] if args[:1] == ["--"] else args
        return bool(paths) and all(not p.startswith("-") and _within(Path(rule["cwd"]) / p, roots)
                                  and (Path(rule["cwd"]) / p).is_file() for p in paths)
    if binary == "git":
        # 禁止别名、fsmonitor、pager、外部 diff/textconv；只开放有限查询。
        prefix = ["--no-pager", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false"]
        if args[:len(prefix)] != prefix:
            return False
        return args[len(prefix):] in (["status", "--porcelain=v1", "--untracked-files=no"],
                                    ["rev-parse", "--verify", "HEAD"])
    if binary != "gh" or not {"host", "repository", "pr", "head_sha"} <= set(rule) or rule.get("host") != "github.com":
        return False
    repo, pr, sha = rule["repository"], str(rule["pr"]), rule["head_sha"]
    qualified_repo = f'{rule["host"]}/{repo}'
    # 固定 repo/PR；不支持 --jq/--template/--web、任意主机、分页或输入参数。
    if args[:3] == ["pr", "view", pr] and len(args) == 7 and args[3:6] == ["--repo", qualified_repo, "--json"]:
        return bool(args[6]) and set(args[6].split(",")) <= {
            "number", "title", "body", "state", "author", "headRefOid", "baseRefOid", "files", "url"}
    if args == ["pr", "diff", pr, "--repo", qualified_repo, "--color", "never"]:
        return True
    endpoints = {f"repos/{repo}/pulls/{pr}", f"repos/{repo}/pulls/{pr}/files",
                 f"repos/{repo}/commits/{sha}", f"repos/{repo}/git/trees/{sha}"}
    return (len(args) == 6 and args[:5] == ["api", "--hostname", rule["host"], "--method", "GET"]
            and args[5] in endpoints)


def _executable_hash(path: Path) -> str | None:
    # 不把设备、FIFO 或无限大的文件读入内存；真实执行仍由原生沙箱决定。
    if not path.is_file() or path.stat().st_size > 128 * 1024 * 1024:
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        total = 0
        while block := stream.read(1024 * 1024):
            total += len(block)
            if total > 128 * 1024 * 1024:
                return None
            digest.update(block)
    return digest.hexdigest()


def assess(policy: dict, approval, task, run, workspace_root: Path, now: str) -> dict:
    result = {"decision": "manual", "reason": "实例未启用有界审批策略", "policy_id": None,
              "target_hash": approval.target_hash, "policy_digest": hashlib.sha256(
                  json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
              "native_confirmed": False}
    if policy["mode"] != "bounded":
        return result
    request = approval.native_request
    params = request.get("params") if isinstance(request, dict) else None
    if (not isinstance(params, dict) or set(request) - {"jsonrpc", "id", "method", "params"}
            or request.get("method") != "item/commandExecution/requestApproval"
            or set(params) - PARAM_KEYS or not isinstance(params.get("cwd"), str)
            or not Path(params["cwd"]).is_absolute()):
        return {**result, "reason": "原生请求包含未支持的操作、权限或参数；需要人工审批"}
    argv = _argv(params.get("command"))
    if not argv:
        return {**result, "reason": "命令不属于可识别的简单 argv；需要人工审批"}
    for rule in policy["rules"]:
        if (rule["workspace"] != task.workspace or rule["backend"] != run.backend.value
                or rule["input_hash"] != task.input_hash or rule["argv"] != argv
                or Grant(expires_at=rule["expires_at"]).is_expired(now)):
            continue
        try:
            cwd = Path(params["cwd"]).resolve(strict=True)
            if cwd != Path(rule["cwd"]).resolve(strict=True) or not cwd.is_dir():
                continue
            if not _within(cwd, [workspace_root, *map(Path, rule.get("read_roots", []))]):
                continue
            # 规则中的可执行文件以及实际 cwd 都是实时验证，描述字段不决定授权。
            if _executable_hash(Path(argv[0])) != rule["executable_sha256"]:
                continue
            if not _read_command(rule, workspace_root):
                continue
        except (OSError, ValueError, RuntimeError):
            continue
        return {**result, "decision": "approve", "reason": "匹配实例中固定输入、有效期、cwd 和完整 argv 的只读规则",
                "policy_id": rule["id"], "expires_at": rule["expires_at"],
                "host": rule.get("host"), "repository": rule.get("repository"), "pr": rule.get("pr"), "head_sha": rule.get("head_sha"),
                "remote_head_verified": False}
    return {**result, "reason": "没有匹配的有效只读规则；需要人工审批"}
