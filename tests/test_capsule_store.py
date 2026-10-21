"""上下文胶囊存储：observe 只生成不派发，inspect 只读，备份可选表。"""
from __future__ import annotations

from contextlib import closing
import json
import sqlite3

import pytest

from asterun.application import Application
from asterun.backup import DB_NAME, _validate_snapshot, backup_state, restore_state
from asterun.capsule.assemble import verify_capsule
from asterun.capsule.model import CapsuleTooLarge
from asterun.capsule.store import list_capsules, load_capsule, record_capsule
from asterun.config_migration import migrate_v1_to_v2
from asterun.config_v2 import default_context_budget
from asterun.context_budget.observe import list_observations
from asterun.contracts import AcceptanceStatus, BackendId, DeliveryStatus, Run, RunId, RunStatus, Task, TaskId
from asterun.errors import AsterunError
from asterun.plugin_store import CAPSULE_TABLE_COLUMNS
from asterun.sqlite_store import SCHEMA_VERSION, SqliteStore


@pytest.fixture
def managed(config_path):
    raw = migrate_v1_to_v2(config_path)["config"]
    config_path.write_text(json.dumps(raw))
    app = Application.from_paths(config_path, config_path.parent / "managed-state")
    try:
        yield app
    finally:
        app.close()


def _native():
    return {
        "usage_source": "native_headless_report",
        "usage_scope": "run",
        "stop_reason": "max_turn",
        "usage": {
            "input_tokens": 40,
            "cache_read_input_tokens": 10,
            "cache_creation_input_tokens": 5,
            "output_tokens": 15,
            "total_tokens": 70,
        },
    }


def _task(task_id="task-cap", text="胶囊任务原文甲\n逐字节"):
    return Task(
        id=TaskId(task_id),
        workspace="demo",
        backend=BackendId("fake"),
        text=text,
        script="success",
        acceptance=AcceptanceStatus.NOT_CONFIGURED,
        delivery=DeliveryStatus.NOT_CONFIGURED,
        created_at="2026-10-21T00:00:00Z",
    )


def _run(run_id="run-cap", task_id="task-cap"):
    return Run(
        RunId(run_id),
        TaskId(task_id),
        BackendId("fake"),
        RunStatus.SUCCEEDED,
        "2026-10-21T00:00:00Z",
        terminated=True,
        native=_native(),
        role="implementation",
    )


def _enable(app, *, mode="observe", markers=False):
    budget = default_context_budget()
    budget["mode"] = mode
    budget["segmentation"]["observe_capsule_min_processed"] = 0
    budget["capsule"]["progress_markers"] = markers
    app.config.context_budget = budget
    return budget


def _capsule_count(conn) -> int:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_capsules'"
    ).fetchone()
    if row is None:
        return 0
    return conn.execute("SELECT COUNT(*) FROM context_capsules").fetchone()[0]


def _blob_count(conn) -> int:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='control_blobs'"
    ).fetchone()
    if row is None:
        return 0
    return conn.execute(
        "SELECT COUNT(*) FROM control_blobs WHERE namespace_id=?",
        ("asterun_checkpoint_v1",),
    ).fetchone()[0]


def test_observe_long_task_writes_ready_zero_and_a_loadable_capsule(managed):
    _enable(managed, markers=False)
    task = _task()
    run = _run()
    managed.store.save_task(task)
    managed.store.save_run(run)
    conn = managed.store._conn
    rows = list_capsules(conn, task_id="task-cap")
    assert len(rows) == 1
    row = rows[0]
    assert row["ready"] == 0
    assert row["trigger"] == "observe_only"
    assert row["error_code"] is None
    assert row["issues"] == []
    assert row["blob_sha256"]
    loaded = load_capsule(managed, row["blob_sha256"])
    stored_task = managed.store.get_task(task.id)
    assert verify_capsule(managed, stored_task, loaded) == []
    assert loaded["mandatory"]["objective"] == task.text
    columns = {item[1] for item in conn.execute("PRAGMA table_info(context_capsules)")}
    assert columns == CAPSULE_TABLE_COLUMNS["context_capsules"]
    indexed = [item[2] for item in conn.execute("PRAGMA index_info(idx_context_capsules_key)")]
    assert indexed == ["namespace_id", "task_id", "created_at"]
    manual = record_capsule(managed, stored_task, managed.store.get_run(run.id), trigger="manual")
    assert manual["ready"] == 1
    assert manual["issues"] == []
    assert _capsule_count(conn) == 2


def test_off_mode_does_not_generate(managed, monkeypatch):
    called = []

    def spy(*args, **kwargs):
        called.append(1)
        raise AssertionError("off 模式不应生成胶囊")

    monkeypatch.setattr("asterun.capsule.store.record_capsule", spy)
    _enable(managed, mode="off", markers=True)
    managed.store.save_task(_task())
    managed.store.save_run(_run("run-off"))
    assert called == []
    assert _capsule_count(managed.store._conn) == 0
    assert list_observations(managed.store._conn, task_id="task-cap") == []


def test_repeat_same_run_updates_one_row(managed):
    _enable(managed, markers=True)
    task = _task()
    run = _run()
    managed.store.save_task(task)
    managed.store.save_run(run)
    first = list_capsules(managed.store._conn, task_id="task-cap")[0]["body_sha256"]
    stored = managed.store.get_task(task.id)
    stored.text = stored.text + "\n又一行"
    managed.store.save_task(stored)
    managed.store.save_run(run)
    rows = list_capsules(managed.store._conn, task_id="task-cap")
    assert len(rows) == 1
    assert rows[0]["run_id"] == "run-cap"
    assert rows[0]["trigger"] == "observe_only"
    assert rows[0]["ready"] == 0
    assert rows[0]["body_sha256"] != first
    loaded = load_capsule(managed, rows[0]["blob_sha256"])
    assert loaded["mandatory"]["objective"].endswith("又一行")


def test_capsule_too_large_records_error_without_blob(managed, monkeypatch):
    def explode(*args, **kwargs):
        raise CapsuleTooLarge(30000)

    monkeypatch.setattr("asterun.capsule.store.assemble_capsule", explode)
    _enable(managed)
    before = _blob_count(managed.store._conn)
    managed.store.save_task(_task())
    managed.store.save_run(_run())
    rows = list_capsules(managed.store._conn, task_id="task-cap")
    assert len(rows) == 1
    assert rows[0]["error_code"] == "CAPSULE_TOO_LARGE"
    assert rows[0]["blob_sha256"] is None
    assert rows[0]["ready"] == 0
    assert _blob_count(managed.store._conn) == before


def test_record_error_does_not_block_save_or_observation(managed, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("capsule failed")

    monkeypatch.setattr("asterun.capsule.store.record_capsule", boom)
    _enable(managed, markers=False)
    task = _task()
    run = _run("run-boom")
    managed.store.save_task(task)
    managed.store.save_run(run)
    assert managed.store.get_run(run.id).id.value == "run-boom"
    observations = list_observations(managed.store._conn, task_id="task-cap")
    assert [item["run_id"] for item in observations] == ["run-boom"]
    assert _capsule_count(managed.store._conn) == 0


def test_list_capsules_missing_table_does_not_create():
    conn = sqlite3.connect(":memory:")
    try:
        assert list_capsules(conn) == []
        assert list_capsules(conn, task_id="task-cap") == []
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
    finally:
        conn.close()


def test_inspect_missing_table_returns_empty_and_does_not_create(managed):
    managed.store.save_task(_task())
    conn = managed.store._conn
    conn.execute("DROP TABLE context_capsules")
    missing = managed.handle("capsule.inspect", {"task_id": "task-cap", "limit": 1})
    assert missing.ok, missing.error
    assert missing.data == {"capsules": []}
    rendered = managed.handle("capsule.inspect", {"task_id": "task-cap", "render": True})
    assert rendered.ok, rendered.error
    assert rendered.data["capsules"] == []
    assert rendered.data["verify_issues"] == []
    assert rendered.data["rendered_prompt"] is None
    left = conn.execute(
        "SELECT name FROM sqlite_master WHERE name='context_capsules' OR name='idx_context_capsules_key'"
    ).fetchall()
    assert left == []


def test_inspect_render_returns_task_text(managed):
    _enable(managed, markers=False)
    task = _task()
    managed.store.save_task(task)
    managed.store.save_run(_run())
    before = _capsule_count(managed.store._conn)
    result = managed.handle("capsule.inspect", {"task_id": "task-cap", "render": True})
    assert result.ok, result.error
    assert result.data["verify_issues"] == []
    assert task.text in result.data["rendered_prompt"]
    assert result.data["capsules"][0]["ready"] == 0
    assert _capsule_count(managed.store._conn) == before


def test_backup_without_capsule_table_still_restores(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    db = SqliteStore(root / DB_NAME)
    try:
        assert SCHEMA_VERSION == 6
        columns = {row[1] for row in db._conn.execute("PRAGMA table_info(context_capsules)")}
        assert columns == CAPSULE_TABLE_COLUMNS["context_capsules"]
    finally:
        db.close()
    with closing(sqlite3.connect(root / DB_NAME)) as conn:
        conn.execute("DROP TABLE context_capsules")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert _validate_snapshot(root / DB_NAME) == 6
    archive = tmp_path / "old.tar.gz"
    assert backup_state(root, archive)["schema_version"] == 6
    restored = tmp_path / "restored"
    assert restore_state(archive, restored)["schema_version"] == 6
    with closing(sqlite3.connect(restored / DB_NAME)) as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "tasks" in names
    assert "context_capsules" not in names


def test_backup_with_capsule_table_restores_the_row(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    db = SqliteStore(root / DB_NAME)
    db.close()
    with closing(sqlite3.connect(root / DB_NAME)) as conn:
        conn.execute(
            """INSERT INTO context_capsules(
                namespace_id, capsule_id, task_id, run_id, segment_index, trigger,
                body_sha256, blob_sha256, size_bytes, truncated, ready, issues,
                error_code, created_at)
               VALUES('ns_local', NULL, 'task-cap', 'run-cap', 0, 'observe_only',
                      NULL, NULL, NULL, 0, 0, '[]', 'CAPSULE_TOO_LARGE', '2026-10-21T00:00:00Z')"""
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    archive = tmp_path / "with.tar.gz"
    assert backup_state(root, archive)["schema_version"] == 6
    restored = tmp_path / "restored"
    assert restore_state(archive, restored)["schema_version"] == 6
    with closing(sqlite3.connect(restored / DB_NAME)) as conn:
        row = conn.execute(
            "SELECT error_code, ready, blob_sha256 FROM context_capsules"
        ).fetchone()
    assert row == ("CAPSULE_TOO_LARGE", 0, None)


def test_capsule_backup_rejects_column_mismatch(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    db = SqliteStore(root / DB_NAME)
    db.close()
    with closing(sqlite3.connect(root / DB_NAME)) as conn:
        conn.execute("DROP TABLE context_capsules")
        conn.execute(
            """CREATE TABLE context_capsules (
                namespace_id TEXT, run_id TEXT, trigger TEXT, payload TEXT,
                PRIMARY KEY(namespace_id, run_id, trigger))"""
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    with pytest.raises(AsterunError) as caught:
        _validate_snapshot(root / DB_NAME)
    assert "表结构" in caught.value.message
    with pytest.raises(AsterunError, match="表结构"):
        backup_state(root, tmp_path / "invalid.tar.gz")


def test_cli_and_mcp_whitelist_capsule_inspect():
    import inspect

    from asterun import cli as cli_module
    from asterun.cli import COMMAND_METHODS, _load_request, build_parser
    from asterun.mcp_server import TOOL_METHODS
    from asterun.requests import REQUEST_SCHEMAS

    args = build_parser().parse_args(
        ["capsule-inspect", "--task", "task-cap", "--limit", "5", "--render"]
    )
    assert args.command == "capsule-inspect"
    assert COMMAND_METHODS[args.command] == "capsule.inspect"
    assert _load_request(args) == {"task_id": "task-cap", "limit": 5, "render": True}
    assert TOOL_METHODS["capsule_inspect"] == "capsule.inspect"
    assert "task_id" in REQUEST_SCHEMAS["capsule.inspect"]["required"]
    assert REQUEST_SCHEMAS["capsule.inspect"]["properties"]["limit"] == {
        "type": "integer", "minimum": 1, "maximum": 200,
    }
    assert REQUEST_SCHEMAS["capsule.inspect"]["properties"]["render"] == {"type": "boolean"}
    assert '"capsule-inspect"' in inspect.getsource(cli_module._app)
