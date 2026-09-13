"""可选质量工作流。运行成功不等于验收通过。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from asterun.config import WorkflowConfig
from asterun.contracts import AcceptanceStatus, Run, Task
from asterun.errors import INVALID_REQUEST, REVISION_CONFLICT, AsterunError
from asterun.paths import resolve_within


@dataclass
class CheckResult:
    kind: str
    passed: bool
    detail: str
    bound_revision: int
    bound_input_hash: str


@dataclass
class Evaluation:
    acceptance: AcceptanceStatus
    review_status: str
    findings: list[dict[str, Any]] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)
    bound_revision: int = 1
    bound_input_hash: str = ""
    paused_for_review: bool = False
    used_substitute: bool = False
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "acceptance": str(self.acceptance),
            "review_status": self.review_status,
            "findings": list(self.findings),
            "checks": [
                {
                    "kind": item.kind,
                    "passed": item.passed,
                    "detail": item.detail,
                    "bound_revision": item.bound_revision,
                    "bound_input_hash": item.bound_input_hash,
                }
                for item in self.checks
            ],
            "bound_revision": self.bound_revision,
            "bound_input_hash": self.bound_input_hash,
            "paused_for_review": self.paused_for_review,
            "used_substitute": self.used_substitute,
            "run_success_is_not_acceptance": True,
            "message": self.message,
        }


def evaluate_quality(
    task: Task,
    run: Run | None,
    *,
    config: WorkflowConfig,
    workspace_root,
    payload: dict[str, Any],
    review_available: bool | None,
) -> Evaluation:
    expected_revision = payload.get("expected_revision", task.config_revision)
    expected_hash = payload.get("expected_input_hash", task.input_hash)
    if int(expected_revision) != task.config_revision:
        raise AsterunError(
            REVISION_CONFLICT,
            "验收绑定的配置 revision 与任务不一致",
            details={"expected": expected_revision, "task": task.config_revision},
        )
    if str(expected_hash) != task.input_hash:
        raise AsterunError(
            INVALID_REQUEST,
            "验收绑定的 input_hash 与任务不一致，旧证据不能用于新输入",
            details={"expected": expected_hash, "task": task.input_hash},
        )
    if task.evidence_stale:
        raise AsterunError(
            INVALID_REQUEST,
            "证据已作废，不能把旧验收带到新版本",
            next_action="重新检查当前 revision/input_hash 后再评价",
        )
    bound_revision = task.config_revision
    bound_hash = task.input_hash
    checks: list[CheckResult] = []
    findings: list[dict[str, Any]] = []

    raw_checks = payload.get("checks")
    if raw_checks is None:
        raw_checks = []
    if not isinstance(raw_checks, list):
        raise AsterunError(INVALID_REQUEST, "checks 必须是数组")
    for item in raw_checks:
        if not isinstance(item, dict) or "kind" not in item:
            raise AsterunError(INVALID_REQUEST, "每个 check 需要 kind")
        result = _run_check(item, task, run, workspace_root, bound_revision, bound_hash)
        checks.append(result)
        if not result.passed:
            findings.append(
                {
                    "kind": result.kind,
                    "summary": result.detail,
                    "bound_revision": bound_revision,
                    "bound_input_hash": bound_hash,
                    "status": "open",
                }
            )

    require_review = config.require_review or payload.get("require_review", False)
    review_status = "not_configured"
    paused = False
    used_substitute = False
    if require_review:
        # 可调用性与替代者配置均不是审查结果；尚无受信的审查执行/证据接口。
        review_status = "pending" if review_available is True or config.approved_substitute else "unavailable"
        paused = True
        findings.append({
            "kind": "review_not_executed", "summary": "尚无绑定当前运行的审查证据，已暂停自动推进",
            "bound_revision": bound_revision, "bound_input_hash": bound_hash, "status": "open",
        })

    independent = [item for item in checks if item.kind != "run_succeeded"]
    if paused:
        acceptance = AcceptanceStatus.PENDING
        message = "尚无审查证据，验收保持 pending，并停止自动推进。"
    elif run is None or str(run.status) not in {"succeeded", "failed", "cancelled"}:
        acceptance = AcceptanceStatus.PENDING
        message = "运行尚未结束，不能提前通过验收。"
    elif str(run.status) != "succeeded":
        acceptance = AcceptanceStatus.FAILED
        message = "运行未成功，不能通过验收。"
    elif not independent:
        acceptance = AcceptanceStatus.NOT_CONFIGURED
        message = "未配置确定性检查，或只有 run_succeeded；运行成功不能当成验收通过。"
    elif findings:
        acceptance = AcceptanceStatus.FAILED
        message = "检查未通过；运行成功仍不是验收通过。"
    else:
        acceptance = AcceptanceStatus.PASSED
        message = "确定性检查通过。"

    return Evaluation(
        acceptance=acceptance,
        review_status=review_status,
        findings=findings,
        checks=checks,
        bound_revision=bound_revision,
        bound_input_hash=bound_hash,
        paused_for_review=paused,
        used_substitute=used_substitute,
        message=message,
    )


def _run_check(
    spec: dict[str, Any],
    task: Task,
    run: Run | None,
    workspace_root,
    revision: int,
    digest: str,
) -> CheckResult:
    kind = str(spec["kind"])
    if kind == "contains":
        needle = str(spec.get("text") or "")
        haystack = "" if run is None else run.summary
        passed = bool(needle) and needle in haystack
        return CheckResult(kind, passed, f"contains {needle!r}: {passed}", revision, digest)
    if kind == "file_exists":
        rel = str(spec.get("path") or "")
        path = resolve_within(workspace_root, rel)
        passed = path.is_file()
        return CheckResult(kind, passed, f"file_exists {rel}: {passed}", revision, digest)
    if kind == "file_contains":
        rel = str(spec.get("path") or "")
        path = resolve_within(workspace_root, rel)
        needle = str(spec.get("text") or "")
        passed = bool(needle) and path.is_file() and needle in path.read_text(encoding="utf-8")
        return CheckResult(kind, passed, f"file_contains {rel} {needle!r}: {passed}", revision, digest)
    if kind == "run_succeeded":
        passed = run is not None and str(run.status) == "succeeded"
        return CheckResult(
            kind,
            passed,
            "run_succeeded 只是输入，不能单独把验收标为通过",
            revision,
            digest,
        )
    raise AsterunError(INVALID_REQUEST, f"未知检查 kind：{kind}")
