"""Task-scoped context token budget ledger (admission_only)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from asterun import control_protocol as protocol
from asterun.config_v2 import context_budget_settings
from asterun.context_budget.observe import _matched_profile
from asterun.contracts import TERMINAL_RUN_STATUSES
from asterun.control_store import ControlStore
from asterun.errors import AsterunError
from asterun.usage.breakdown import normalize_breakdown

NAMESPACE = "asterun.context_budget.v1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def budget_id(task_id: str) -> str:
    digest = hashlib.sha256(task_id.encode()).hexdigest()[:24]
    return "bud_cb_" + digest


def reservation_id(run_id: str) -> str:
    return "res_cb_" + run_id


def _entity(kind: str, id: str, **fields) -> dict:
    now = _now()
    body = {
        "schema_version": protocol.SCHEMA_VERSION,
        "kind": kind,
        "id": id,
        "namespace_id": NAMESPACE,
        "revision": 1,
        "created_at": now,
        "updated_at": now,
        "extensions": {},
    }
    body.update(fields)
    return body


def _policy_sha(settings: dict) -> str:
    payload = json.dumps(settings, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _positive_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def ensure_budget(store, task_id, profile_key, task_hard, policy_sha) -> dict:
    """Return the task budget. An existing row keeps its limit snapshot."""
    bid = budget_id(task_id)
    try:
        return store.get("Budget", bid, NAMESPACE)
    except AsterunError as exc:
        if exc.code != "NOT_FOUND":
            raise
    budget = _entity(
        "Budget",
        bid,
        task_id=task_id,
        limits=[
            {
                "meter": "tokens",
                "limit": int(task_hard),
                "enforcement": "admission_only",
                "billing_pool_ref": None,
            }
        ],
    )
    budget["extensions"] = {
        "asterun.legacy_task_id": task_id,
        "asterun.profile_key": profile_key,
        "asterun.budget_policy_sha256": policy_sha,
        "asterun.token_basis": "processed",
    }
    return store.put(budget)


def _profile(app, run):
    settings = context_budget_settings(app.config)
    if not isinstance(settings, dict) or settings.get("mode") != "enforce":
        return None
    profiles = settings.get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}
    role = run.role or "implementation"
    key, _matched_role = _matched_profile(app, run.backend.value, role, profiles)
    profile = profiles.get(key)
    if not isinstance(profile, dict):
        profile = {}
    return settings, key, profile


def _totals(store, bid) -> dict:
    for row in store.budget_totals(NAMESPACE, bid):
        if row.get("meter") == "tokens":
            return {"settled": row.get("settled", 0), "held": row.get("held", 0)}
    return {"settled": 0, "held": 0}


def guard_dispatch(app, run) -> dict | None:
    selected = _profile(app, run)
    if selected is None:
        return None
    settings, profile_key, profile = selected
    store = ControlStore(app.store._conn)
    run_id = run.id.value
    task_id = run.task_id.value
    with store.transaction():
        try:
            bud = store.get("Budget", budget_id(task_id), NAMESPACE)
        except AsterunError as exc:
            if exc.code != "NOT_FOUND":
                raise
            task_hard = profile.get("task_hard")
            if not _positive_int(task_hard):
                return None
            bud = ensure_budget(
                store,
                task_id,
                profile_key,
                task_hard,
                _policy_sha(settings),
            )
        limit = bud["limits"][0]["limit"]
        soft = profile.get("segment_soft")
        if _positive_int(soft):
            amount = min(soft, limit)
        else:
            totals = _totals(store, bud["id"])
            remaining = limit - totals["settled"] - totals["held"]
            amount = max(1, min(limit, remaining))
        reservation = _entity(
            "Reservation",
            reservation_id(run_id),
            task_id=task_id,
            budget_id=bud["id"],
            action_id=run_id,
            reserved=[
                {"meter": "tokens", "amount": amount, "billing_pool_ref": None}
            ],
            actual=None,
            status="HELD",
            receipt_evidence_ids=[],
        )
        try:
            return store.reserve(reservation)
        except AsterunError as exc:
            if exc.code != "BUDGET_EXHAUSTED":
                raise
            totals = _totals(store, bud["id"])
            raise AsterunError(
                "BUDGET_EXHAUSTED",
                "任务 Token 预算已用尽（admission_only）；已附胶囊，需显式加额后再继续",
                details={
                    "budget_id": bud["id"],
                    "task_hard": bud["limits"][0]["limit"],
                    "settled": totals["settled"],
                    "held": totals["held"],
                    "requested": amount,
                },
            ) from exc


def settle_run(app, run) -> None:
    try:
        settings = context_budget_settings(app.config)
        if not isinstance(settings, dict) or settings.get("mode") != "enforce":
            return
        if run.status not in TERMINAL_RUN_STATUSES:
            return
        store = ControlStore(app.store._conn, initialize=False)
        rid = reservation_id(run.id.value)
        try:
            current = store.get("Reservation", rid, NAMESPACE)
        except AsterunError as exc:
            if exc.code == "NOT_FOUND":
                return
            raise
        if current.get("status") != "HELD":
            return
        native = run.native if isinstance(run.native, dict) else {}
        breakdown = normalize_breakdown(native)
        processed = breakdown.get("processed")
        if (
            breakdown.get("attributable")
            and isinstance(processed, int)
            and processed >= 0
        ):
            store.settle(
                NAMESPACE,
                rid,
                [
                    {
                        "meter": "tokens",
                        "amount": processed,
                        "billing_pool_ref": None,
                    }
                ],
            )
            return
        store.mark_uncertain(NAMESPACE, rid)
    except Exception:
        return


def _token_amount(items) -> int:
    total = 0
    if not isinstance(items, list):
        return 0
    for item in items:
        if isinstance(item, dict) and item.get("meter") == "tokens":
            amount = item.get("amount")
            if type(amount) is int:
                total += amount
    return total


def budget_view(conn, task_id) -> dict | None:
    try:
        bid = budget_id(task_id)
        row = conn.execute(
            "SELECT payload FROM control_entities WHERE namespace_id=? AND kind='Budget' AND id=?",
            (NAMESPACE, bid),
        ).fetchone()
        if row is None:
            return None
        bud = json.loads(row[0])
        settled = 0
        held = 0
        uncertain = 0
        rows = conn.execute(
            "SELECT e.payload FROM control_reservations r "
            "JOIN control_entities e ON e.namespace_id=r.namespace_id "
            "AND e.kind='Reservation' AND e.id=r.id "
            "WHERE r.namespace_id=? AND r.budget_id=?",
            (NAMESPACE, bid),
        ).fetchall()
        for item in rows:
            payload = json.loads(item[0])
            status = payload.get("status")
            if status == "SETTLED":
                settled += _token_amount(payload.get("actual"))
            elif status in ("HELD", "UNCERTAIN"):
                held += _token_amount(payload.get("reserved"))
                if status == "UNCERTAIN":
                    uncertain += 1
        limits = bud.get("limits") or []
        task_hard = limits[0]["limit"] if limits else None
        extensions = bud.get("extensions") or {}
        return {
            "budget_id": bud["id"],
            "task_hard": task_hard,
            "settled": settled,
            "held": held,
            "uncertain_reservations": uncertain,
            "profile_key": extensions.get("asterun.profile_key"),
            "enforcement": "admission_only",
        }
    except Exception:
        return None
