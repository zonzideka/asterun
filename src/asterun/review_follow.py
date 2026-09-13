"""可恢复的审查收件箱和有界跟进；不发布、不修复、不重新提交。"""
from __future__ import annotations

import math
from pathlib import Path
import re
import time

from asterun.errors import AsterunError, INVALID_REQUEST, REMOTE_STATE_UNKNOWN
from asterun.review_recipe import (_lock, _manifest_path, _private_directory, _read, _read_manifest,
                                   _view, _write, digest, read_review, require)


def notification(status):
    result = status.get("result", {})
    if result.get("stage") in {None, "running"}:
        return None
    review, execution = status["review"], status["execution"]
    approval = execution.get("approval") or {}
    value = {"review_id": review["id"], "identity": review["identity"],
             "task_id": review["task_id"], "run_id": execution["run"]["id"],
             "stage": result["stage"], "gate_verdict": result["gate_verdict"],
             "counts": result.get("counts", {}), "output_sha256": result["output_sha256"],
             "approval_id": approval.get("id"), "approval_target_hash": approval.get("target_hash"),
             "publication_status": status.get("publication", {}).get("status", "not_published"),
             "next_action": result["next_action"]}
    value["notification_sha256"] = digest(value)
    return value


def _receipt(state_dir, identifier, consumer_id, principal):
    require(isinstance(consumer_id, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", consumer_id), "consumer_id 无效")
    key = digest({"consumer": consumer_id, "principal": principal})
    return _manifest_path(state_dir, identifier).parent / "notifications" / (key + ".json")


def inbox(client, state_dir, *, consumer_id, principal="local", after=None, limit=20):
    require(type(limit) is int and 1 <= limit <= 100, "limit 必须为 1–100")
    require(after is None or isinstance(after, str) and re.fullmatch(r"rev_[0-9a-f]{32}", after), "after 无效")
    require(isinstance(consumer_id, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", consumer_id), "consumer_id 无效")
    base = Path(state_dir) / "reviews"
    if not base.exists():
        return {"notifications": [], "next_cursor": None, "requires_host_polling": True}
    _private_directory(base)
    identifiers = sorted(p.name for p in base.iterdir() if re.fullmatch(r"rev_[0-9a-f]{32}", p.name)
                         and (after is None or p.name > after))
    selected = identifiers[:limit]
    notices = []
    for identifier in selected:
        try:
            status = read_review(client, state_dir, identifier)
            if not status.get("execution"):
                continue
            value = notification(status)
            if value is None:
                continue
            receipt = _receipt(state_dir, identifier, consumer_id, principal)
            if receipt.exists():
                try:
                    if _read(receipt).get("notification_sha256") == value["notification_sha256"]:
                        continue
                except (AsterunError, OSError):
                    pass  # 确认记录损坏不能吞掉待交付通知。
            notices.append(value)
        except (AsterunError, OSError):
            # 不把另一个主体无权读取的记录、损坏目录或错误原文暴露在列表中。
            continue
    return {"notifications": notices, "next_cursor": selected[-1] if len(identifiers) > limit else None,
            "requires_host_polling": True, "delivery_semantics": "read_does_not_acknowledge",
            "next_action": "向用户交付结论或阻塞说明后，按该通知摘要确认；到页末后下次从第一页检查"}


def acknowledge(client, state_dir, identifier, *, consumer_id, notification_sha256, principal="local"):
    path = _manifest_path(state_dir, identifier)
    with _lock(path.parent):
        value = notification(read_review(client, state_dir, identifier))
        require(value is not None and value["notification_sha256"] == notification_sha256,
                "通知已变化或尚无通知；重新读取并报告当前状态后再确认")
        receipt = _receipt(state_dir, identifier, consumer_id, principal)
        _private_directory(receipt.parent)
        _write(receipt, {"notification_sha256": notification_sha256})
        return {"acknowledged": True, "notification_sha256": notification_sha256,
                "meaning": "发起端声明已交付；核心没有独立验证聊天显示"}


def handle(app, method, payload):
    require(app.state_dir is not None, "审查入口需要持久状态目录")
    from asterun.policy import enforce

    class AuthorizedClient:
        def handle(self, name, request):
            return app.handle(name, request)

    client = AuthorizedClient()
    if method == "review.inbox":
        return inbox(client, app.state_dir, principal=app.principal.subject_id, **payload)
    identifier = payload["review_id"]
    manifest = _read_manifest(_manifest_path(app.state_dir, identifier))
    enforce(app.policy.authorize(app.principal, "task.get", manifest["identity"]["workspace"]))
    if method == "review.status":
        return read_review(client, app.state_dir, identifier)
    if method == "review.ack":
        return acknowledge(client, app.state_dir, identifier, consumer_id=payload["consumer_id"],
                           notification_sha256=payload["notification_sha256"], principal=app.principal.subject_id)
    raise AsterunError(INVALID_REQUEST, "未知审查跟进入口")


def watch_review(client, state_dir, identifier, *, timeout=60, interval=1, apply_policy=False,
                 on_change=None, clock=time.monotonic, sleep=time.sleep):
    require(all(isinstance(n, (float, int)) and not isinstance(n, bool) and math.isfinite(n) and n > 0
                for n in (timeout, interval)) and timeout <= 60 and interval <= 5, "观察时限最多 60 秒，间隔最多 5 秒")
    deadline = clock() + timeout
    last = None
    attempts = set()
    while True:
        if clock() >= deadline:
            return {"reason": "deadline", "last_snapshot": last, "resume_review_id": identifier,
                    "next_action": "继续观察同一 review_id；宿主必须安排下次观察，读取不等于已通知"}
        # 本机传输自身有时限，并进一步约束到剩余总时间。
        if hasattr(client, "timeout"):
            client.timeout = min(5, max(.001, deadline - clock()))
        try:
            current = read_review(client, state_dir, identifier)
        except AsterunError as error:
            # _view 将只读传输失败归为 REMOTE_STATE_UNKNOWN。此处没有发出
            # 审批或提交；观察预算耗尽可以继续观察同一引用，不是执行失败。
            if error.code == REMOTE_STATE_UNKNOWN and clock() >= deadline:
                return {"reason": "deadline", "last_snapshot": last, "resume_review_id": identifier,
                        "error": error.to_dict(), "next_action": "读取耗尽本次观察时限；继续观察同一 review_id"}
            return {"reason": "unknown", "last_snapshot": last, "resume_review_id": identifier,
                    "error": error.to_dict(), "next_action": "回读同一审查，勿重发提交或审批"}
        if current != last and on_change:
            on_change(current)
        last = current
        result = current.get("result", {})
        policy_result = None
        if result.get("stage") == "waiting_input" and apply_policy:
            approval = current.get("execution", {}).get("approval") or {}
            if approval.get("state") == "pending" and approval.get("id") not in attempts:
                attempts.add(approval["id"])
                if clock() >= deadline:
                    continue
                if hasattr(client, "timeout"):
                    client.timeout = min(5, max(.001, deadline - clock()))
                try:
                    answer = _view(client, "approval.apply-policy", {"approval_id": approval["id"],
                        "expected_task_id": current["review"]["task_id"],
                        "expected_run_id": current["execution"]["run"]["id"],
                        "expected_target_hash": approval["target_hash"]})
                except AsterunError as error:
                    return {"reason": "unknown", "last_snapshot": last, "resume_review_id": identifier,
                            "error": error.to_dict(), "next_action": "审批结果未确认；核对原审批，不重发"}
                policy_result = {"approval_id": approval["id"],
                                 "forwarded": answer.get("forwarded", False),
                                 "manual_required": answer.get("manual_required", False),
                                 "assessment": answer.get("assessment") or
                                     (answer.get("approval") or {}).get("assessment")}
                if answer.get("forwarded") and not answer.get("manual_required"):
                    continue
        if result.get("stage") not in {None, "running"} or not current.get("execution"):
            return {"reason": result.get("stage", current.get("submission_status", "not_submitted")),
                    "last_snapshot": current, "notification": notification(current),
                    "resume_review_id": identifier,
                    **({"policy_result": policy_result} if policy_result is not None else {})}
        sleep(min(interval, max(0, deadline - clock())))
