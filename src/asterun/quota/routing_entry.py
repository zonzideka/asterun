"""Quota-preference routing at task submit and task view."""

from __future__ import annotations

from asterun.errors import (
    BUDGET_EXHAUSTED,
    INVALID_CONFIG,
    INVALID_REQUEST,
    AsterunError,
)
from asterun.quota.decisions import last_chosen, latest_for_task, record_decision
from asterun.quota.service import _NAMESPACE, _member_windows, evaluate_group
from asterun.quota.settings import quota_preference_mode


def _namespace(app):
    admission = getattr(app, "admission", None)
    if admission is not None:
        return admission.namespace
    return _NAMESPACE


def _conn(app):
    return app.store._conn


def _get(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _routing_config(app):
    return _get(getattr(app, "config", None), "routing", {}) or {}


def _candidate_groups(app):
    return _get(_routing_config(app), "candidate_groups", {}) or {}


def _primary(group):
    if isinstance(group, dict):
        return group["primary"]
    return group.primary


def _allows_paid(app) -> bool:
    pref = _get(_routing_config(app), "quota_preference", {}) or {}
    return _get(pref, "allow_paid_on_explicit", False) is True


def resolve_backend(app, requested):
    """Resolve a backend name. Group refs (``@…``) also return a routing dict."""
    name = requested or _get(app.config, "default_backend")
    if not isinstance(name, str) or not name.startswith("@"):
        return name, None

    # "@implementer" 是候选组引用；配置键和 evaluate_group 用去掉 @ 的组名。
    group = name[1:]
    if _get(app.config, "schema_version", 1) != 2:
        raise AsterunError(INVALID_CONFIG, "schema v1 不支持候选组路由")

    groups = _candidate_groups(app)
    if not isinstance(groups, dict) or group not in groups:
        raise AsterunError(
            INVALID_REQUEST,
            f"未知候选组：{group}",
            details={"group": group},
        )

    primary = _primary(groups[group])
    mode = quota_preference_mode(app.config)
    if mode == "off":
        return primary, {
            "mode": "off",
            "group": group,
            "primary": primary,
            "chosen": primary,
            "reason_code": "preference_off",
            "would_choose": None,
        }

    try:
        res = evaluate_group(
            app,
            group,
            sticky=last_chosen(_conn(app), _namespace(app), group),
        )
    except Exception:
        return primary, {
            "mode": mode,
            "group": group,
            "primary": primary,
            "chosen": primary,
            "reason_code": "quota_state_invalid",
            "would_choose": None,
        }

    if mode == "dry_run":
        return primary, {
            "mode": "dry_run",
            "group": group,
            "primary": primary,
            "chosen": primary,
            "would_choose": res["chosen"],
            "reason_code": res["reason_code"],
            "_result": res,
        }

    return res["chosen"], {
        "mode": "on",
        "group": group,
        "primary": primary,
        "chosen": res["chosen"],
        "would_choose": None,
        "reason_code": res["reason_code"],
        "_result": res,
    }


def paid_guard(app, name):
    """Block an explicit backend when the window is exhausted and overage is paid."""
    if _allows_paid(app):
        return
    try:
        windows = _member_windows(app, name)
        exhausted = False
        paid = False
        for window in windows or []:
            if not isinstance(window, dict):
                continue
            obs = window.get("observation") or {}
            if not isinstance(obs, dict):
                continue
            freshness = window.get("freshness")
            used = obs.get("used_bp") or 0
            confidence_ok = obs.get("confidence") in {"high", "medium"}
            window_exhausted = freshness == "exhausted" or (
                freshness == "fresh" and used >= 10000
            )
            if window_exhausted and confidence_ok:
                exhausted = True
            if obs.get("paid_overage") is True:
                paid = True
    except Exception:
        return
    if exhausted and paid:
        raise AsterunError(
            BUDGET_EXHAUSTED,
            "订阅窗口已耗尽，继续会使用付费额度；默认阻断",
            next_action=(
                "确认后可在 routing.quota_preference 设置 allow_paid_on_explicit: true"
            ),
            details={"backend": name, "auto_retry": False},
        )


def record(app, task_id, run_id, role, routing):
    """Persist a group routing decision. Failures are swallowed."""
    try:
        result = routing.get("_result") or {
            "chosen": routing["chosen"],
            "primary": routing["primary"],
            "reason_code": routing["reason_code"],
            "candidates": [],
        }
        record_decision(
            _conn(app),
            namespace=_namespace(app),
            task_id=task_id,
            run_id=run_id,
            role=role,
            group=routing["group"],
            result=result,
            mode=routing["mode"],
        )
    except Exception:
        pass


def view(app, task_id):
    """Return the latest routing decision for a task, or None."""
    try:
        row = latest_for_task(_conn(app), _namespace(app), task_id)
        if not row:
            return None
        payload = row.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        return {
            "mode": row.get("mode"),
            "group": row.get("group_name"),
            "chosen": row.get("chosen"),
            "primary": row.get("primary_alias"),
            "reason_code": row.get("reason_code"),
            "would_choose": payload.get("would_choose"),
        }
    except Exception:
        return None
