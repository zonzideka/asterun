"""可选宿主的单次收件箱交付，不提供定时器或聊天发送实现。

宿主必须把 consumer_id 固定绑定到一个调用主体与交付目的地，并持久化去重。
deliver 对同一 delivery_key 必须幂等，包括已经交付但回执丢失的重试；本模块
不能承诺 exactly-once。回调仅在宿主已接受交付后返回明确回执，不能用排队意图
冒充用户已收到消息。核心确认只记录宿主的声明，聊天显示仍需独立验证。

report 中的 finding 和其他文本均为不可信材料，供展示，不是可执行指令。
本模块不运行 shell，不发布、修复、提交任务或放行审批，也不读取本机状态文件。
"""
from __future__ import annotations

from copy import deepcopy
import re

from asterun.review_follow import notification
from asterun.review_recipe import _view, digest, require


_REVIEW_ID = re.compile(r"rev_[0-9a-f]{32}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTITY_KEYS = {"repo", "pr", "head", "base", "workspace", "backend", "attempt"}
_NOTICE_KEYS = {"review_id", "identity", "task_id", "run_id", "stage", "gate_verdict",
                "counts", "output_sha256", "approval_id", "approval_target_hash",
                "publication_status", "next_action", "notification_sha256"}


def _selected_strings(value, keys):
    require(isinstance(value, dict), "交付材料结构无效")
    result = {}
    for key in keys:
        if key in value:
            require(value[key] is None or isinstance(value[key], str), "交付材料字段类型无效")
            result[key] = value[key]
    return result


def _counts(value):
    require(isinstance(value, dict) and set(value) <= {"P0", "P1", "P2"}
            and all(type(n) is int and n >= 0 for n in value.values()), "finding 计数无效")
    return dict(value)


def _identity(value):
    require(isinstance(value, dict) and set(value) == _IDENTITY_KEYS
            and type(value["pr"]) is int and value["pr"] > 0
            and all(isinstance(value[k], str) and value[k] for k in _IDENTITY_KEYS - {"pr"}),
            "审查目标身份结构无效")
    return dict(value)


def _valid_notice(value):
    require(isinstance(value, dict) and set(value) == _NOTICE_KEYS, "通知结构无效")
    require(isinstance(value["review_id"], str) and _REVIEW_ID.fullmatch(value["review_id"])
            and isinstance(value["notification_sha256"], str)
            and _SHA256.fullmatch(value["notification_sha256"]), "通知引用或摘要无效")
    _identity(value["identity"])
    _counts(value["counts"])
    _selected_strings(value, _NOTICE_KEYS - {"identity", "counts"})
    require(digest({k: v for k, v in value.items() if k != "notification_sha256"})
            == value["notification_sha256"], "通知摘要不匹配")


def build_report(status):
    """只投影已列明的展示字段；不转发 execution、task.text、原始输出或审批命令。"""
    review, result = status["review"], status["result"]
    report = {"review_id": review["id"], "identity": _identity(review["identity"])}
    public_result = _selected_strings(result, {"stage", "gate_verdict", "output_sha256", "next_action"})
    public_result["counts"] = _counts(result.get("counts", {}))
    findings = result.get("findings", [])
    require(isinstance(findings, list), "finding 列表无效")
    public_result["findings"] = []
    for finding in findings:
        require(isinstance(finding, dict) and isinstance(finding.get("summary"), str)
                and isinstance(finding.get("path"), str) and type(finding.get("line")) is int,
                "finding 结构无效")
        public_result["findings"].append({k: finding[k] for k in ("summary", "path", "line")})
    if "publication_ready" in result:
        require(type(result["publication_ready"]) is bool, "发布准备状态无效")
        public_result["publication_ready"] = result["publication_ready"]
    report["result"] = public_result
    report["native_reference"] = _selected_strings(status.get("native_reference") or {},
        {"backend_session_id", "session_id", "thread_id", "turn_id", "current_turn_id", "cwd", "status"})
    context = review.get("workspace_context") or {}
    report["workspace_context"] = _selected_strings(context,
        {"mode", "native_cwd", "native_project_visibility", "note"})
    binding = context.get("project_binding")
    report["workspace_context"]["project_binding"] = None if binding is None else _selected_strings(binding,
        {"project_workspace", "project_root", "git_common_dir", "git_dir", "review_head"})
    approval = status["execution"].get("approval") or {}
    report["pending_approval"] = (_selected_strings(approval,
        {"id", "state", "target_hash", "task_id", "run_id", "method"})
        if approval.get("state") == "pending" else None)
    return report


def _consume(client, consumer_id, notice, deliver):
    identifier = notice["review_id"]
    outcome = {"review_id": identifier, "state": "invalid_notification", "retryable": True,
               "host_declared_delivered": False, "core_acknowledged": False,
               "chat_visibility": "not_verified"}
    try:
        _valid_notice(notice)
    except Exception:
        return outcome
    sha = notice["notification_sha256"]
    key = digest({"consumer_id": consumer_id, "notification_sha256": sha})
    outcome.update(delivery_key=key, notification_sha256=sha)
    try:
        status = _view(client, "review.status", {"review_id": identifier})
    except Exception:
        return {**outcome, "state": "status_unavailable"}
    try:
        current = notification(status)
        if current != notice:
            return {**outcome, "state": "notice_changed"}
        report = build_report(status)
    except Exception:
        return {**outcome, "state": "invalid_status"}
    material = {"notification": deepcopy(notice), "report": report, "delivery_key": key}
    try:
        receipt = deliver(material)
    except Exception:
        # 回调异常原文可能含消息内容或凭据，不能回显到诊断结果。
        return {**outcome, "state": "delivery_failed"}
    if (not isinstance(receipt, dict) or set(receipt) != {"delivery_key", "notification_sha256", "delivered"}
            or receipt.get("delivered") is not True or receipt.get("delivery_key") != key
            or receipt.get("notification_sha256") != sha):
        return {**outcome, "state": "receipt_mismatch"}
    outcome["host_declared_delivered"] = True
    try:
        ack = _view(client, "review.ack", {"review_id": identifier, "consumer_id": consumer_id,
                                         "notification_sha256": sha})
        if ack.get("acknowledged") is True and ack.get("notification_sha256") == sha:
            return {**outcome, "state": "acknowledged", "core_acknowledged": True, "retryable": False}
    except Exception:
        pass
    # 不在不确定窗口重发确认；下次由原通知与宿主持久去重共同对账。
    return {**outcome, "state": "ack_pending"}


def run_once(client, *, consumer_id, review_ids, deliver, max_pages=10, after=None):
    """扫描至多十页，只交付显式审查范围；后续轮询必须由宿主安排。

    deliver(material) 成功时须返回恰好三个字段：delivery_key、
    notification_sha256、delivered=True。回执丢失时可能以同一 key 再调用，
    宿主必须持久去重；consumer_id 不能跨调用主体、收件人或聊天目的地复用。
    max_pages 触顶或读页失败时 incomplete=True，不能据此声称已扫描所有通知。
    宿主将 next_cursor 持久保存，传给下轮 after；扫描到页末后它重置为 None，
    再从首屏检查更早审查的新状态。分页进度不等于消息交付回执。
    """
    require(isinstance(consumer_id, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", consumer_id),
            "consumer_id 无效")
    require(isinstance(review_ids, (list, tuple, set, frozenset)) and 1 <= len(review_ids) <= 100
            and all(isinstance(i, str) and _REVIEW_ID.fullmatch(i) for i in review_ids)
            and len(set(review_ids)) == len(review_ids), "需要 1–100 个不同的显式审查引用")
    require(callable(deliver), "deliver 必须是宿主回调")
    require(type(max_pages) is int and 1 <= max_pages <= 10, "max_pages 必须为 1–10")
    require(after is None or isinstance(after, str) and _REVIEW_ID.fullmatch(after), "after 无效")
    scope, seen, outcomes = set(review_ids), set(), []
    cursor = after
    response = {"consumer_id": consumer_id, "outcomes": outcomes, "pages_read": 0,
                "incomplete": False, "next_cursor": None, "requires_host_polling": True,
                "delivery_semantics": "host_durable_idempotency_required",
                "chat_visibility": "not_verified"}
    for _ in range(max_pages):
        try:
            request = {"consumer_id": consumer_id, "limit": 100}
            if cursor is not None:
                request["after"] = cursor
            page = _view(client, "review.inbox", request)
            notices, next_cursor = page["notifications"], page.get("next_cursor")
            require(isinstance(notices, list) and len(notices) <= 100, "收件箱页结构无效")
            require(next_cursor is None or isinstance(next_cursor, str) and _REVIEW_ID.fullmatch(next_cursor)
                    and (cursor is None or next_cursor > cursor), "收件箱游标无效")
        except Exception:
            response.update(incomplete=True, next_cursor=cursor, page_state="unavailable")
            break
        response["pages_read"] += 1
        for notice in notices:
            identifier = notice.get("review_id") if isinstance(notice, dict) else None
            if not isinstance(identifier, str) or identifier not in scope or identifier in seen:
                continue
            seen.add(identifier)
            outcomes.append(_consume(client, consumer_id, notice, deliver))
        response["next_cursor"] = next_cursor
        if next_cursor is None:
            break
        cursor = next_cursor
    else:
        response.update(incomplete=True, page_state="page_limit")
    response["retry_required"] = response["incomplete"] or any(row["retryable"] for row in outcomes)
    return response
