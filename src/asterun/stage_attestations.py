"""固定报告的现场回执导入。保留外部自报来源，绝不据此执行部署。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
import re

from asterun.errors import AsterunError
from asterun.external_evaluation import _read_report, _unique_pairs
from asterun.quality import digest
from asterun.stage_gates import EXTERNAL_STAGES


def read_report(root, spec, bound, stage):
    raw = _read_report(root, spec["path"], spec["sha256"])
    try:
        report = json.loads(raw, object_pairs_hook=_unique_pairs)
        keys = {"task_id", "run_id", "input_hash", "target_hash", "revision", "stage", "environment",
                "artifact_sha256", "release_id", "checks"}
        if not isinstance(report, dict) or set(report) != keys or stage not in EXTERNAL_STAGES:
            raise ValueError()
        if any(report[key] != bound[key] for key in ("task_id", "run_id", "input_hash", "target_hash", "revision")) or report["stage"] != stage:
            raise ValueError()
        if type(report["revision"]) is not int:
            raise ValueError()
        for key in ("environment", "release_id"):
            value = report[key]
            if not isinstance(value, str) or not 1 <= len(value) <= 160 or any(ord(c) < 32 for c in value):
                raise ValueError()
        if not isinstance(report["artifact_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", report["artifact_sha256"]):
            raise ValueError()
        checks = report["checks"]
        if not isinstance(checks, list) or not 1 <= len(checks) <= 32:
            raise ValueError()
        names = set()
        passed = True
        for check in checks:
            if (not isinstance(check, dict) or set(check) - {"name", "passed", "detail", "counts"}
                    or not {"name", "passed", "detail"}.issubset(check)
                    or not isinstance(check["name"], str) or not 1 <= len(check["name"]) <= 100
                    or check["name"] in names or type(check["passed"]) is not bool
                    or not isinstance(check["detail"], str) or len(check["detail"]) > 2000):
                raise ValueError()
            names.add(check["name"])
            valid_counts = True
            if "counts" in check:
                counts = check["counts"]
                if (not isinstance(counts, dict) or set(counts) != {"passed", "failed", "skipped", "load_errors"}
                        or any(type(n) is not int or n < 0 for n in counts.values())):
                    raise ValueError()
                valid_counts = counts["passed"] > 0 and counts["failed"] == 0 and counts["load_errors"] == 0
            passed = passed and check["passed"] and valid_counts
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        raise AsterunError("INVALID_REQUEST", "阶段回执格式或固定版本绑定不匹配") from exc
    return {"status": "passed" if passed else "failed", "source": "external_reported", "independently_verified": False,
            "environment": report["environment"], "release_id": report["release_id"],
            "artifact_sha256": report["artifact_sha256"], "checks_count": len(checks)}


def record(app, task, run, payload, bound):
    stage = payload["stage"]
    entries = deepcopy(run.native.get("stage_attestations", {}))
    key = digest(payload["idempotency_key"])
    request_hash = digest({k: v for k, v in payload.items() if k != "idempotency_key"})
    old = entries.get(key)
    if old and old["request_hash"] != request_hash:
        raise AsterunError("IDEMPOTENCY_CONFLICT", "阶段回执幂等键已绑定其他内容")
    root = app.config.get_workspace(task.workspace).root
    report = read_report(root, payload["report"], bound, stage)
    app._bound_external_target(task, run, payload, required=True)
    if old:
        return {**old["result"], "reused": True, "acceptance_unchanged": True, "deployment_executed": False}
    if len(entries) >= 32:
        raise AsterunError("RATE_LIMITED", "单次运行最多保存 32 份阶段回执")
    entries[key] = {"stage": stage, "request_hash": request_hash, "report": deepcopy(payload["report"]),
                    "bound": {k: deepcopy(bound[k]) for k in ("task_id", "run_id", "revision", "input_hash", "target_hash", "target_paths")},
                    "result": report, "recorded_by": app.principal.subject_id}
    app.store.save_run(replace(run, native={**run.native, "stage_attestations": entries}))
    return {**report, "reused": False, "acceptance_unchanged": True, "deployment_executed": False}


def latest(app, task, runs, bound, stage):
    entries = [entry for run in runs for entry in run.native.get("stage_attestations", {}).values() if entry["stage"] == stage]
    if not entries:
        return None
    entry = entries[-1]
    expected = entry["bound"]
    if any(expected[k] != bound[k] for k in ("task_id", "revision", "input_hash", "target_hash", "target_paths")):
        return {"status": "stale", "source": "external_reported", "independently_verified": False}
    try:
        return read_report(app.config.get_workspace(task.workspace).root, entry["report"], expected, stage)
    except (AsterunError, OSError, UnicodeError):
        return {"status": "stale", "source": "external_reported", "independently_verified": False}
