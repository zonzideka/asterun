from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from asterun.application import Application
from asterun.backends.fake import FakeBackend
from asterun.config import load_config
from asterun.contracts import SessionBinding
from asterun.ids import AccountRef, BackendId, BackendSessionId, ConversationId, RuntimeRef
from asterun.policy import Grant, Principal
from asterun.sqlite_store import SCHEMA_VERSION, SqliteStore
from asterun.store import MemoryStore
from tests.conftest import write_config

V1_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operations (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    principal TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    task_id TEXT,
    run_id TEXT,
    UNIQUE (principal, action, target, idempotency_key)
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
"""


def _app(config_path: Path, store=None, **kwargs) -> Application:
    defaults = {
        "store": store or MemoryStore(),
        "principal": Principal("test:user", "test"),
        "account_ref": AccountRef("account:test"),
        "runtime_ref": RuntimeRef("host:test"),
    }
    defaults.update(kwargs)
    return Application(load_config(config_path), **defaults)


def test_submit_creates_session_and_resume_reattaches_index(config_path: Path) -> None:
    store = MemoryStore()
    app = _app(config_path, store=store)
    submitted = app.handle("task.submit", {"workspace": "demo", "script": "success"})
    assert submitted.ok
    conversation_id = submitted.ids["conversation_id"]
    assert submitted.data["session"]["account_ref"] == "account:test"
    assert submitted.data["session"]["runtime_ref"] == "host:test"
    assert submitted.data["session"]["backend_session_id"] is None
    missing = app.handle("session.resume", {"workspace": "demo"})
    assert missing.error["code"] == "INVALID_REQUEST"
    resumed = app.handle("session.resume", {"conversation_id": conversation_id})
    assert resumed.ok
    assert resumed.data["index_found"] is True
    assert resumed.data["binding_ok"] is True
    assert resumed.data["local_reattached"] is True
    assert resumed.data["native_checked"] is False
    assert resumed.data["native_resumed"] is False
    assert resumed.data["notes"]["native_resume_is_not_index_resume"] is True


def test_restart_reads_session_index(config_path: Path, isolated_env: Path) -> None:
    state = isolated_env / "state"
    first = Application.from_paths(
        config_path,
        state,
        principal=Principal("test:user", "test"),
        account_ref=AccountRef("account:test"),
        runtime_ref=RuntimeRef("host:test"),
    )
    try:
        submitted = first.handle("task.submit", {"workspace": "demo", "script": "success"})
        conversation_id = submitted.ids["conversation_id"]
        first.close()
        second = Application.from_paths(
            config_path,
            state,
            principal=Principal("test:user", "test"),
            account_ref=AccountRef("account:test"),
            runtime_ref=RuntimeRef("host:test"),
        )
        try:
            resumed = second.handle("session.resume", {"conversation_id": conversation_id})
            assert resumed.ok
            assert resumed.data["native_resumed"] is False
            fetched = second.handle("task.get", {"task_id": submitted.ids["task_id"]})
            assert fetched.data["task"]["conversation_id"] == conversation_id
        finally:
            second.close()
    finally:
        if first.instance_lock is not None:
            try:
                first.close()
            except Exception:
                pass


def test_account_or_host_change_does_not_reuse_thread(config_path: Path) -> None:
    store = MemoryStore()
    app = _app(config_path, store=store)
    fake = FakeBackend(load_config(config_path).backends["fake"])
    app.backends["fake"] = fake
    submitted = app.handle("task.submit", {"workspace": "demo", "script": "success"})
    conversation_id = submitted.ids["conversation_id"]
    other = _app(
        config_path,
        store=store,
        account_ref=AccountRef("account:other"),
        runtime_ref=RuntimeRef("host:test"),
        backends={"fake": fake},
    )
    resumed = other.handle("session.resume", {"conversation_id": conversation_id})
    assert resumed.ok is False
    assert resumed.error["code"] == "BINDING_MISMATCH"
    assert "account_ref" in resumed.error["details"]["mismatches"]
    assert "backend_session_id" not in resumed.error["details"]
    assert fake.dispatch_count == 1
    host = _app(
        config_path,
        store=store,
        account_ref=AccountRef("account:test"),
        runtime_ref=RuntimeRef("host:other"),
        backends={"fake": fake},
    )
    host_resume = host.handle("session.resume", {"conversation_id": conversation_id})
    assert host_resume.error["code"] == "BINDING_MISMATCH"
    assert fake.dispatch_count == 1


def test_config_cas_does_not_overwrite(isolated_env: Path, workspace_root: Path) -> None:
    path = write_config(isolated_env / "config.json", workspace_root)
    store = MemoryStore()
    first = _app(path, store=store)
    assert first.handle("config.validate", {}).data["applied_config_revision"] == 1
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["revision"] = 2
    path.write_text(json.dumps(raw), encoding="utf-8")
    second = _app(path, store=store)
    blocked = second.handle("task.submit", {"workspace": "demo", "script": "success"})
    assert blocked.error["code"] == "REVISION_CONFLICT"
    applied = second.handle("config.apply", {"expected_revision": 1})
    assert applied.ok
    assert applied.data["applied_config_revision"] == 2
    conflict = second.handle("config.apply", {"expected_revision": 1})
    assert conflict.error["code"] == "REVISION_CONFLICT"
    submitted = second.handle("task.submit", {"workspace": "demo", "script": "success"})
    assert submitted.ok


def test_expired_grant_does_not_dispatch(config_path: Path) -> None:
    config = load_config(config_path)
    fake = FakeBackend(config.backends["fake"])
    app = Application(
        config,
        store=MemoryStore(),
        backends={"fake": fake},
        principal=Principal("test:user", "test"),
        grant=Grant(expires_at="2020-01-01T00:00:00Z"),
        account_ref=AccountRef("account:test"),
        runtime_ref=RuntimeRef("host:test"),
    )
    result = app.handle("task.submit", {"workspace": "demo", "script": "success"})
    assert result.ok is False
    assert result.error["code"] == "AUTH_REQUIRED"
    assert result.error["details"]["grant"] == "expired"
    assert fake.dispatch_count == 0
    assert app.store.tasks == {}


def test_self_reported_runtime_ref_is_rejected(config_path: Path) -> None:
    app = _app(config_path)
    result = app.handle(
        "task.submit",
        {"workspace": "demo", "script": "success", "runtime_ref": "host:forged"},
    )
    assert result.error["code"] == "SCOPE_DENIED"
    assert app.store.sessions == {}


def test_native_session_lookup_and_schema_upgrade(tmp_path: Path, isolated_env: Path, workspace_root: Path) -> None:
    db = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(V1_SCHEMA)
    conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', '1')")
    conn.commit()
    conn.close()
    store = SqliteStore(db)
    assert SCHEMA_VERSION == 6
    version = store._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    assert version["value"] == "6"
    assert store.migration_backup.is_file()
    assert store.migration_backup.stat().st_mode & 0o777 == 0o600
    backup = sqlite3.connect(store.migration_backup)
    try:
        assert backup.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "1"
    finally:
        backup.close()
    now = "2026-09-07T00:00:00Z"
    session = SessionBinding(
        conversation_id=ConversationId("con_legacy"),
        backend=BackendId("codex"),
        workspace="demo",
        account_ref=AccountRef("account:test"),
        runtime_ref=RuntimeRef("host:test"),
        created_at=now,
        updated_at=now,
        backend_session_id=BackendSessionId("thread-native-1"),
        config_revision=1,
    )
    store.save_session(session)
    found = store.find_session_by_native("codex", "thread-native-1")
    assert found is not None
    assert found.conversation_id.value == "con_legacy"
    path = write_config(isolated_env / "config.json", workspace_root)
    app = Application(
        load_config(path),
        store=store,
        principal=Principal("test:user", "test"),
        account_ref=AccountRef("account:other"),
        runtime_ref=RuntimeRef("host:test"),
    )
    resumed = app.handle(
        "session.resume",
        {"session_id": "thread-native-1", "backend": "codex"},
    )
    assert resumed.error["code"] == "BINDING_MISMATCH"
    assert resumed.data is None

    app.close()
