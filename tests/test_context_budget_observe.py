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


def _run(run_id, *, native=None, status=RunStatus.SUCCEEDED, role="implementation", task_id="task-budget"):
    return Run(
        RunId(run_id),
        TaskId(task_id),
        BackendId("fake"),
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

