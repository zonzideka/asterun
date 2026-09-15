"""外部主控的固定版本报告；校验内容绑定，不把自报结果升级为独立审查。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat

from asterun.errors import AsterunError, INVALID_REQUEST
from asterun.contracts import TERMINAL_RUN_STATUSES
from asterun.quality import MAX_BYTES, _open_file, snapshot


def _invalid(message: str) -> AsterunError:
    return AsterunError(INVALID_REQUEST, message)


def _bound_run(task, run) -> None:
    if (run is None or run.task_id != task.id or task.current_run_id != run.id):
        raise _invalid("外部验收必须绑定任务的当前运行")
    if run.status not in TERMINAL_RUN_STATUSES:
        raise _invalid("外部验收需要已确认终态的运行，不能读取执行中或未决版本作为验收目标")


def prepare_snapshot(task, run, workspace_root, paths, state_dir=None) -> dict:
    """只返回现有快照协议的 manifest，不返回文件正文或改变验收状态。"""
    _bound_run(task, run)
    if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
        raise _invalid("target_paths 必须是相对文件路径数组")
    target, _ = snapshot(Path(workspace_root), paths,
                         state_dir=None if state_dir is None else Path(state_dir))
    return {"task_id": task.id.value, "run_id": run.id.value,
            "revision": task.config_revision, "input_hash": task.input_hash,
            "target_paths": sorted(paths), "target_hash": target["hash"], "target": target}


def _read_report(workspace_root, path: str, sha256: str) -> bytes:
    if (not isinstance(path, str) or not isinstance(sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", sha256) is None):
        raise _invalid("外部报告需要规范相对路径与小写 SHA-256")
    relative = Path(path)
    if (relative.is_absolute() or str(relative) != path or not relative.parts
            or any(part in {"..", ".git"} for part in relative.parts)):
        raise _invalid("外部报告必须是工作区内的规范相对文件路径")
    try:
        descriptor = _open_file(Path(workspace_root).resolve(), relative)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_BYTES:
                raise _invalid("外部报告必须是最多 256 KiB 的普通文件")
            raw = stream.read(MAX_BYTES + 1)
            after = os.fstat(stream.fileno())
        if (len(raw) > MAX_BYTES or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise _invalid("外部报告读取期间变化或超过大小限制")
    except OSError as exc:
        raise _invalid("无法安全读取工作区内的外部报告") from exc
    if hashlib.sha256(raw).hexdigest() != sha256:
        raise _invalid("外部报告 SHA-256 与调用方指定版本不一致")
    return raw


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def check_external_report(spec, task, run, target_hash, workspace_root) -> dict:
    """验证严格报告与运行/输入/目标的绑定；结果始终保留外部自报来源。"""
    _bound_run(task, run)
    if (not isinstance(spec, dict) or set(spec) != {"kind", "path", "sha256"}
            or spec["kind"] != "external_report"):
        raise _invalid("external_report 检查只接受 kind、path、sha256")
    raw = _read_report(workspace_root, spec["path"], spec["sha256"])
    try:
        report = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs)
        if (not isinstance(report, dict)
                or set(report) != {"task_id", "run_id", "input_hash", "target_hash", "checks"}
                or report["task_id"] != task.id.value or report["run_id"] != run.id.value
                or report["input_hash"] != task.input_hash or report["target_hash"] != target_hash):
            raise ValueError("invalid report binding")
        checks = report["checks"]
        if not isinstance(checks, list) or not 1 <= len(checks) <= 64:
            raise ValueError("invalid checks")
        names = set()
        for check in checks:
            if (not isinstance(check, dict) or set(check) != {"name", "passed", "detail"}
                    or not isinstance(check["name"], str) or not check["name"].strip()
                    or len(check["name"]) > 200 or check["name"] in names
                    or type(check["passed"]) is not bool or not isinstance(check["detail"], str)
                    or len(check["detail"]) > 4000):
                raise ValueError("invalid check")
            names.add(check["name"])
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise _invalid("外部报告格式或版本绑定无效，不能作为当前运行的验收证据") from exc
    failed = [check["name"] for check in checks if not check["passed"]]
    detail = f"外部报告自报 {len(checks) - len(failed)}/{len(checks)} 项检查通过"
    if failed:
        detail += "；未通过：" + "、".join(failed)
    return {"passed": not failed, "detail": detail,
            "report": {"path": spec["path"], "sha256": spec["sha256"]},
            "source": "external_reported"}


def validate_external_evaluation(task, run, workspace_root, state_dir=None) -> bool:
    """只读复核持久化证据；缺失、损坏或漂移均返回 False，由调用方决定失效处理。"""
    evidence = getattr(task, "external_evaluation", None)
    if not isinstance(evidence, dict) or not evidence:
        return False
    try:
        _bound_run(task, run)
        if (evidence["expected_run_id"] != run.id.value
                or type(evidence["expected_revision"]) is not int
                or evidence["expected_revision"] != task.config_revision
                or evidence["expected_input_hash"] != task.input_hash):
            return False
        current = prepare_snapshot(task, run, workspace_root, evidence["target_paths"], state_dir)
        if current["target_hash"] != evidence["target_hash"]:
            return False
        reports = evidence["reports"]
        if not isinstance(reports, list) or len(reports) > 64:
            return False
        for report in reports:
            if not isinstance(report, dict) or set(report) != {"path", "sha256"}:
                return False
            check_external_report({"kind": "external_report", **report}, task, run,
                                  current["target_hash"], workspace_root)
        # 报告读取期间目标也可能被外部执行器改变。
        return prepare_snapshot(task, run, workspace_root, evidence["target_paths"], state_dir) == current
    except (AsterunError, OSError, UnicodeError, KeyError, TypeError, ValueError, RecursionError):
        return False
