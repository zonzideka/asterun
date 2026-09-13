"""固定 PR 的项目工作区绑定；只核验已有 Git worktree，不创建或移动工作区。"""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess

from asterun.errors import AsterunError, INVALID_REQUEST
from asterun.review_recipe import _bounded_run, require


def _git(root, *args, absent_ok=False):
    # 仅读取 Git 元数据；避免继承另一个 checkout 的 GIT_DIR 或外部 fsmonitor。
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    try:
        result = _bounded_run(["git", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
            "-c", "core.hooksPath=" + os.devnull, "-C", str(root), *args], env=env, timeout=10)
        require(result.returncode == 0 or absent_ok and result.returncode == 1,
                "无法核验本机 Git 工作区；未输出配置或命令原文")
        return result.stdout.decode("utf-8", errors="strict").rstrip("\n")
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        raise AsterunError(INVALID_REQUEST, "无法读取本机 Git 工作区元数据") from None


def _root(path):
    require(isinstance(path, str), "项目工作区路径无效")
    root = Path(path)
    require(root.is_absolute() and root.is_dir() and root == root.resolve()
            and not any(p.is_symlink() for p in (root, *root.parents)), "项目工作区必须为无符号链接的规范目录")
    require(_git(root, "rev-parse", "--show-toplevel") == str(root), "请选择 Git 工作树根目录")
    return root


def _common(root):
    return str(Path(_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve())


def validate_binding(value, root, identity):
    keys = {"project_workspace", "project_root", "git_common_dir", "git_dir", "review_head"}
    require(isinstance(value, dict) and set(value) == keys
            and all(isinstance(v, str) and v for v in value.values()), "审查项目绑定结构无效")
    require(value["project_workspace"] != identity["workspace"] and value["review_head"] == identity["head"],
            "审查项目别名或 HEAD 绑定无效")
    for key in ("project_root", "git_common_dir", "git_dir"):
        path = Path(value[key])
        require(path.is_absolute() and ".." not in path.parts, "审查项目路径必须为规范绝对路径")
    require(Path(value["project_root"]) != root
            and Path(value["git_dir"]).parent == Path(value["git_common_dir"]) / "worktrees",
            "审查必须使用关联项目的独立 worktree")


def bind_worktree(config, workspace, project_workspace, repo, head):
    require(isinstance(project_workspace, str) and project_workspace in config.get("workspaces", {})
            and project_workspace != workspace, "项目必须显式选择另一个已配置工作区")
    require(isinstance(head, str) and re.fullmatch(r"[0-9a-f]{40}", head), "审查 HEAD 无效")
    root = _root(config["workspaces"][workspace]["root"])
    project = _root(config["workspaces"][project_workspace]["root"])
    require(root != project and not root.is_relative_to(project) and not project.is_relative_to(root),
            "审查 worktree 与业务工作区必须相互独立")
    marker = root / ".git"
    require(marker.is_file() and not marker.is_symlink(), "审查工作区必须为已关联项目的 Git worktree")
    common = _common(project)
    git_dir = Path(_git(root, "rev-parse", "--absolute-git-dir")).resolve()
    require(_common(root) == common and git_dir.parent == Path(common) / "worktrees",
            "审查 worktree 不属于指定项目")
    backlink = git_dir / "gitdir"
    require(backlink.is_file() and not backlink.is_symlink()
            and backlink.read_text().strip() == str(marker), "Git worktree 反向绑定无效")
    remote = _git(project, "config", "--local", "--no-includes", "--get", "remote.origin.url", absent_ok=True)
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?", remote)
    require(match is not None and match[1].casefold() == repo.casefold(), "指定项目的 origin 与 PR 仓库不匹配")
    require(_git(root, "symbolic-ref", "-q", "HEAD", absent_ok=True) == "", "审查 worktree 必须为 detached HEAD")
    require(_git(root, "rev-parse", "HEAD") == head, "审查 worktree HEAD 与 PR 固定提交不符")
    rows = _git(root, "ls-files", "--stage", "-z").split("\0")
    require(all(not r.startswith(("120000 ", "160000 ")) for r in rows), "审查 worktree 不支持符号链接或子模块")
    require(not any("\t.asterun-reviews/" in r or r.endswith("\t.asterun-reviews") for r in rows),
            "仓库不能跟踪配方保留目录 .asterun-reviews")
    require(all(not row or row.startswith("H ") for row in _git(root, "ls-files", "-v", "-z").split("\0")),
            "审查 worktree 不能通过 assume-unchanged 或 skip-worktree 隐藏变更")
    require(not _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=matching",
                     "--ignore-submodules=all", "--", ".", ":(exclude).asterun-reviews"),
            "审查 worktree 必须干净，不能混入未提交、未跟踪或忽略的业务文件")
    binding = {"project_workspace": project_workspace, "project_root": str(project), "git_common_dir": common,
               "git_dir": str(git_dir), "review_head": head}
    validate_binding(binding, root, {"workspace": workspace, "head": head})
    return binding
