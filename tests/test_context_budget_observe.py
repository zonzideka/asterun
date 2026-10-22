"""context_budget observe：终态运行写一行幂等观测，失败不影响 save_run。"""
from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from asterun.application import Application
from asterun.config_migration import migrate_v1_to_v2
from asterun.config_v2 import context_budget_settings, default_context_budget
from asterun.context_budget.observe import latest_for_task, list_observations, observe_run
from asterun.contracts import AcceptanceStatus, BackendId, DeliveryStatus, Run, RunId, RunStatus, Task, TaskId
from asterun.plugin_store import CONTEXT_BUDGET_TABLE_COLUMNS


@pytest.fixture
def managed(config_path):
    raw = migrate_v1_to_v2(config_path)["config"]
    config_path.write_text(json.dumps(raw))
    app = Application.from_paths(config_path, config_path.parent / "managed-state")
    try:
        yield app
    finally:
        app.close()


def _native(*, stop_reason="max_turn", input_tokens=40, cache_read=10, cache_write=5, output=15):
    return {
        "usage_source": "native_headless_report",
        "usage_scope": "run",
        "stop_reason": stop_reason,
        "usage": {
            "input_tokens": input_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_write,
            "output_tokens": output,
            "total_tokens": input_tokens + cache_read + cache_write + output,
        },
    }


def _run(run_id, *, native=None, status=RunStatus.SUCCEEDED, role="implementation", task_id="task-budget", backend="fake"):
    return Run(
        RunId(run_id),
        TaskId(task_id),
        BackendId(backend),
        status,
        "2026-10-21T00:00:00Z",
        terminated=status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED},
        native={} if native is None else native,
        role=role,
    )


def _enable(app, **profile):
    budget = default_context_budget()
    budget["mode"] = "observe"
    budget["segmentation"]["backends"] = ["fake"]
    budget["segmentation"]["roles"] = ["implementation", "repair"]
    budget["segmentation"]["min_long_task_processed"] = 60
    budget["segmentation"]["observe_capsule_min_processed"] = 60
    budget["profiles"]["fake/implementation"] = {
        "segment_soft": 50,
        "task_soft": 60,
        "task_hard": 200,
        "max_segments": 1,
    }
    budget["profiles"]["fake/implementation"].update(profile)
    app.config.context_budget = budget
    return budget


def _count(conn) -> int:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_budget_observations'"
    ).fetchone()
    if row is None:
        return 0
    return conn.execute("SELECT COUNT(*) FROM context_budget_observations").fetchone()[0]


def test_enforce_writes_observation_row(managed):
    _enable(managed)
    managed.config.context_budget["mode"] = "enforce"
    assert context_budget_settings(managed.config)["mode"] == "enforce"
    managed.store.save_run(_run("run-enforce", native=_native()))
    conn = managed.store._conn
    assert _count(conn) == 1
    row = latest_for_task(conn, "task-budget")
    assert row["run_id"] == "run-enforce"
    assert row["profile_key"] == "fake/implementation"
    assert row["run_processed"] == 70


def test_off_mode_does_not_write(managed):
    budget = default_context_budget()
    budget["mode"] = "off"
    managed.config.context_budget = budget
    assert context_budget_settings(managed.config)["mode"] == "off"
    managed.store.save_run(_run("run-off", native=_native()))
    assert observe_run(managed, _run("run-off-direct", native=_native())) == 0
    assert _count(managed.store._conn) == 0


def test_observe_writes_one_row_with_flags(managed):
    _enable(managed)
    managed.store.save_run(_run("run-gap", native={}))
    run = _run("run-main", native=_native())
    managed.store.save_run(run)
    conn = managed.store._conn
    assert _count(conn) == 2
    rows = list_observations(conn, task_id="task-budget")
    main = next(item for item in rows if item["run_id"] == "run-main")
    assert main["profile_key"] == "fake/implementation"
    assert main["backend"] == "fake"
    assert main["role"] == "implementation"
    assert main["run_processed"] == 70
    assert main["run_new"] == 60
    assert main["task_processed"] == 70
    assert main["task_new"] == 60
    assert main["unknown_runs"] == 1
    assert main["flags"] == {
        "segment_soft_exceeded": True,
        "task_soft_exceeded": True,
        "task_hard_exceeded": False,
        "max_segments_exceeded": True,
        "long_task": True,
        "would_segment": True,
        "capsule_candidate": True,
    }
    expected_sha = hashlib.sha256(
        json.dumps(context_budget_settings(managed.config), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert main["policy_sha256"] == expected_sha
    assert latest_for_task(conn, "task-budget")["run_id"] == "run-main"
    columns = {row[1] for row in conn.execute("PRAGMA table_info(context_budget_observations)")}
    assert columns == CONTEXT_BUDGET_TABLE_COLUMNS["context_budget_observations"]
    indexed = [row[2] for row in conn.execute("PRAGMA index_info(idx_context_budget_observations_key)")]
    assert indexed == ["namespace_id", "task_id", "observed_at"]
    same = conn.execute(
        "SELECT COUNT(*) FROM context_budget_observations WHERE run_id=?",
        ("run-main",),
    ).fetchone()[0]
    assert same == 1


def test_repeat_save_keeps_one_row(managed):
    _enable(managed)
    run = _run("run-once", native=_native())
    managed.store.save_run(run)
    run.native = _native(input_tokens=80, cache_read=10, cache_write=5, output=15)
    managed.store.save_run(run)
    conn = managed.store._conn
    assert _count(conn) == 1
    row = list_observations(conn, task_id="task-budget")[0]
    assert row["run_id"] == "run-once"
    assert row["run_processed"] == 110
    assert row["task_processed"] == 110


def test_observe_error_does_not_block_save_or_quota_hook(managed, monkeypatch):
    _enable(managed)
    seen = {}

    def spy(config):
        seen["quota"] = True
        from asterun.quota.settings import codex_quota_settings
        return codex_quota_settings(config)

    def boom(*args, **kwargs):
        raise RuntimeError("observe exploded")

    monkeypatch.setattr("asterun.quota.passive.codex_quota_settings", spy)
    monkeypatch.setattr("asterun.context_budget.observe.normalize_breakdown", boom)
    run = _run("run-boom", native=_native())
    managed.store.save_run(run)
    assert managed.store.get_run(run.id).id.value == "run-boom"
    assert seen["quota"] is True
    assert observe_run(managed, run) == 0
    assert list_observations(managed.store._conn, task_id="task-budget") == []


def test_list_observations_missing_table_does_not_create():
    conn = sqlite3.connect(":memory:")
    try:
        assert list_observations(conn) == []
        assert list_observations(conn, task_id="task-budget") == []
        assert latest_for_task(conn, "task-budget") is None
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
    finally:
        conn.close()


def _task(task_id="task-budget", current_run_id=None):
    return Task(
        id=TaskId(task_id),
        workspace="demo",
        backend=BackendId("fake"),
        text="offline",
        script="success",
        acceptance=AcceptanceStatus.NOT_CONFIGURED,
        delivery=DeliveryStatus.NOT_CONFIGURED,
        created_at="2026-10-21T00:00:00Z",
        current_run_id=current_run_id,
    )


def test_task_get_omits_key_without_rows_and_includes_it_with_a_row(managed):
    managed.store.save_task(_task())
    missing = managed.handle("task.get", {"task_id": "task-budget"})
    assert missing.ok, missing.error
    assert "context_budget" not in missing.data

    _enable(managed)
    run = _run("run-visible", native=_native())
    managed.store.save_task(_task(current_run_id=run.id))
    managed.store.save_run(run)
    present = managed.handle("task.get", {"task_id": "task-budget"})
    assert present.ok, present.error
    assert present.data["context_budget"]["run_id"] == "run-visible"
    assert present.data["context_budget"]["flags"]["would_segment"] is True


def test_inspect_missing_table_returns_empty_and_does_not_create(managed):
    conn = managed.store._conn
    conn.execute("DROP TABLE context_budget_observations")
    result = managed.handle("context_budget.inspect", {"task_id": "task-budget", "limit": 1})
    assert result.ok, result.error
    assert result.data == {"observations": [], "mode": context_budget_settings(managed.config)["mode"]}
    left = conn.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE '%context_budget%'"
    ).fetchall()
    assert left == []


def test_inspect_returns_saved_rows_without_writing(managed):
    _enable(managed)
    managed.store.save_run(_run("run-listed", native=_native()))
    before = _count(managed.store._conn)
    result = managed.handle("context_budget.inspect", {"task_id": "task-budget"})
    assert result.ok, result.error
    assert result.data["mode"] == "observe"
    assert [item["run_id"] for item in result.data["observations"]] == ["run-listed"]
    assert _count(managed.store._conn) == before


def test_cli_and_mcp_whitelist_context_budget_inspect():
    from asterun.cli import COMMAND_METHODS, _load_request, build_parser
    from asterun.mcp_server import TOOL_METHODS
    from asterun.requests import REQUEST_SCHEMAS

    args = build_parser().parse_args(["context-budget-inspect", "--task", "task-budget", "--limit", "5"])
    assert args.command == "context-budget-inspect"
    assert COMMAND_METHODS[args.command] == "context_budget.inspect"
    assert _load_request(args) == {"task_id": "task-budget", "limit": 5}
    assert TOOL_METHODS["context_budget_inspect"] == "context_budget.inspect"
    assert REQUEST_SCHEMAS["context_budget.inspect"]["properties"]["limit"] == {
        "type": "integer", "minimum": 1, "maximum": 200,
    }


def test_task_get_compact_projects_context_budget_and_leaves_full_view(managed):
    from asterun.observe import compact_task_snapshot

    managed.store.save_task(_task())
    missing = managed.handle("task.get", {"task_id": "task-budget", "compact": True})
    assert missing.ok, missing.error
    assert "context_budget" not in missing.data
    bare = {"task": {"id": "tsk_1", "acceptance": "pending"}, "run": None}
    projected = compact_task_snapshot(bare)
    assert json.dumps(projected, ensure_ascii=False) == (
        '{"compact": true, "task": {"id": "tsk_1", "acceptance": "pending"}, "run": null, '
        '"details": {"method": "task.get", "compact": false, "task_id": "tsk_1"}}'
    )

    _enable(managed)
    run = _run("run-visible", native=_native())
    managed.store.save_task(_task(current_run_id=run.id))
    managed.store.save_run(run)
    full = managed.handle("task.get", {"task_id": "task-budget"})
    assert full.ok, full.error
    assert set(full.data["context_budget"]) == {
        "namespace_id", "run_id", "task_id", "profile_key", "backend", "role",
        "run_processed", "run_new", "task_processed", "task_new", "unknown_runs",
        "flags", "policy_sha256", "observed_at",
    }
    assert "flags_true" not in full.data["context_budget"]
    compact = managed.handle("task.get", {"task_id": "task-budget", "compact": True})
    assert compact.ok, compact.error
    assert compact.data["context_budget"] == {
        "profile_key": "fake/implementation",
        "task_processed": 70,
        "task_new": 60,
        "run_processed": 70,
        "flags_true": [
            "capsule_candidate", "long_task", "segment_soft_exceeded",
            "task_soft_exceeded", "would_segment",
        ],
        "observed_at": full.data["context_budget"]["observed_at"],
    }
    assert compact_task_snapshot(compact.data) == compact.data


def test_compact_context_budget_stays_inside_projection_limits():
    from asterun.observe import COMPACT_FIELD_BYTES, COMPACT_USAGE_BYTES, compact_task_snapshot

    snapshot = {
        "task": {"id": "tsk_1", "acceptance": "pending"},
        "run": {
            "id": "run_1", "task_id": "tsk_1", "status": "succeeded", "terminated": True,
            "native": {"usage": {"input_tokens": 50, "unknown_vendor_payload": "大" * 350000}},
        },
        "context_budget": {
            "profile_key": "x" * 100000,
            "task_processed": 70,
            "task_new": 60,
            "run_processed": 70,
            "observed_at": "2026-10-21T00:00:00.000000Z",
            "flags": {f"flag-{index:04d}": True for index in range(5000)} | {"task_soft_exceeded": False},
            "policy_sha256": "ab" * 200000,
            "run_id": "run_1",
        },
    }
    result = compact_task_snapshot(snapshot)
    budget = result["context_budget"]
    assert "profile_key" not in budget
    assert budget["omitted_fields"] == ["profile_key"]
    assert budget["task_processed"] == 70 and budget["task_new"] == 60 and budget["run_processed"] == 70
    assert budget["observed_at"] == "2026-10-21T00:00:00.000000Z"
    assert "task_soft_exceeded" not in budget["flags_true"]
    assert "policy_sha256" not in budget and "run_id" not in budget and "flags" not in budget
    assert len(json.dumps(budget["flags_true"], ensure_ascii=False).encode()) <= COMPACT_FIELD_BYTES
    native = result["run"]["native"]
    assert native["usage"] == {"input_tokens": 50}
    assert native["usage_truncated"] is True
    assert native["usage_byte_limit"] == COMPACT_USAGE_BYTES
    assert len(json.dumps(result, ensure_ascii=False).encode()) < 32000
    assert compact_task_snapshot(result) == result
    assert "unknown_vendor_payload" in snapshot["run"]["native"]["usage"]


def _codex_budget(app, profiles):
    from asterun.config import BackendConfig

    app.config.backends["codex"] = BackendConfig(name="codex", kind="codex", enabled=True)
    budget = default_context_budget()
    budget["mode"] = "observe"
    budget["profiles"] = profiles
    app.config.context_budget = budget
    return budget


def test_codex_implementation_run_uses_review_profile_when_only_review_is_configured(managed):
    _codex_budget(managed, {"codex/review": {
        "segment_soft": 50, "task_soft": 60, "task_hard": 200, "max_segments": 1,
    }})
    run = _run("run-codex-review", native=_native(), backend="codex")
    managed.store.save_run(run)
    assert run.role == "implementation"
    row = latest_for_task(managed.store._conn, "task-budget")
    assert row["profile_key"] == "codex/review"
    assert row["role"] == "review"
    assert row["backend"] == "codex"
    assert row["flags"]["segment_soft_exceeded"] is True
    assert row["flags"]["task_soft_exceeded"] is True
    assert row["flags"]["task_hard_exceeded"] is False
    assert run.role == "implementation"


def test_codex_implementation_profile_wins_when_it_is_configured(managed):
    _codex_budget(managed, {
        "codex/implementation": {
            "segment_soft": 1000, "task_soft": 1000, "task_hard": 2000, "max_segments": 5,
        },
        "codex/review": {
            "segment_soft": 50, "task_soft": 60, "task_hard": 200, "max_segments": 1,
        },
    })
    run = _run("run-codex-impl", native=_native(), backend="codex")
    managed.store.save_run(run)
    row = latest_for_task(managed.store._conn, "task-budget")
    assert row["profile_key"] == "codex/implementation"
    assert row["role"] == "implementation"
    assert row["flags"]["segment_soft_exceeded"] is False
    assert row["flags"]["task_soft_exceeded"] is False
    assert run.role == "implementation"


def test_unknown_backend_keeps_implementation_profile_key(managed):
    managed.config.backends.pop("codex", None)
    budget = default_context_budget()
    budget["mode"] = "observe"
    budget["profiles"] = {"codex/review": {
        "segment_soft": 50, "task_soft": 60, "task_hard": 200, "max_segments": 1,
    }}
    managed.config.context_budget = budget
    run = _run("run-codex-missing", native=_native(), backend="codex")
    managed.store.save_run(run)
    row = latest_for_task(managed.store._conn, "task-budget")
    assert row["profile_key"] == "codex/implementation"
    assert row["role"] == "implementation"
    assert row["flags"]["segment_soft_exceeded"] is False
    assert run.role == "implementation"

