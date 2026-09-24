"""显式选择文件的私有恢复产物，不修改工作区、Git 索引或原生会话。"""
from __future__ import annotations

import difflib
import json
import subprocess

from asterun.errors import AsterunError
from asterun.quality import snapshot


def _write(app, document):
    content = json.dumps(document, ensure_ascii=False, sort_keys=True).encode()
    if len(content) > 4 * 1024 * 1024:
        raise AsterunError("INVALID_REQUEST", "检查点产物超过 4 MiB，请缩小目标")
    return app.store.put_checkpoint_blob(content)


def _read(app, sha):
    return json.loads(app.store.get_checkpoint_blob(sha))


def _binding(app, task, run):
    return {"task_id": task.id.value, "run_id": run.id.value, "input_hash": task.input_hash,
            "config_revision": task.config_revision, "account_ref": task.account_ref.value,
            "runtime_ref": task.runtime_ref.value, "workspace": task.workspace,
            "conversation_id": run.conversation_id.value if run.conversation_id else None}


def before_dispatch(app, task, run):
    paths = app.config.workflow.checkpoint_paths.get(task.workspace)
    if not paths or run.role == "review":
        return
    from asterun.policy import enforce
    enforce(app.policy.authorize(app.principal, "workspace.read", task.workspace))
    root = app.config.get_workspace(task.workspace).root
    target, files = snapshot(root, paths, state_dir=app.state_dir)
    prior = run.native.get("checkpoint")
    if prior:
        document = _read(app, prior["baseline_sha256"])
        if document["target"] != target or document["binding"] != _binding(app, task, run):
            raise AsterunError("REVISION_CONFLICT", "派发前检查点已漂移，未派发后端")
        return
    dirty = None
    if target["git_head"]:
        command = ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "status",
                   "--porcelain=v1", "-z", "--untracked-files=all", "--", *paths]
        result = subprocess.run(command, cwd=root, capture_output=True, timeout=5, check=False)
        if result.returncode or len(result.stdout) > 65536:
            raise AsterunError("INVALID_REQUEST", "无法取得检查点范围内的 Git dirty 清单")
        dirty = result.stdout.decode("utf-8").split("\0")[:-1]
    document = {"version": 1, "binding": _binding(app, task, run), "target": target,
                "files": files, "dirty_at_baseline": dirty, "scope": "selected_files_only"}
    if snapshot(root, paths, state_dir=app.state_dir)[0] != target:
        raise AsterunError("REVISION_CONFLICT", "保存基线期间源码改变")
    sha = _write(app, document)
    run.native["checkpoint"] = {"status": "baseline", "baseline_sha256": sha, "latest_sha256": sha,
                                "target_hash": target["hash"], "scope": "selected_files_only",
                                "next_action": "等待运行终态；未决运行先对账"}
    app.store.save_run(run)


def after_result(app, run, status):
    checkpoint = run.native.get("checkpoint")
    if not checkpoint or status not in {"succeeded", "failed", "cancelled", "pending_reconcile"}:
        return
    task = app.store.get_task(run.task_id)
    baseline = _read(app, checkpoint["baseline_sha256"])
    paths = [f["path"] for f in baseline["files"]]
    target, files = snapshot(app.config.get_workspace(task.workspace).root, paths, state_dir=app.state_dir)
    old = {f["path"]: f for f in baseline["files"]}
    patch = []
    changes = []
    for item in files:
        before = old[item["path"]]
        if item != before:
            changes.append({"path": item["path"], "before_sha256": before.get("sha256"),
                            "after_sha256": item.get("sha256"), "new_file": bool(before.get("missing")),
                            "deleted": bool(item.get("missing")), "mode": item.get("mode")})
            patch.extend(difflib.unified_diff(before.get("content", "").splitlines(keepends=True),
                         item.get("content", "").splitlines(keepends=True),
                         fromfile="a/" + item["path"], tofile="b/" + item["path"]))
    document = {"version": 1, "binding": baseline["binding"], "baseline_sha256": checkpoint["baseline_sha256"],
                "target": target, "files": files, "changes": changes, "patch": "".join(patch),
                "native_session_id": run.native.get("backend_session_id", run.native.get("session_id")),
                "run_status": status, "scope": "selected_files_only"}
    if snapshot(app.config.get_workspace(task.workspace).root, paths, state_dir=app.state_dir)[0] != target:
        raise AsterunError("REVISION_CONFLICT", "保存检查点期间源码改变")
    sha = _write(app, document)
    run.native["checkpoint"] = {**checkpoint, "status": "captured", "latest_sha256": sha,
                                "target_hash": target["hash"], "changed_files": len(changes),
                                "next_action": "先对账，禁止重派" if status == "pending_reconcile" else "运行已结束，执行绑定检查和独立验收"}


def inspect(app, task, run, *, include_artifact=False):
    checkpoint = run.native.get("checkpoint")
    if not checkpoint:
        raise AsterunError("NOT_FOUND", "该运行没有配置检查点")
    artifact = _read(app, checkpoint["latest_sha256"])
    if artifact["binding"] != _binding(app, task, run):
        raise AsterunError("BINDING_MISMATCH", "检查点任务身份不匹配")
    current, _ = snapshot(app.config.get_workspace(task.workspace).root,
                          [f["path"] for f in artifact["files"]], state_dir=app.state_dir)
    matches = current == artifact["target"]
    jobs = run.native.get("local_checks", {})
    last = list(jobs.values())[-1] if jobs else None
    value = {**checkpoint, "source_matches": matches, "run_status": run.status.value,
             "native_session_id": artifact.get("native_session_id"),
             "last_check": {"job_id": last["job_id"], "status": last["status"]} if last else None,
             "resume_dispatched": False, "workspace_restored": False,
             "recovery_ready": matches and checkpoint["status"] == "captured"
                 and run.status.value in {"succeeded", "failed", "cancelled"} and task.current_run_id == run.id}
    if not matches:
        value["next_action"] = "源码已漂移，先比较私有产物；不会覆盖工作区或重派"
    if include_artifact:
        # IPC 有 1 MiB 上限，原件仍随数据库备份保存。
        if len(json.dumps(artifact, ensure_ascii=False).encode()) > 512 * 1024:
            raise AsterunError("INVALID_REQUEST", "检查点大于单次 IPC 产物上限；原件保留在状态备份中")
        value["artifact"] = artifact
    return value
