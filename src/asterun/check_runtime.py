"""受控检查的持久化意图与后台执行；只有核心线程读写状态库。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from threading import Event
from datetime import datetime, timezone

from asterun.errors import AsterunError
from asterun.executor import Executor
from asterun.ids import RunId, TypedId, new_id
from asterun.quality import digest
from asterun import local_checks

MAX_JOBS = 32


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class CheckRuntime:
    def __init__(self, app):
        self.app = app
        self.executor = Executor() if app.executor is not None else None
        self.active = {}  # job id -> (run id, cancellation event)

    def jobs(self, run):
        return deepcopy(run.native.get("local_checks", {}))

    def save(self, run, jobs):
        self.app.store.save_run(replace(run, native={**run.native, "local_checks": jobs}))

    def restore(self):
        # 已提交意图可能已送达。重启只标未决，绝不自动重跑检查。
        for task_id in self.app.store.list_task_ids():
            for run in self.app.store.list_runs(task_id):
                jobs = self.jobs(run)
                changed = False
                for job in jobs.values():
                    if job["status"] in {"running", "cancelling"}:
                        job.update(status="unknown", error_code="REMOTE_STATE_UNKNOWN")
                        changed = True
                if changed:
                    self.save(run, jobs)

    def submit(self, task, run, payload, bound, spec):
        self.drain()
        asynchronous = payload.get("async", False)
        if asynchronous and (self.executor is None or not payload.get("idempotency_key")):
            raise AsterunError("INVALID_REQUEST", "异步检查需要常驻核心及 idempotency_key")
        request_hash = digest({"bound": bound, "spec": spec, "check_name": payload["check_name"]})
        key = digest(payload["idempotency_key"]) if payload.get("idempotency_key") else None
        run = self.app.store.get_run(run.id)
        jobs = self.jobs(run)
        for job in jobs.values():
            if key and job.get("key") == key:
                if job["request_hash"] != request_hash:
                    raise AsterunError("IDEMPOTENCY_CONFLICT", "检查幂等键已绑定其他请求")
                view = self.view(task, run, job)
                if not asynchronous and view["status"] in {"completed", "cancelled"}:
                    return {**view.get("result", {}), "job_id": view["job_id"], "status": view["status"]}
                return view
        if len(jobs) >= MAX_JOBS:
            raise AsterunError("RATE_LIMITED", "单次运行最多保存 32 个检查意图；请复用已有检查或推进任务")
        if len(self.active) >= self.app.config.scheduler.local_check_concurrency:
            raise AsterunError("RATE_LIMITED", "检查槽位均在执行，请在完成后重试")
        job_id = new_id("chk")
        job = {"job_id": job_id, "status": "running", "check_name": payload["check_name"],
               "key": key, "request_hash": request_hash,
               "stage": spec.get("stage", "coding"),
               "bound": {k: deepcopy(v) for k, v in payload.items() if k in {"task_id", "expected_run_id",
                   "expected_revision", "expected_input_hash", "expected_target_hash", "target_paths"}},
               "check_sha256": digest(spec), "created_at": utc_now()}
        candidates = [j for j in jobs.values() if j["status"] == "completed" and j["request_hash"] == request_hash]
        jobs[job_id] = job
        self.save(run, jobs)  # 必须在启动任何进程之前持久化意图。
        root = self.app.config.get_workspace(task.workspace).root
        state_dir = self.app.state_dir
        spec = deepcopy(spec)
        stop = Event()

        def execute(_publish=None, _stopping=None):
            try:
                if stop.is_set():
                    return {"status": "cancelled"}
                fingerprint = local_checks.runtime_fingerprint(spec)
                for candidate in reversed(candidates):
                    previous = candidate.get("result", {})
                    if (fingerprint and previous.get("runtime_fingerprint") == fingerprint
                            and not previous.get("timed_out") and not previous.get("cancelled")):
                        return {"status": "completed", "result": {**deepcopy(previous), "cache_hit": True,
                                "cache_source_job_id": candidate["job_id"]}}
                if stop.is_set():
                    return {"status": "cancelled"}
                result = local_checks.execute_check(root, bound, spec, payload["check_name"], state_dir,
                                                    stopping=stop)
                if result.get("cancelled") or stop.is_set():
                    return {"status": "cancelled"}
                after = local_checks.runtime_fingerprint(spec) if fingerprint else None
                result.update(cache_hit=False, runtime_fingerprint=fingerprint if fingerprint == after else None,
                              cache_available=bool(fingerprint and fingerprint == after))
                if fingerprint and fingerprint != after:
                    return {"status": "stale", "error_code": "REVISION_CONFLICT"}
                return {"status": "cancelled" if result.get("cancelled") else "completed", "result": result}
            except AsterunError as error:
                return {"status": "stale" if error.code == "REVISION_CONFLICT" else "failed", "error_code": error.code}
            except Exception:
                return {"status": "unknown", "error_code": "REMOTE_STATE_UNKNOWN"}

        self.active[job_id] = (run.id, stop)
        if asynchronous:
            try:
                self.executor.start(TypedId(job_id), execute)
            except Exception:
                self.record(TypedId(job_id), {"status": "unknown", "error_code": "REMOTE_STATE_UNKNOWN"})
                raise AsterunError("REMOTE_STATE_UNKNOWN", "检查意图已保存，未确认启动；不会自动重派")
            return self.view(task, run, job)
        self.record(TypedId(job_id), execute())
        job = self.jobs(self.app.store.get_run(run.id))[job_id]
        if job["status"] not in {"completed", "cancelled"}:
            raise AsterunError(job.get("error_code", "REMOTE_STATE_UNKNOWN"), "检查未生成有效报告")
        return {**job.get("result", {}), "job_id": job_id, "status": job["status"]}

    def record(self, job_id, update):
        active = self.active.get(job_id.value)
        if active is None:
            return
        run = self.app.store.get_run(active[0])
        jobs = self.jobs(run)
        job = jobs[job_id.value]
        status = update.get("status")
        if status not in {"completed", "cancelled", "stale", "failed", "unknown"}:
            update = {"status": "unknown", "error_code": "REMOTE_STATE_UNKNOWN"}
        if job["status"] == "cancelling" or active[1].is_set():
            update = {"status": "cancelled"}  # 不公布与取消竞争的成功报告。
        if update["status"] == "completed":
            task = self.app.store.get_task(run.task_id)
            try:
                self.app._bound_external_target(task, self.app.store.get_run(task.current_run_id), job["bound"], required=True)
                spec = self.app.config.workflow.local_checks.get(job["check_name"])
                if spec is None or digest(spec) != job["check_sha256"]:
                    raise AsterunError("REVISION_CONFLICT", "检查配置已改变")
            except AsterunError:
                update = {"status": "stale", "error_code": "REVISION_CONFLICT"}
        job.update(update, finished_at=utc_now())
        job["passed"] = update.get("result", {}).get("passed") if update["status"] == "completed" else None
        self.save(run, jobs)
        self.active.pop(job_id.value, None)

    def view(self, task, run, job):
        result = deepcopy(job)
        for name in ("bound", "key", "request_hash"):
            result.pop(name, None)
        if result["status"] == "completed":
            try:
                current = self.app.store.get_run(task.current_run_id) if task.current_run_id else None
                self.app._bound_external_target(task, current, job["bound"], required=True)
                spec = self.app.config.workflow.local_checks.get(job["check_name"])
                if spec is None or digest(spec) != job["check_sha256"]:
                    raise AsterunError("REVISION_CONFLICT", "检查配置已改变")
            except AsterunError:
                result.pop("result", None)
                result.update(status="stale", passed=None, error_code="REVISION_CONFLICT")
        result.update(acceptance_unchanged=True, independent_review=False)
        return result

    def get(self, task, payload, *, cancel=False):
        # 取消先落盘，再接收后台结果；与成功竞争时以取消意图为准。
        run = self.app.store.get_run(RunId(payload["expected_run_id"]))
        if run.task_id != task.id:
            raise AsterunError("INVALID_REQUEST", "检查运行不属于该任务")
        job = self.jobs(run).get(payload["job_id"])
        if job is None:
            raise AsterunError("NOT_FOUND", "找不到检查意图")
        if cancel and job["status"] in {"running", "cancelling"}:
            jobs = self.jobs(run)
            jobs[job["job_id"]]["status"] = "cancelling"
            self.save(run, jobs)
            active = self.active.get(job["job_id"])
            if active:
                active[1].set()
        self.drain()
        run = self.app.store.get_run(run.id)
        return self.view(task, run, self.jobs(run)[job["job_id"]])

    def drain(self):
        if self.executor:
            self.executor.drain(self.record)
            # 终态写入失败后 worker 已退出，不能让检查永远显示 running。
            for job_id, (run_id, _) in list(self.active.items()):
                if job_id not in self.executor.workers:
                    run = self.app.store.get_run(run_id)
                    jobs = self.jobs(run)
                    jobs[job_id].update(status="unknown", error_code="REMOTE_STATE_UNKNOWN")
                    self.save(run, jobs)
                    self.active.pop(job_id)

    def close(self):
        for _, stop in self.active.values():
            stop.set()
        if self.executor:
            self.executor.stopping.set()
            for worker in self.executor.workers.values():
                worker.join(timeout=5)
            self.drain()
        # 关闭未确认的完成或取消均保留 UNKNOWN，由下次人工决定。
        for job_id in list(self.active):
            run = self.app.store.get_run(self.active[job_id][0])
            jobs = self.jobs(run)
            jobs[job_id].update(status="unknown", error_code="REMOTE_STATE_UNKNOWN")
            self.save(run, jobs)
            self.active.pop(job_id)
