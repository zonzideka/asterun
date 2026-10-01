"""将已配置的空工作区槽位物化为固定提交的独立 Git 工作树。"""
from __future__ import annotations

import os
import selectors
import subprocess
import time

from asterun.backends.grok_code import CodeProcessHandle
from asterun.contracts import TERMINAL_RUN_STATUSES
from asterun.errors import AsterunError
from asterun.policy import enforce


def _git(root, args):
    env = {"PATH": "/usr/bin:/bin", "LANG": "C", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1",
           "GIT_LFS_SKIP_SMUDGE": "1", "GIT_OPTIONAL_LOCKS": "0"}
    command = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
               "-c", "submodule.recurse=false", "-c", "core.sparseCheckout=false"]
    handle = CodeProcessHandle()
    try:
        proc = handle.spawn([*command, *args], cwd=root, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        out = bytearray()
        total = 0
        deadline = time.monotonic() + 30
        try:
            with selectors.DefaultSelector() as reader:
                reader.register(proc.stdout, selectors.EVENT_READ)
                reader.register(proc.stderr, selectors.EVENT_READ)
                while reader.get_map():
                    if time.monotonic() >= deadline:
                        raise AsterunError("REMOTE_STATE_UNKNOWN", "Git 操作超时，保留已有目录；先核对工作树，不自动重建")
                    for key, _ in reader.select(timeout=0.05):
                        part = os.read(key.fd, 65536)
                        if not part:
                            reader.unregister(key.fd)
                            continue
                        total += len(part)
                        if total > 1024 * 1024:
                            raise AsterunError("REMOTE_STATE_UNKNOWN", "Git 输出超过限额，保留已有目录供核对")
                        if key.fileobj is proc.stdout:
                            out.extend(part)
            proc.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            raise AsterunError("REMOTE_STATE_UNKNOWN", "Git 操作超时，保留已有目录供核对") from exc
        finally:
            proc.stdout.close()
            proc.stderr.close()
        if proc.returncode:
            raise AsterunError("INVALID_REQUEST", "Git 本地校验或工作树创建失败；保留现有目录供核对")
        return out.decode("utf-8").strip()
    finally:
        handle.close()


def materialize(app, payload):
    source_alias, target_alias = payload["source_workspace"], payload["workspace"]
    for alias in (source_alias, target_alias):
        for action in ("workspace.read", "workspace.write", "process.execute"):
            enforce(app.policy.authorize(app.principal, action, alias))
        app._require_grant(alias)
    app._require_applied_config()
    source = app.config.get_workspace(source_alias).root.resolve()
    target = app.config.get_workspace(target_alias).root.resolve()
    if source == target or target.is_relative_to(source) or source.is_relative_to(target):
        raise AsterunError("INVALID_REQUEST", "工作树槽位必须独立且不嵌套")
    for task_id in app.store.list_task_ids():
        task = app.store.get_task(task_id)
        if app._workspace_key(task) in {str(source), str(target)} and task.current_run_id:
            run = app.store.get_run(task.current_run_id)
            if run.status not in TERMINAL_RUN_STATUSES:
                raise AsterunError("INVALID_REQUEST", "相关工作区存在执行中、排队或未决任务")
    if _git(source, ["rev-parse", "--show-toplevel"]) != str(source):
        raise AsterunError("INVALID_REQUEST", "源工作区不是独立 Git 根")
    commit = _git(source, ["rev-parse", "--verify", payload["commit"] + "^{commit}"])
    if commit != payload["commit"]:
        raise AsterunError("INVALID_REQUEST", "需要完整的现有提交 SHA")
    common = _git(source, ["rev-parse", "--path-format=absolute", "--git-common-dir"])
    # 只读取键名，不读取可能包含凭据的配置值。
    entries = _git(source, ["config", "--includes", "--name-only", "--list"]).splitlines()
    filters = [key for key in entries
               if key.startswith("filter.") and key.endswith((".smudge", ".process", ".required"))]
    # 未启用自定义过滤器时没有进程/网络；启用者拒绝，不静默产出失真的文件。
    if filters:
        raise AsterunError("CAPABILITY_UNSUPPORTED", "仓库配置了 checkout 过滤器，需要人工准备并核对工作树")
    if (target / ".git").exists():
        target_keys = _git(target, ["config", "--includes", "--name-only", "--list"]).splitlines()
        if any(key.startswith("filter.") and key.endswith((".smudge", ".process", ".required")) for key in target_keys):
            raise AsterunError("CAPABILITY_UNSUPPORTED", "已有工作树配置了 checkout 过滤器，需要人工核对")
        if (_git(target, ["rev-parse", "--show-toplevel"]) != str(target)
                or _git(target, ["rev-parse", "--path-format=absolute", "--git-common-dir"]) != common
                or _git(target, ["rev-parse", "HEAD"]) != commit
                or _git(target, ["status", "--porcelain"])):
            raise AsterunError("REVISION_CONFLICT", "已有槽位的仓库、提交或工作内容不匹配，不覆盖")
        return {"workspace": target_alias, "commit": commit, "reused": True, "source_dirty_copied": False}
    if any(target.iterdir()):
        raise AsterunError("INVALID_REQUEST", "目标工作区必须为空；不会清理已有内容")
    _git(source, ["worktree", "add", "--detach", str(target), commit])
    if _git(target, ["rev-parse", "HEAD"]) != commit or _git(target, ["status", "--porcelain"]):
        raise AsterunError("REMOTE_STATE_UNKNOWN", "工作树创建后核对不一致，保留目录供检查")
    return {"workspace": target_alias, "commit": commit, "reused": False, "detached": True, "source_dirty_copied": False}
