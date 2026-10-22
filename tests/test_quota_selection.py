"""选路评分、退回矩阵，以及只读 routing.explain。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from asterun.application import Application
from asterun.plugin_store import PluginStore
from asterun.quota.selection import resolve_prefs, score_candidate, select
from asterun.quota.service import handle

NOW = datetime(2026, 10, 20, tzinfo=timezone.utc)
PREFS = resolve_prefs({"enabled": True})


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def window(*, kind="weekly", bucket="weekly", used=3000, confidence="high", freshness="fresh",
           paid=False, grants=None, hours_to_reset=24, duration_s=7 * 24 * 3600, hint=None,
           source="probe_test"):
    return {
        "observation": {
            "bucket_id": bucket,
            "window_kind": kind,
            "used_bp": used,
            "resets_at": _iso(NOW + timedelta(hours=hours_to_reset)),
            "window_duration_s": duration_s,
            "confidence": confidence,
            "paid_overage": paid,
            "source_kind": source,
            "exhausted_hint": hint,
            "expiring_grants": grants or [],
        },
        "freshness": freshness,
        "age_s": 30,
        "issues": [],
    }


def _score(windows, **kwargs):
    params = {"now": NOW, "prefs": PREFS, "inflight_runs": 0, "est_bp_per_run": 0, "weight": 1}
    params.update(kwargs)
    return score_candidate(windows, **params)


def test_weekly_waste_uses_elapsed_rate_when_enough_time_has_passed():
    scored = _score([window()])
    # used 30%，T=24h，D=168h，E=144h ≥ 0.1D。r=7000，rate·T=500，waste=6500。
    assert scored["S"] == 6500
    assert scored["S_A"] == 7000 // 24
    assert scored["U"] == 7000
    assert scored["tier"] == 0
    assert scored["excluded"] is False
    assert scored["windows_used"] == ["weekly/weekly"]


def test_full_speed_fallback_shrinks_waste_before_the_window_has_history():
    scored = _score([window(used=0, hours_to_reset=23, duration_s=24 * 3600)])
    # E=1h < max(1h, 0.1·24h)。满速 10000/D，waste = floor(10000/24) = 416。
    assert scored["S"] == 416
    assert scored["tier"] == 1


def test_five_hour_window_only_gates_unless_short_windows_are_enabled():
    weekly = window()
    short = window(kind="5h", bucket="five", used=0, hours_to_reset=1, duration_s=5 * 3600)
    gated = _score([short, weekly])
    assert gated["S"] == 6500
    assert gated["U"] == 7000
    assert "five/5h" in gated["windows_used"]

    only_short = _score([short])
    assert only_short["S"] == 0
    assert only_short["U"] == 10000
    assert only_short["tier"] == 1
    assert only_short["excluded"] is False

    counted = _score([short], prefs=resolve_prefs({"consider_short_windows": True}))
    assert counted["S"] == 10000
    assert counted["tier"] == 0


def test_exhausted_and_latched_candidates_are_excluded():
    exhausted = _score([window(used=10000, freshness="exhausted")])
    assert exhausted["excluded"] is True
    assert exhausted["tier"] == 3
    assert "exhausted" in exhausted["reason_codes"]

    hinted = _score([window(hint="exhausted")])
    assert hinted["excluded"] is True and "exhausted" in hinted["reason_codes"]

    latched = _score([window(kind="latch", bucket="latch", used=None, freshness="latched", source="latch")])
    assert latched["excluded"] is True
    assert latched["tier"] == 3
    assert "latched" in latched["reason_codes"]


def test_flipped_latch_window_does_not_replace_primary():
    expired = _score([window(
        kind="latch", bucket="latch", used=None, freshness="latched", source="latch", hours_to_reset=-1,
    )])
    assert expired["tier"] == 2
    assert expired["excluded"] is False
    assert "latched" not in expired["reason_codes"]

    flipped = window(
        kind="latch", bucket="latch", used=None, freshness="unknown", source="latch", hours_to_reset=-1,
    )
    flipped["issues"] = ["window_flipped"]
    primary = _score([flipped])
    assert primary["tier"] == 2
    assert primary["excluded"] is False
    assert primary["reason_codes"] == ["unknown"]

    other = _score([window()])
    assert other["tier"] == 0 and other["excluded"] is False
    decision = select(_group("primary", "other"), {"primary": primary, "other": other}, prefs=PREFS)
    assert decision["chosen"] == "primary"
    assert decision["mode"] == "fallback_primary"
    assert decision["reason_code"] == "primary_unknown"


def test_floor_demotes_to_tier_three_without_excluding():
    scored = _score([window(used=9600)], est_bp_per_run=0)
    assert scored["U"] == 400
    assert scored["tier"] == 3
    assert scored["excluded"] is False
    assert "below_floor" in scored["reason_codes"]

    with_estimate = _score([window(used=9000)], est_bp_per_run=100)
    assert with_estimate["U"] == 1000
    assert with_estimate["tier"] == 3


def test_low_confidence_cannot_reach_tier_zero():
    low = _score([window(confidence="low")])
    assert low["S"] == 6500
    assert low["tier"] == 1
    assert "confidence_below_required" in low["reason_codes"]
    assert _score([window(confidence="high")])["tier"] == 0
    assert _score([window(confidence="medium")])["tier"] == 0


def test_paid_overage_and_reset_grants_do_not_change_the_score():
    base = _score([window()])
    paid = _score([
        window(bucket="overage", used=9900, paid=True, freshness="fresh"),
        window(),
    ])
    assert paid["S"] == base["S"] == 6500
    assert paid["U"] == base["U"] == 7000
    assert paid["tier"] == 0 and paid["excluded"] is False
    assert "paid_overage_ignored" in paid["reason_codes"]

    grants = [{
        "count": 4,
        "expires_at": _iso(NOW + timedelta(hours=12)),
        "kind": "reset",
    }]
    assert _score([window(grants=grants)])["S"] == base["S"]
    forced = _score([window(grants=grants)], prefs=resolve_prefs({"count_reset_grants": True}))
    assert forced["S"] == base["S"]


def test_missing_or_stale_windows_are_unknown():
    assert _score([])["tier"] == 2
    assert "no_windows" in _score([])["reason_codes"]
    stale = _score([window(freshness="stale")])
    assert stale["tier"] == 2
    assert stale["U"] is None
    assert "unknown" in stale["reason_codes"]


def _candidate(tier, score, *, s_a=0, excluded=False, reasons=(), resets="2026-10-27T00:00:00Z", can_start=None):
    item = {
        "tier": tier, "S": score, "S_A": s_a, "excluded": excluded,
        "reason_codes": list(reasons), "resets_at": resets, "U": 0, "windows_used": [],
    }
    if can_start is not None:
        item["can_start"] = can_start
    return item


def _group(*members, primary=None):
    names = list(members)
    return {"role": "implementation", "members": names, "primary": primary or names[0]}


def test_select_falls_back_for_unknown_primary_and_all_latched():
    unknown = _candidate(2, 0, reasons=["unknown"])
    fresh = _candidate(0, 8000)
    group = _group("primary", "other")
    all_unknown = select(group, {"primary": unknown, "other": unknown}, prefs=PREFS)
    assert all_unknown["chosen"] == "primary"
    assert all_unknown["mode"] == "fallback_primary"
    assert all_unknown["reason_code"] == "all_unknown"

    primary_unknown = select(group, {"primary": unknown, "other": fresh}, prefs=PREFS)
    assert primary_unknown["chosen"] == "primary"
    assert primary_unknown["reason_code"] == "primary_unknown"

    latched = _candidate(3, 0, excluded=True, reasons=["latched"])
    excluded_primary = select(group, {"primary": latched, "other": fresh}, prefs=PREFS)
    assert excluded_primary["chosen"] == "other"
    assert excluded_primary["mode"] == "switch"
    assert excluded_primary["reason_code"] == "primary_excluded"

    all_latched = select(group, {"primary": latched, "other": latched}, prefs=PREFS)
    assert all_latched["chosen"] == "primary"
    assert all_latched["mode"] == "fallback_primary"
    assert all_latched["reason_code"] == "all_latched"


def test_margin_ties_and_sticky_keep_the_choice_conservative():
    group = _group("primary", "other")
    close = select(group, {
        "primary": _candidate(0, 1000),
        "other": _candidate(0, 1400),
    }, prefs=PREFS)
    assert close["chosen"] == "primary" and close["reason_code"] == "stay_primary"

    wide = select(group, {
        "primary": _candidate(0, 1000),
        "other": _candidate(0, 1600),
    }, prefs=PREFS)
    assert wide["chosen"] == "other" and wide["reason_code"] == "waste_margin"

    rounded = select(group, {
        "primary": _candidate(0, 1000),
        "other": _candidate(0, 1490),
    }, prefs=PREFS)
    assert rounded["chosen"] == "other"

    tied = _group("primary", "later", "earlier")
    removed = _candidate(3, 0, excluded=True, reasons=["exhausted"])
    choice = select(tied, {
        "primary": removed,
        "later": _candidate(0, 2000, resets="2026-10-25T00:00:00Z", s_a=10),
        "earlier": _candidate(0, 1600, resets="2026-10-21T00:00:00Z", s_a=10),
    }, prefs=PREFS)
    assert choice["chosen"] == "earlier"

    higher_rate = select(tied, {
        "primary": removed,
        "later": _candidate(0, 2000, resets="2026-10-21T00:00:00Z", s_a=10),
        "earlier": _candidate(0, 1600, resets="2026-10-21T00:00:00Z", s_a=80),
    }, prefs=PREFS)
    assert higher_rate["chosen"] == "earlier"

    declared = select(tied, {
        "primary": removed,
        "later": _candidate(0, 2000, resets="2026-10-21T00:00:00Z", s_a=10),
        "earlier": _candidate(0, 1700, resets="2026-10-21T00:00:00Z", s_a=10),
    }, prefs=PREFS)
    assert declared["chosen"] == "later"

    sticky = select(tied, {
        "primary": removed,
        "later": _candidate(0, 2000, resets="2026-10-25T00:00:00Z"),
        "earlier": _candidate(0, 2400, resets="2026-10-21T00:00:00Z"),
    }, prefs=PREFS, sticky="later")
    assert sticky["chosen"] == "later" and sticky["reason_code"] == "sticky"

    held_unknown = select(group, {"primary": _candidate(2, 0, reasons=["unknown"]), "other": _candidate(0, 9000)},
                          prefs=PREFS, sticky="other")
    assert held_unknown["chosen"] == "primary" and held_unknown["reason_code"] == "primary_unknown"


def test_single_member_group_stays_on_primary():
    group = _group("only")
    result = select(group, {"only": _candidate(3, 0, excluded=True, reasons=["latched"])}, prefs=PREFS)
    assert result["chosen"] == "only"
    assert result["primary"] == "only"
    assert result["mode"] == "primary"
    assert result["reason_code"] == "single_candidate"
    assert result["candidates"] == [{
        "alias": "only", "tier": 3, "S": 0, "S_A": 0, "excluded": True, "reason_codes": ["latched"],
    }]


def _account(name: str) -> dict:
    return {
        "provider": "xai", "credential_ref": "native-home:/tmp/" + name,
        "credential_revision": 1, "revoked": False,
    }


def _pool() -> dict:
    return {
        "billing_mode": "subscription", "paid_overage_allowed": False, "remaining": None,
        "overage_verified": False, "allow_unknown_subscription": False, "risk_acceptance": None,
    }


def _grok_config(workspace: Path) -> dict:
    def connection(account: str, home: str, model: str) -> dict:
        return {
            "plugin_id": "xai.grok", "enabled": True, "provider_account_ref": account,
            "runtime_ref": "runtime:configure", "auth_mode": "native_account",
            "billing_pool_refs": [account + "-pool"],
            "options": {"home": home, "model": model},
        }

    return {
        "schema_version": 2,
        "revision": 1,
        "workspaces": {"demo": {"root": str(workspace), "allow_non_git": True}},
        "default_backend": "grok-a",
        "plugins": {"xai.grok": {"enabled": True}},
        "provider_accounts": {"acct-a": _account("acct-a"), "acct-b": _account("acct-b")},
        "billing_pools": {"acct-a-pool": _pool(), "acct-b-pool": _pool()},
        "connections": {
            "conn-a": connection("acct-a", "/tmp/asterun-pr5-grok-a", "grok-code-fast-1"),
            "conn-b": connection("acct-b", "/tmp/asterun-pr5-grok-b", "grok-code-fast-1"),
        },
        "backends": {"grok-a": {"connection_ref": "conn-a"}, "grok-b": {"connection_ref": "conn-b"}},
        "routing": {
            "automatic_paid_fallback": False,
            "automatic_account_switch": False,
            "default_order": [],
            "rules": [],
            "candidate_groups": {
                "implementer": {
                    "role": "implementation", "members": ["grok-a", "grok-b"], "primary": "grok-a",
                },
            },
            "quota_preference": {"enabled": False},
        },
    }


def test_routing_explain_is_read_only_and_falls_back_when_primary_is_unknown(config_path: Path, workspace_root: Path):
    config_path.write_text(json.dumps(_grok_config(workspace_root)), encoding="utf-8")
    app = Application.from_paths(config_path, config_path.parent / "routing-explain")
    try:
        first = app.handle("routing.explain", {"group": "implementer"})
        assert first.ok, first.error
        assert first.data["dispatch_changed"] is False
        assert first.data["preference_enabled"] is False
        assert first.data["chosen"] == "grok-a"
        assert first.data["primary"] == "grok-a"
        assert first.data["mode"] == "fallback_primary"
        assert first.data["reason_code"] == "all_unknown"
        assert [item["alias"] for item in first.data["candidates"]] == ["grok-a", "grok-b"]

        saved = app.handle("quota.set", {
            "backend": "grok-b",
            "bucket_id": "weekly",
            "window_kind": "weekly",
            "used_percent": "30.00",
            "valid_until": _iso(datetime.now(timezone.utc) + timedelta(hours=2)),
            "resets_at": _iso(datetime.now(timezone.utc) + timedelta(hours=20)),
            "window_duration_s": 7 * 24 * 3600,
        })
        assert saved.ok, saved.error
        tasks_before = app.store.list_task_ids()
        conn = app.store._conn
        conn.execute("PRAGMA query_only=ON")
        try:
            explained = handle(app, "routing.explain", {"backend": "@implementer"})
        finally:
            conn.execute("PRAGMA query_only=OFF")
        assert explained["dispatch_changed"] is False
        assert explained["preference_enabled"] is False
        assert explained["chosen"] == "grok-a"
        assert explained["reason_code"] == "primary_unknown"
        assert explained["mode"] == "fallback_primary"
        assert {item["alias"] for item in explained["candidates"]} == {"grok-a", "grok-b"}
        assert app.store.list_task_ids() == tasks_before
    finally:
        app.close()


def test_routing_explain_uses_same_epoch_history_under_query_only(config_path: Path, workspace_root: Path):
    config_path.write_text(json.dumps(_grok_config(workspace_root)), encoding="utf-8")
    app = Application.from_paths(config_path, config_path.parent / "routing-explain-history")
    try:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        resets = _iso(now + timedelta(hours=24))
        common = {
            "scope_ref": "acct-b-pool",
            "bucket_id": "weekly",
            "window_kind": "weekly",
            "source_kind": "probe_codex",
            "confidence": "high",
            "paid_overage": False,
            "resets_at": resets,
            "window_duration_s": 7 * 24 * 3600,
            "valid_until": _iso(now + timedelta(hours=2)),
        }
        store = PluginStore(app.store._conn)
        store.append_quota_snapshot({
            **common, "observed_at": _iso(now - timedelta(hours=2, minutes=1)), "used_bp": 0,
        })
        store.append_quota_snapshot({
            **common, "observed_at": _iso(now - timedelta(minutes=1)), "used_bp": 3000,
        })
        # 没有历史时按已过时间折算，30% 用量、约 24 小时后重置的浪费量是数千。
        elapsed = _score([window(used=3000, confidence="high")])
        assert elapsed["S"] > 1000
        assert elapsed["tier"] == 0
        conn = app.store._conn
        conn.execute("PRAGMA query_only=ON")
        try:
            explained = handle(app, "routing.explain", {"group": "implementer"})
        finally:
            conn.execute("PRAGMA query_only=OFF")
        scored = next(item for item in explained["candidates"] if item["alias"] == "grok-b")
        assert explained["dispatch_changed"] is False
        assert scored["S"] == 0
        assert scored["S"] != elapsed["S"]
        assert scored["tier"] == 1
        assert scored["S_A"] > 0
        assert scored["excluded"] is False
        assert "unknown" not in scored["reason_codes"]
    finally:
        app.close()


def test_routing_explain_rejects_v1_configuration(config_path: Path):
    app = Application.from_paths(config_path, config_path.parent / "routing-v1")
    try:
        rejected = app.handle("routing.explain", {"group": "implementer"})
    finally:
        app.close()
    assert rejected.ok is False
    assert rejected.error["code"] == "INVALID_CONFIG"
    assert "v2" in rejected.error["message"]
