"""核心拥有的可恢复质量流程；每个审查/修复意图和预算原子落盘。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

from asterun.contracts import (AcceptanceStatus, EventType, Operation, OperationStatus,
                              Run, RunStatus, SessionBinding, TERMINAL_RUN_STATUSES)
from asterun.errors import (AsterunError, BUDGET_EXHAUSTED, CAPABILITY_UNSUPPORTED,
                           IDEMPOTENCY_CONFLICT, INVALID_REQUEST, REMOTE_STATE_UNKNOWN)
from asterun.ids import BackendId, RunId, new_conversation_id, new_operation_id, new_run_id
from asterun.policy import enforce
from asterun.quality import digest, parse_review, review_prompt, snapshot
from asterun.workflows import evaluate_quality


class QualityRuntime:
    def __init__(self, app):
        self.app = app

    def _authorize(self, task):
        app = self.app
        app._require_grant(task.workspace)
        app._require_applied_config()
        app._assert_task_binding(task)
        enforce(app.policy.authorize(app.principal, "workflow.start", task.workspace))
        if task.quality:
            if task.quality["principal"] != app.principal.subject_id:
                raise AsterunError(CAPABILITY_UNSUPPORTED, "质量流程属于其他主体")
            if not app.config.entries.enabled(task.quality["entry"]):
                raise AsterunError(CAPABILITY_UNSUPPORTED, "启动质量流程的入口已关闭")

    def start(self, payload):
        app = self.app
        task = app._require_task(payload["task_id"])
        self._authorize(task)
        request_hash = digest(payload)
        previous_requests = task.quality.get("requests", {})
        if payload["idempotency_key"] in previous_requests:
            if previous_requests[payload["idempotency_key"]] != request_hash:
                raise AsterunError(IDEMPOTENCY_CONFLICT, "同一质量流程幂等键对应不同输入")
            return app.task_get(task.id.value)
        if task.paused or task.orchestration_owner != "core":
            raise AsterunError(CAPABILITY_UNSUPPORTED, "任务由人工持有，先恢复核心编排")
        if task.quality and task.quality["phase"] not in {"completed", "stale", "blocked"}:
            raise AsterunError(INVALID_REQUEST, "任务已有质量流程，不能再启动另一编排者")
        run = app.store.get_run(task.current_run_id) if task.current_run_id else None
        if task.quality and run and run.role == "review":
            if run.status not in TERMINAL_RUN_STATUSES:
                raise AsterunError(INVALID_REQUEST, "审查仍在运行或未决，不能开始另一流程")
            run = app.store.get_run(RunId(task.quality["implementation_run_id"]))
        if not run or run.status != RunStatus.SUCCEEDED or run.cancel_requested:
            raise AsterunError(INVALID_REQUEST, "质量流程需要已成功且未取消的实现运行")
        checks = payload["checks"]
        if not any(item["kind"] != "run_succeeded" for item in checks):
            raise AsterunError(INVALID_REQUEST, "质量流程至少需要一项独立确定性检查")
        for item in checks:
            if item["kind"] in {"file_exists", "file_contains"} and item.get("path") not in payload["target_paths"]:
                raise AsterunError(INVALID_REQUEST, "文件检查必须位于 target_paths 的验收范围内")
        target, _ = snapshot(app.config.get_workspace(task.workspace).root, payload["target_paths"],
                             state_dir=app.state_dir)
        previous = task.quality
        task = replace(task, acceptance=AcceptanceStatus.PENDING, evidence_stale=False, review_status="pending",
                       quality={"phase": "checking", "principal": app.principal.subject_id, "entry": app.entry,
                                "idempotency_key": payload["idempotency_key"], "request_hash": request_hash,
                                "requests": {**previous_requests, payload["idempotency_key"]: request_hash},
                                "checks": checks, "target_paths": payload["target_paths"], "target": target,
                                "implementation_run_id": run.id.value, "review_run_id": None,
                                "auto_repair": payload.get("auto_repair", True),
                                "reviews": previous.get("reviews", []), "closures": previous.get("closures", []),
                                "open_findings": previous.get("open_findings", task.findings)})
        app.store.save_task(task)
        self.advance()
        return app.task_get(task.id.value)

    def _capture(self, task):
        return snapshot(self.app.config.get_workspace(task.workspace).root, task.quality["target_paths"],
                        state_dir=self.app.state_dir)

    def _remember_findings(self, task):
        remembered = task.quality.setdefault("open_findings", [])
        known = {digest(item) for item in remembered}
        remembered.extend(item for item in task.findings if digest(item) not in known)

    def _checks(self, task):
        app = self.app
        implementation = app.store.get_run(RunId(task.quality["implementation_run_id"]))
        evaluation = evaluate_quality(task, implementation,
            config=replace(app.config.workflow, require_review=False),
            workspace_root=app.config.get_workspace(task.workspace).root,
            payload={"checks": task.quality["checks"]}, review_available=None)
        return implementation, evaluation

    def pause(self, task, code, message, *, phase=None):
        task = replace(task, paused=True, orchestration_owner="human", acceptance=AcceptanceStatus.PENDING,
                       quality=deepcopy(task.quality))
        task.quality["blocked_reason"] = {"code": code, "message": message}
        if phase:
            task.quality["phase"] = phase
        self.app.store.save_task(task)

    def invalidate(self, task):
        task.evidence_stale = True
        task.review_status = "stale"
        self.pause(task, INVALID_REQUEST, "目标内容或 Git HEAD 已改变，旧证据已作废；需要重新启动质量流程", phase="stale")

    def validate_evidence(self, task):
        if task.quality and task.quality["phase"] == "completed":
            try:
                self._authorize(task)
                current, _ = self._capture(task)
                _, checks = self._checks(task)
                if (current != task.quality["target"] or task.evidence_stale
                        or checks.acceptance != AcceptanceStatus.PASSED):
                    self.invalidate(task)
            except (AsterunError, OSError, UnicodeError):
                self.invalidate(task)
            return self.app.store.get_task(task.id)
        return task

    def advance(self):
        app = self.app
        # 每次 poll 对每个任务至多推进一个阶段；不会在请求里无限递归派发。
        for task_id in app.store.list_task_ids():
            task = app.store.get_task(task_id)
            if (not task.quality or task.paused or task.orchestration_owner != "core"
                    or task.quality["phase"] in {"completed", "stale", "blocked"}):
                continue
            try:
                self._authorize(task)
                self._advance_one(task)
            except (AsterunError, OSError, UnicodeError) as exc:
                # 重新读取：意图可能已原子提交，不能用旧 task 覆盖运行指针或预算。
                current = app.store.get_task(task_id)
                code = exc.code if isinstance(exc, AsterunError) else INVALID_REQUEST
                run = app.store.get_run(current.current_run_id)
                phase = "blocked" if code in {INVALID_REQUEST, BUDGET_EXHAUSTED} and run.status in TERMINAL_RUN_STATUSES else None
                self.pause(current, code, str(exc), phase=phase)

    def _advance_one(self, task):
        app, quality = self.app, task.quality
        run = app.store.get_run(task.current_run_id)
        if task.evidence_stale:
            self.invalidate(task)
            return
        if run.cancel_requested or run.status == RunStatus.CANCELLED:
            self.pause(task, INVALID_REQUEST, "运行已请求取消，质量流程停止", phase="blocked")
            return
        if run.status == RunStatus.PENDING_RECONCILE:
            self.pause(task, REMOTE_STATE_UNKNOWN, "运行结果未决；先对账再恢复，不自动重派")
            return
        if run.status not in TERMINAL_RUN_STATUSES:
            return
        if run.status != RunStatus.SUCCEEDED:
            self.pause(task, INVALID_REQUEST, "运行失败，保留证据供人工处理", phase="blocked")
            return
        target, files = self._capture(task)
        if quality["phase"] == "repairing":
            quality["implementation_run_id"] = run.id.value
            quality["target"] = target
            quality["phase"] = "checking"
            app.store.save_task(task)
            return
        if target != quality["target"]:
            self.invalidate(task)
            return
        implementation, evaluation = self._checks(task)
        # 检查前后的快照必须相同，避免把变化期间读到的结果绑到旧版本。
        if self._capture(task)[0] != target:
            self.invalidate(task)
            return
        if quality["phase"] == "checking":
            quality["check_results"] = evaluation.to_dict()["checks"]
            if evaluation.acceptance != AcceptanceStatus.PASSED:
                task.findings = [{**item, "id": digest([implementation.id.value, item]),
                                  "source_run_id": implementation.id.value, "target_hash": target["hash"]}
                                 for item in evaluation.findings]
                task.review_status = "pending"
                self._remember_findings(task)
                app.store.save_task(task)
                self._repair(task)
                return
            self._review(task, implementation, target, files)
            return
        if quality["phase"] == "reviewing":
            if run.role != "review" or run.id.value != quality["review_run_id"] or run.target_hash != target["hash"]:
                raise AsterunError(INVALID_REQUEST, "审查运行与目标引用不一致")
            findings = parse_review(run.summary, target)
            evidence = {"run_id": run.id.value, "implementation_run_id": implementation.id.value,
                        "backend": run.backend.value, "conversation_id": run.conversation_id.value,
                        "target_hash": target["hash"], "input_hash": task.input_hash,
                        "config_revision": task.config_revision, "findings": findings,
                        "simulated": app._backend(run.backend.value).config.kind == "fake",
                        "used_substitute": quality["used_substitute"], "checks": evaluation.to_dict()["checks"],
                        "scope": quality["target_paths"], "permission": "snapshot_only_no_tools"}
            evidence["independence"] = "separate_run_and_conversation"
            evidence["provider_identity_verified"] = False
            previous = list(quality["open_findings"])
            task.findings = [{**item, "id": digest([run.id.value, item]), "status": "open",
                              "source_run_id": run.id.value, "target_hash": target["hash"]} for item in findings]
            task.findings.extend(evaluation.findings)
            self._remember_findings(task)
            quality["reviews"].append(evidence)
            quality["phase"] = "reviewed"
            task.review_status = "findings" if task.findings else "passed"
            if not task.findings and evaluation.acceptance == AcceptanceStatus.PASSED:
                if implementation.role == "repair":
                    quality["closures"].extend({"finding": item, "repair_run_id": implementation.id.value,
                        "review_run_id": run.id.value, "target_hash": target["hash"]} for item in previous)
                quality["phase"] = "completed"
                quality["open_findings"] = []
                quality.pop("blocked_reason", None)
                task.acceptance = AcceptanceStatus.PASSED
                task.evidence_input_hash = task.input_hash
            app.store.save_task(task)
            app._event(run.id, EventType.ACCEPTANCE_EVALUATED, "core", {
                "acceptance": str(task.acceptance), "target_hash": target["hash"], "review_run_id": run.id.value})
            return
        if quality["phase"] == "reviewed":
            self._repair(task)

    def _review(self, task, implementation, target, files):
        from asterun.application import utc_now

        app = self.app
        chosen = None
        for name in (app.config.workflow.review_backend, app.config.workflow.approved_substitute):
            if name and name in app.config.backends:
                backend = app._backend(name)
                if (backend.can_dispatch() and callable(getattr(backend, "dispatch_review", None))
                        and not (backend.config.kind == "fake" and app._backend(task.backend.value).config.kind != "fake")):
                    chosen = backend
                    break
        if chosen is None:
            task.review_status = "unavailable"
            self.pause(task, CAPABILITY_UNSUPPORTED, "没有可执行快照审查的后端或已批准替代者")
            return
        if task.run_count >= app.config.scheduler.max_runs_per_task:
            raise AsterunError(BUDGET_EXHAUSTED, "审查也计入任务运行上限，预算已耗尽")
        now = utc_now()
        conversation_id = new_conversation_id()
        session = SessionBinding(conversation_id, BackendId(chosen.config.name), task.workspace,
            app.account_ref, app.runtime_ref, now, now, task_id=task.id, config_revision=task.config_revision)
        run = Run(new_run_id(), task.id, session.backend, RunStatus.QUEUED, now, role="review",
                  prompt=review_prompt(task, implementation, target, files, task.quality["check_results"]),
                  conversation_id=conversation_id, target_hash=target["hash"])
        operation = Operation(new_operation_id(), f"{task.quality['idempotency_key']}:{implementation.id.value}",
            app.principal.subject_id, "workflow.start", task.id.value, digest([target, run.prompt]),
            OperationStatus.INTENDED, now)
        decision = app.scheduler.admit(chosen.config.name, app._workspace_key(task))
        task = replace(task, current_run_id=run.id, run_count=task.run_count + 1, quality=deepcopy(task.quality))
        task.quality.update(phase="reviewing", review_run_id=run.id.value,
            used_substitute=chosen.config.name != app.config.workflow.review_backend)
        operation, task, run, reused = app.store.commit_intent(operation, task, run, session=session)
        if reused:
            return
        if decision == "queue":
            app.scheduler.enqueue(chosen.config.name, task.id, run.id, app._workspace_key(task))
        else:
            try:
                app._dispatch_committed(task, run, operation, session, chosen, chosen.config.name, now)
            except AsterunError:
                if operation.status == OperationStatus.INTENDED:
                    app._set_run(run, RunStatus.FAILED, terminated=True, summary="审查派发前校验失败，未调用后端")
                    app._finish_operation(run)
                raise

    def _repair(self, task):
        if not task.quality["auto_repair"]:
            self.pause(task, INVALID_REQUEST, "已记录 finding，自动修复未启用")
            return
        self.app.workflow_repair({"task_id": task.id.value,
            "idempotency_key": f"{task.quality['idempotency_key']}:repair:{task.current_run_id.value}"}, _quality=True)

    def before_dispatch(self, task, run):
        if not task.quality or not run.target_hash:
            return
        self._authorize(task)
        if task.paused or task.orchestration_owner != "core" or self._capture(task)[0]["hash"] != run.target_hash:
            raise AsterunError(INVALID_REQUEST, "派发前目标已变化或编排已暂停，未调用后端")
