"""可选原生项目登记。只改展示元数据，不创建或续接执行任务。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from asterun.transport import JsonRpcError


class _Unavailable(Exception):
    def __init__(self, code: str, *, status: str = "unsupported"):
        self.code = code
        self.status = status


class CodexProjects:
    """项目根来自执行机真实路径，名字和 Git remote 不参与身份判断。

    原生 API 没有 metadata CAS；更新前重读并拒绝已有冲突，更新后回读。
    native_home 必须由调用方传入该 transport 的实际 CODEX_HOME。
    """

    def __init__(self, transport, cwd, *, native_home, timeout_seconds=10.0, max_pages=100,
                 prepare_project=None):
        self.transport = transport
        self.cwd = cwd
        self.native_home = native_home
        self.timeout_seconds = timeout_seconds
        self.max_pages = max_pages
        self.prepare_project = prepare_project

    def present(self, thread_id: str) -> dict[str, Any]:
        self._result = {
            "registration": "not_attempted", "association": "not_attempted",
            "client_visibility": "not_verified", "project_id": None,
            "project_root": None, "native_cwd": None, "thread_id": thread_id,
            "reason": None,
        }
        self._stage = "binding"
        try:
            if (not isinstance(thread_id, str) or not thread_id
                    or isinstance(self.timeout_seconds, bool)
                    or not math.isfinite(float(self.timeout_seconds)) or self.timeout_seconds <= 0
                    or isinstance(self.max_pages, bool) or not isinstance(self.max_pages, int)
                    or self.max_pages <= 0):
                raise _Unavailable("invalid_presentation_input")
            self._deadline = time.monotonic() + min(float(self.timeout_seconds), 60.0)
            self._cwd = self._directory(self.cwd)
            self._home = self._directory(self.native_home)
            self._result["native_cwd"] = str(self._cwd)
            before = self._thread(thread_id)
            self._stage = "identity"
            self._root = self._project_root()
            self._result["project_root"] = str(self._root)
            assigned = before.get("projectId")
            if self.prepare_project is not None:
                self._stage = "prepare_desktop_project"
                try:
                    receipt = self.prepare_project(self._root, self._remaining())
                except Exception:
                    raise _Unavailable("desktop_registration_failed", status="unknown") from None
                if not isinstance(receipt, dict):
                    raise _Unavailable("invalid_desktop_receipt", status="unknown")
                self._result["desktop"] = receipt
                if receipt.get("registration") != "registered":
                    status = receipt.get("registration")
                    raise _Unavailable("desktop_registration_unconfirmed", status=(
                        status if status in {"unsupported", "unknown", "conflict"} else "unknown"))
                self._remaining()
                project_id = receipt.get("project_id")
                if (not isinstance(project_id, str) or not project_id
                        or not isinstance(receipt.get("desktop_project_id"), str)
                        or not receipt["desktop_project_id"]):
                    raise _Unavailable("invalid_desktop_receipt", status="unknown")
                self._stage = "read_desktop_project"
                self._result["project_id"] = project_id
                project = self._read_project(project_id)
                self._require_root(project)
                self._result["registration"] = "existing"
                if assigned:
                    if assigned != project_id:
                        raise _Unavailable("thread_project_conflict", status="conflict")
                    self._result["association"] = "existing"
                    return self._result
                # Desktop 返回的准确 ID 优先于同根列表；不重建或猜 legacy 映射。
            else:
                self._stage = "list"
                projects = self._list()
                if assigned:
                    self._stage = "read_project"
                    self._result.update(registration="existing", project_id=assigned)
                    project = self._read_project(assigned)
                    self._require_root(project)
                    self._result.update(registration="existing", association="existing", project_id=assigned)
                    return self._result
                project = self._select(projects)
            if project is None:
                self._stage = "create"
                self._result["registration"] = "unknown"
                key = "asterun-project-v1-" + hashlib.sha256(json.dumps(
                    [str(self._home), str(self._root)], ensure_ascii=True,
                    separators=(",", ":"),
                ).encode("ascii")).hexdigest()
                try:
                    response = self._request("project/create", {
                        "idempotencyKey": key, "name": self._root.name or str(self._root),
                        "roots": [{"path": str(self._root)}],
                    })
                    project = self._project(response.get("project"))
                    self._require_root(project)
                    self._result["registration"] = "created"
                except (TimeoutError, ConnectionError, OSError, JsonRpcError) as exc:
                    if isinstance(exc, JsonRpcError) and exc.code != -32603:
                        raise
                    # 未知写入只完整重查；后续调用仍使用同一键，绝不换键补发。
                    self._stage = "reconcile_create"
                    project = self._select(self._list())
                    if project is None:
                        raise _Unavailable("project_create_unconfirmed", status="unknown")
                    self._result["registration"] = "reconciled"
            else:
                self._result["registration"] = "existing"
            project_id = project["id"]
            self._result["project_id"] = project_id
            self._stage = "pre_association"
            current = self._thread(thread_id)
            self._unchanged(before, current)
            if current.get("projectId"):
                if current["projectId"] != project_id:
                    raise _Unavailable("thread_project_changed", status="conflict")
                self._require_root(self._read_project(project_id))
                self._result["association"] = "existing"
                return self._result
            # 防止列表读取后项目被改成多根或另一个目录。
            self._require_root(self._read_project(project_id))
            self._stage = "associate"
            self._result["association"] = "unknown"
            try:
                self._request("thread/metadata/update", {"threadId": thread_id, "projectId": project_id})
            except (TimeoutError, ConnectionError, OSError, JsonRpcError) as exc:
                if isinstance(exc, JsonRpcError) and exc.code != -32603:
                    raise
                self._stage = "reconcile_association"
            self._stage = "readback"
            after = self._thread(thread_id)
            self._unchanged(before, after)
            if after.get("projectId") != project_id:
                raise _Unavailable("thread_association_unconfirmed", status="unknown")
            self._require_root(self._read_project(project_id))
            self._result["association"] = "associated"
        except _Unavailable as exc:
            self._fail(exc.code, exc.status)
        except JsonRpcError as exc:
            unsupported = exc.code == -32601 or (
                exc.code == -32600 and "experimental" in str(exc).lower())
            self._fail("native_project_api_unsupported" if unsupported else "native_request_failed",
                       "unsupported" if unsupported else "unknown")
        except (TimeoutError, ConnectionError, OSError):
            self._fail("presentation_unconfirmed", "unknown")
        except Exception:
            # 原生错误可能带配置/私有字段；展示报告不转发异常原文。
            self._fail("invalid_native_response", "unknown")
        return self._result

    def _fail(self, code, status):
        field = "association" if self._result["registration"] in {"existing", "created", "reconciled"} else "registration"
        self._result[field] = status
        self._result["reason"] = {"code": code, "stage": self._stage}

    def _remaining(self):
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("presentation deadline")
        return remaining

    def _request(self, method, params):
        result = self.transport.request(method, params, timeout=self._remaining())
        self._remaining()
        if not isinstance(result, dict):
            raise _Unavailable("invalid_native_response", status="unknown")
        return result

    @staticmethod
    def _directory(value):
        path = Path(value)
        if not path.is_absolute():
            raise _Unavailable("absolute_path_required")
        path = path.resolve(strict=True)
        if not path.is_dir():
            raise _Unavailable("directory_required")
        return path

    def _thread(self, thread_id):
        thread = self._request("thread/read", {"threadId": thread_id, "includeTurns": False}).get("thread")
        if not isinstance(thread, dict) or thread.get("id") != thread_id:
            raise _Unavailable("thread_identity_mismatch", status="conflict")
        if self._directory(thread.get("cwd")) != self._cwd:
            raise _Unavailable("thread_cwd_mismatch", status="conflict")
        project_id = thread.get("projectId")
        if project_id is not None and not isinstance(project_id, str):
            raise _Unavailable("invalid_thread_project", status="unknown")
        return thread

    @staticmethod
    def _unchanged(before, after):
        # thread/read 不暴露所有有效权限，不能宣称仅据此完成权限验收。
        for key in ("environments", "runtimeWorkspaceRoots", "approvalPolicy", "sandbox",
                    "sandboxPolicy", "activePermissionProfile"):
            if (key in before) != (key in after) or before.get(key) != after.get(key):
                raise _Unavailable("thread_execution_context_changed", status="conflict")

    @staticmethod
    def _project(project):
        if (not isinstance(project, dict) or not isinstance(project.get("id"), str)
                or not project["id"] or not isinstance(project.get("roots"), list)
                or not project["roots"]):
            raise _Unavailable("invalid_project_response", status="unknown")
        for root in project["roots"]:
            if (not isinstance(root, dict) or not isinstance(root.get("path"), str)
                    or not Path(root["path"]).is_absolute()):
                raise _Unavailable("invalid_project_response", status="unknown")
        return project

    def _read_project(self, project_id):
        project = self._project(self._request("project/read", {"projectId": project_id}).get("project"))
        if project["id"] != project_id:
            raise _Unavailable("project_identity_mismatch", status="conflict")
        return project

    def _roots(self, project):
        return [Path(item["path"]).resolve() for item in project["roots"]]

    def _require_root(self, project):
        roots = self._roots(project)
        if roots != [self._root]:
            raise _Unavailable("project_roots_conflict", status="conflict")

    def _select(self, projects):
        matches = [project for project in projects if self._root in self._roots(project)]
        if len(matches) > 1:
            raise _Unavailable("ambiguous_project_roots", status="conflict")
        if matches:
            self._require_root(matches[0])
            return matches[0]
        return None

    def _list(self):
        projects, cursors, ids = [], set(), set()
        cursor = None
        for _ in range(min(self.max_pages, 1000)):
            params = {"limit": 100}
            if cursor is not None:
                params["cursor"] = cursor
            response = self._request("project/list", params)
            page = response.get("data")
            if not isinstance(page, list):
                raise _Unavailable("invalid_project_page", status="unknown")
            for item in page:
                project = self._project(item)
                if project["id"] in ids:
                    raise _Unavailable("project_pagination_changed", status="unknown")
                ids.add(project["id"])
                projects.append(project)
            cursor = response.get("nextCursor")
            if cursor is None:
                return projects
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise _Unavailable("invalid_project_cursor", status="unknown")
            cursors.add(cursor)
        raise _Unavailable("project_pagination_incomplete", status="unknown")

    def _git(self, cwd, *args):
        # Git 配置/环境不能把发现命令重定向到别的仓库或启动 fsmonitor。
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS="0")
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + os.devnull,
             "-C", str(cwd), *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env, timeout=self._remaining(), check=False,
        )
        self._remaining()
        if result.returncode != 0:
            raise _Unavailable("repository_identity_unverified")
        return os.fsdecode(result.stdout).rstrip("\n")

    def _project_root(self):
        has_marker = any((path / ".git").exists() or (path / ".git").is_symlink()
                         for path in (self._cwd, *self._cwd.parents))
        try:
            top = self._directory(self._git(self._cwd, "rev-parse", "--show-toplevel"))
        except (FileNotFoundError, _Unavailable):
            if has_marker:
                raise _Unavailable("repository_identity_unverified")
            return self._cwd
        git_dir = self._directory(self._git(top, "rev-parse", "--absolute-git-dir"))
        common = self._directory(self._git(top, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        raw = self._git(top, "worktree", "list", "--porcelain", "-z")
        # prunable 的无关旧 worktree 可以已经不存在；只要求本次路径和主根存在。
        roots = [Path(item[9:]).resolve() for item in raw.split("\0") if item.startswith("worktree ")]
        if top not in roots or not roots:
            raise _Unavailable("worktree_identity_unverified")
        main = self._directory(roots[0])
        if (self._directory(self._git(main, "rev-parse", "--absolute-git-dir")) != common
                or self._directory(self._git(main, "rev-parse", "--show-toplevel")) != main):
            raise _Unavailable("worktree_identity_unverified")
        if git_dir != common:
            # 同时检查 Git 的正向列表和该 worktree 的反向 .git 指针。
            pointer = git_dir / "gitdir"
            if (not pointer.is_file() or pointer.is_symlink() or
                    Path(pointer.read_text().rstrip("\n")).resolve(strict=True) != (top / ".git").resolve(strict=True)):
                raise _Unavailable("worktree_identity_unverified")
        return main
