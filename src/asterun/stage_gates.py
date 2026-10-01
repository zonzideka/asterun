"""持久化阶段要求：只能收紧，来源与验收状态分别保存。"""
from __future__ import annotations

from copy import deepcopy

from asterun.contracts import AcceptanceStatus
from asterun.errors import AsterunError
from asterun.quality import digest

LOCAL_STAGES = {"coding", "consumer", "integration"}
EXTERNAL_STAGES = {"consumer", "integration", "deployment"}


def validate_policy(value):
    if not isinstance(value, dict) or set(value) - (LOCAL_STAGES | {"independent_review", "deployment"}):
        raise AsterunError("INVALID_CONFIG", "stage_gates 必须指定已知阶段及允许的证据来源")
    for stage, source in value.items():
        allowed = {"core", "external_reported"} if stage in EXTERNAL_STAGES else {"core"}
        if stage == "deployment":
            allowed = {"external_reported"}
        if not isinstance(source, str) or source not in allowed:
            raise AsterunError("INVALID_CONFIG", "编码和独立审查要求核心证据；部署仅支持明确允许的外部回执")
    return value


def policy_for(app, task):
    policy = deepcopy(task.stage_gate.get("policy", {}))
    for stage, source in app.config.workflow.stage_gates.items():
        previous = policy.get(stage, {})
        if previous.get("source") == "core":
            source = "core"
        names = sorted(set(previous.get("checks", [])) | {
            name for name, spec in app.config.workflow.local_checks.items()
            if spec.get("stage", "coding") == stage and source == "core"})
        policy[stage] = {"source": source, "checks": names}
    return policy


def seed(app, task):
    policy = policy_for(app, task)
    if policy:
        task.stage_gate = {"policy": policy, "satisfied": False, "status": "unverified"}


def quality_bound(task, run):
    return {"task_id": task.id.value, "run_id": run.id.value, "revision": task.config_revision,
            "input_hash": task.input_hash, "target_hash": task.quality["target"]["hash"],
            "target_paths": task.quality["target_paths"], "target": task.quality["target"]}


def apply_gate(app, task, run, bound):
    from asterun.workflow_evidence import evidence
    policy = policy_for(app, task)
    if not policy:
        return task
    if bound is None:
        raise AsterunError("INVALID_REQUEST", "阶段门禁需要完整固定版本绑定")
    task.stage_gate = {**task.stage_gate, "policy": policy}
    report = evidence(app, task, run, bound)
    app._bound_external_target(task, run, {
        "expected_run_id": bound["run_id"], "expected_revision": bound["revision"],
        "expected_input_hash": bound["input_hash"], "expected_target_hash": bound["target_hash"],
        "target_paths": bound["target_paths"],
    }, required=True)
    states = {}
    for stage, rule in policy.items():
        row = report["stages"][stage]
        trusted = (row.get("source") in {"isolated_local_check", "core_review"}
                   or rule["source"] == "external_reported" and row.get("source") == "external_reported")
        states[stage] = {"status": row["status"], "source": row.get("source"),
                         "satisfied": row["status"] == "passed" and trusted}
    satisfied = all(row["satisfied"] for row in states.values())
    task.stage_gate = {"policy": policy, "satisfied": satisfied,
                       "status": "passed" if satisfied else "failed" if any(row["status"] == "failed" for row in states.values()) else "pending",
                       "bound": {key: deepcopy(bound[key]) for key in ("task_id", "run_id", "revision", "input_hash", "target_hash", "target_paths")},
                       "states": states}
    task.findings = [f for f in task.findings if f.get("kind") != "stage_gate"]
    for stage, row in states.items():
        if not row["satisfied"]:
            stage_details = report["stages"][stage]
            diagnostics = [d["sample"] for check in stage_details.get("checks", []) for d in check.get("diagnostics", [])]
            summary = f"{stage} 阶段未满足：{row['status']}，来源 {row['source']}"
            if diagnostics:
                summary += "；" + "；".join(diagnostics)[:1200]
            task.findings.append({"kind": "stage_gate", "stage": stage, "summary": summary,
                                  "status": "open", "target_hash": bound["target_hash"],
                                  "source_run_id": run.id.value, "id": digest([stage, bound["target_hash"], summary])})
    if not satisfied and task.acceptance == AcceptanceStatus.PASSED:
        task.acceptance = AcceptanceStatus.FAILED if task.stage_gate["status"] == "failed" else AcceptanceStatus.PENDING
        task.evidence_input_hash = ""
    return task


def revalidate(app, task, run):
    completed_review = (task.quality.get("phase") == "completed" and task.review_status == "passed"
                        and not task.evidence_stale and not task.paused)
    if not policy_for(app, task) or run is None or (task.acceptance != AcceptanceStatus.PASSED and not completed_review):
        return task
    # 调用方已先复核核心质量目标；补回同版本阶段证据不需要再花一次审查轮次。
    if completed_review:
        task.acceptance = AcceptanceStatus.PASSED
        task.evidence_input_hash = task.input_hash
    bound = task.stage_gate.get("bound")
    if bound is None and task.quality:
        bound = quality_bound(task, run)
    if bound is None and task.external_evaluation:
        prior = task.external_evaluation
        bound = {"task_id": task.id.value, "run_id": prior["expected_run_id"], "revision": prior["expected_revision"],
                 "input_hash": prior["expected_input_hash"], "target_hash": prior["target_hash"], "target_paths": prior["target_paths"]}
    task.stage_gate["policy"] = policy_for(app, task)
    try:
        if not bound:
            raise AsterunError("REVISION_CONFLICT", "阶段绑定缺失")
        payload = {"expected_run_id": bound["run_id"], "expected_revision": bound["revision"],
                   "expected_input_hash": bound["input_hash"], "expected_target_hash": bound["target_hash"],
                   "target_paths": bound["target_paths"]}
        app._bound_external_target(task, run, payload, required=True)
        apply_gate(app, task, run, bound)
    except (AsterunError, OSError, UnicodeError):
        task.acceptance = AcceptanceStatus.PENDING
        task.evidence_input_hash = ""
        task.stage_gate = {**task.stage_gate, "satisfied": False, "status": "stale"}
    app.store.save_task(task)
    return task
