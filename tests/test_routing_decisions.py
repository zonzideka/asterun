"""路由决策记录、裁剪，以及 evaluate_group 与 explain_routing 一致。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from asterun.application import Application
from asterun.backup import DB_NAME, _validate_snapshot
from asterun.errors import AsterunError
from asterun.plugin_store import ROUTING_TABLE_COLUMNS, SCHEMA
from asterun.quota.decisions import last_chosen, latest_for_task, record_decision
from asterun.quota.service import evaluate_group, explain_routing
from asterun.sqlite_store import SqliteStore

_CANDIDATE_KEYS = {"alias", "tier", "S", "S_A", "excluded", "reason_codes"}


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    return conn


def _result(chosen: str = "grok-a", **extra) -> dict:
    body = {
        "chosen": chosen,
        "primary": "grok-a",
        "mode": "primary",
        "reason_code": "stay_primary",
        "candidates": [{
            "alias": chosen,
            "tier": 1,
            "S": 5,
            "S_A": 1,
            "excluded": False,
            "reason_codes": ["stay_primary"],
        }],
    }
    body.update(extra)
    return body


def _record(conn, **overrides):
    params = {
        "namespace": "ns_local",
        "task_id": "task-1",
        "run_id": "run-1",
        "role": "implementation",
        "group": "implementer",
        "result": _result(),
        "mode": "primary",
    }
    params.update(overrides)
    return record_decision(conn, **params)


def test_record_last_chosen_and_latest_for_task():
    conn = _connection()
    try:
        assert last_chosen(conn, "ns_local", "implementer") is None
        assert latest_for_task(conn, "ns_local", "task-1") is None
        _record(conn, result=_result("grok-a"), mode="primary")
        _record(
            conn,
            task_id="task-1",
            run_id="run-2",
            result=_result("grok-b", reason_code="waste_margin"),
            mode="switch",
        )
        _record(
            conn,
            task_id="task-review",
            run_id="run-3",
            role="review",
            group="reviewer",
            result=_result("grok-c"),
            mode="primary",
        )
        assert last_chosen(conn, "ns_local", "implementer") == "grok-b"
        assert last_chosen(conn, "ns_local", "reviewer") == "grok-c"
        assert last_chosen(conn, "ns_other", "implementer") is None
        latest = latest_for_task(conn, "ns_local", "task-1")
        assert latest is not None
        assert latest["run_id"] == "run-2"
        assert latest["role"] == "implementation"
        assert latest["group_name"] == "implementer"
        assert latest["chosen"] == "grok-b"
        assert latest["primary_alias"] == "grok-a"
        assert latest["mode"] == "switch"
        assert latest["reason_code"] == "waste_margin"
        assert latest["payload"]["would_choose"] == "grok-b"
        assert latest_for_task(conn, "ns_local", "task-review")["chosen"] == "grok-c"
        assert latest_for_task(conn, "ns_local", "missing") is None
    finally:
        conn.close()


def test_prune_caps_at_1000_and_drops_rows_older_than_30_days():
    conn = _connection()
    try:
        _record(conn, task_id="old-task", result=_result("old"))
        _record(conn, task_id="kept-task", result=_result("kept"))
        old = "2020-01-01T00:00:00.000000Z"
        recent = (datetime.now(timezone.utc) - timedelta(days=29)).isoformat(timespec="microseconds").replace("+00:00", "Z")
        conn.execute("UPDATE plugin_routing_decisions SET evaluated_at=? WHERE task_id=?", (old, "old-task"))
        conn.execute("UPDATE plugin_routing_decisions SET evaluated_at=? WHERE task_id=?", (recent, "kept-task"))
        _record(conn, task_id="new-task", result=_result("new"))
        assert latest_for_task(conn, "ns_local", "old-task") is None
        assert latest_for_task(conn, "ns_local", "kept-task")["chosen"] == "kept"
        assert last_chosen(conn, "ns_local", "implementer") == "new"

        for index in range(1000):
            _record(conn, task_id=f"bulk-{index}", result=_result(f"alias-{index}"))
        _record(conn, task_id="bulk-final", result=_result("final"))
        count = conn.execute("SELECT COUNT(*) FROM plugin_routing_decisions").fetchone()[0]
        assert count == 1000
        assert latest_for_task(conn, "ns_local", "bulk-0") is None
        assert latest_for_task(conn, "ns_local", "bulk-final")["chosen"] == "final"
        assert last_chosen(conn, "ns_local", "implementer") == "final"
    finally:
        conn.close()


def test_payload_keeps_only_candidate_score_fields_and_would_choose():
    conn = _connection()
    try:
        result = _result(
            "grok-b",
            mode="switch",
            reason_code="waste_margin",
            dispatch_changed=False,
            preference_enabled=True,
            windows_used=["weekly"],
        )
        result["candidates"] = [{
            "alias": "grok-a",
            "tier": 1,
            "S": 10,
            "S_A": 4,
            "excluded": False,
            "reason_codes": ["primary_unknown"],
            "U": 80,
            "windows_used": ["weekly"],
            "resets_at": "2026-10-21T00:00:00Z",
        }, {
            "alias": "grok-b",
            "tier": 0,
            "S": 30,
            "S_A": 12,
            "excluded": False,
            "reason_codes": [],
            "U": 10,
        }]
        _record(conn, task_id="task-payload", result=result, mode="switch")
        raw = conn.execute(
            "SELECT payload FROM plugin_routing_decisions WHERE task_id=?",
            ("task-payload",),
        ).fetchone()[0]
        parsed = json.loads(raw)
        assert raw == json.dumps(parsed, sort_keys=True)
        assert set(parsed) == {"candidates", "would_choose"}
        assert parsed["would_choose"] == "grok-b"
        assert [set(item) for item in parsed["candidates"]] == [_CANDIDATE_KEYS, _CANDIDATE_KEYS]
        assert parsed["candidates"][0]["reason_codes"] == ["primary_unknown"]
        assert "U" not in raw
        assert "windows_used" not in raw
        assert "dispatch_changed" not in raw
        stored = latest_for_task(conn, "ns_local", "task-payload")
        assert stored["payload"] == parsed
    finally:
        conn.close()


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


def test_evaluate_group_matches_explain_routing(config_path: Path, workspace_root: Path):
    config_path.write_text(json.dumps(_grok_config(workspace_root)), encoding="utf-8")
    app = Application.from_paths(config_path, config_path.parent / "routing-decisions")
    try:
        evaluated = evaluate_group(app, "implementer")
        explained = explain_routing(app, {"group": "implementer"})
        via_backend = explain_routing(app, {"backend": "@implementer"})
        assert explained["dispatch_changed"] is False
        assert explained["preference_enabled"] is False
        assert via_backend == explained
        rest = {key: value for key, value in explained.items() if key not in {"dispatch_changed", "preference_enabled"}}
        assert rest == evaluated
        assert "dispatch_changed" not in evaluated
        assert "preference_enabled" not in evaluated
    finally:
        app.close()


def _checkpoint(path: Path, *statements: str) -> None:
    with closing(sqlite3.connect(path)) as conn:
        for statement in statements:
            conn.execute(statement)
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def test_routing_table_is_optional_but_columns_must_match(tmp_path: Path):
    root = tmp_path / "source"
    root.mkdir()
    db = SqliteStore(root / DB_NAME)
    try:
        columns = {row[1] for row in db._conn.execute("PRAGMA table_info(plugin_routing_decisions)")}
        assert columns == ROUTING_TABLE_COLUMNS["plugin_routing_decisions"]
        indexed = [row[2] for row in db._conn.execute("PRAGMA index_info(idx_plugin_routing_decisions_key)")]
        assert indexed == ["namespace_id", "group_name", "evaluated_at"]
    finally:
        db.close()
    assert _validate_snapshot(root / DB_NAME) == 6
    _checkpoint(root / DB_NAME, "DROP TABLE plugin_routing_decisions")
    assert _validate_snapshot(root / DB_NAME) == 6
    _checkpoint(
        root / DB_NAME,
        """CREATE TABLE plugin_routing_decisions (
            namespace_id TEXT, id TEXT PRIMARY KEY, task_id TEXT, payload TEXT)""",
    )
    with pytest.raises(AsterunError) as caught:
        _validate_snapshot(root / DB_NAME)
    assert "表结构" in caught.value.message
