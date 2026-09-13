import json
import os
import sqlite3
import sys
import time
import pytest

from asterun.application import Application
from asterun.sqlite_store import SqliteStore
from tests.conftest import write_config
from tests.test_native_lifecycle import wait_for


@pytest.mark.parametrize("old_version", ["2", "3"])
def test_schema_upgrade_preserves_snapshot_and_only_migrates_once(config_path, isolated_env, old_version):
    state = isolated_env / "state"
    app = Application.from_paths(config_path, state)
    task = app.handle("task.submit", {"workspace": "demo", "text": "旧任务"})
    app.store._conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (old_version,))
    app.close()
    app = Application.from_paths(config_path, state)
    snapshot = app.store.migration_backup
    try:
        assert app.handle("task.get", {"task_id": task.ids["task_id"]}).data["run"]["summary"] == "旧任务"
        conn = sqlite3.connect(snapshot)
        try:
            assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == old_version
            assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
        finally:
            conn.close()
    finally:
        app.close()
    app = Application.from_paths(config_path, state)
    assert app.store.migration_backup is None
    app.close()
    assert list(state.glob(f"*.schema-{old_version}.*.sqlite")) == [snapshot]


def test_workspace_path_change_cannot_rebind_existing_session(config_path, isolated_env):
    app = Application.from_paths(config_path, isolated_env / "state")
    task = app.handle("task.submit", {"workspace": "demo", "text": "原目录"})
    app.close()
    config = json.loads(config_path.read_text())
    replacement = isolated_env / "replacement"
    replacement.mkdir()
    config["workspaces"]["demo"]["root"] = str(replacement)
    config_path.write_text(json.dumps(config))
    app = Application.from_paths(config_path, isolated_env / "state")
    try:
        result = app.handle("session.resume", {"conversation_id": task.ids["conversation_id"]})
        assert not result.ok and result.error["code"] == "BINDING_MISMATCH"
    finally:
        app.close()


def test_claude_cancel_confirms_only_after_local_process_exit(isolated_env, workspace_root, monkeypatch):
    binary = isolated_env / "claude-stub"
    binary.write_text(f"#!{sys.executable}\n" + "import os, time\nfrom pathlib import Path\nPath('pid').write_text(str(os.getpid()))\ntime.sleep(30)\n")
    binary.chmod(0o700)
    monkeypatch.setenv("ASTERUN_RUN_CLAUDE", "1")
    config = write_config(isolated_env / "config.json", workspace_root,
                          extra_backends={"claude": {"kind": "claude", "bin": str(binary)}})
    app = Application.from_paths(config, isolated_env / "state", background=True)
    try:
        result = app.handle("task.submit", {"workspace": "demo", "backend": "claude", "text": "等待取消"})
        assert result.ok
        deadline = time.monotonic() + 5
        while not (workspace_root / "pid").exists() and time.monotonic() < deadline:
            app.poll()
            time.sleep(0.02)
        assert (workspace_root / "pid").exists()
        process = app.backends["claude"]._sessions[result.ids["run_id"]].proc
        cancelled = app.handle("task.cancel", {"task_id": result.ids["task_id"]})
        assert cancelled.ok and not cancelled.data["terminated"]
        wait_for(app, result.ids["task_id"], lambda data: data["run"]["status"] == "cancelled")
        assert process.poll() is not None
    finally:
        app.close()
