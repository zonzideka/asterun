"""故障注入：结果落盘失败、多步写入原子性、重启时缺少操作记录。"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from asterun.application import Application
from asterun.backends.fake import FakeBackend
from asterun.config import load_config
from asterun.contracts import UNAPPLIED_RESULT_NATIVE_KEY, ApprovalState, OperationStatus, RunStatus
from asterun.errors import REMOTE_STATE_UNKNOWN
from asterun.ids import ApprovalId, RunId
from asterun.policy import AllowConfiguredWorkspaces, Principal
from asterun.sqlite_store import SqliteStore
from tests.conftest import write_config

KEPT_SUMMARY = "backend finished"
KEPT_SESSION = "sess-kept"
KEPT_MARKER = "kept-result-marker"


class StreamBackend:
    """kind 不是 fake，结果经后台执行器回到核心线程。"""

    def __init__(self, config, scripts: dict[str, dict]) -> None:
        self.config = config
        self.scripts = scripts
        self.dispatch_count = 0
        self.calls: list[str] = []

    def can_dispatch(self) -> bool:
        return True

    def unavailable_error(self):
        from asterun.errors import BACKEND_UNAVAILABLE, AsterunError

        return AsterunError(BACKEND_UNAVAILABLE, "stream backend unavailable")

    def dispatch_stream(self, task_id, run_id, script, text, *, cwd, native, publish, stopping, **kwargs):
        self.dispatch_count += 1
        self.calls.append(run_id.value)
        return self.scripts[script]


def _wait(predicate, message: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(message)


def _result(script: str) -> dict:
    payload = {
        "status": "succeeded",
        "terminated": True,
        "summary": KEPT_SUMMARY,
        "native": {"backend_session_id": KEPT_SESSION, "turn_id": "turn-kept"},
        "events": [{"type": "message", "text": KEPT_MARKER}],
    }
    if script == "needs_approval":
        payload["needs_approval"] = True
    return payload


def _worker_app(isolated_env: Path, workspace_root: Path, store: SqliteStore, backend: StreamBackend, *, concurrency: int):
    path = write_config(
        isolated_env / "persist-config.json",
        workspace_root,
        extra_backends={"worker": {"kind": "claude", "enabled": True}},
        extra={"scheduler": {"max_queue": 4, "per_backend_concurrency": concurrency}},
    )
    config = load_config(path)
    backend.config = config.backends["worker"]
    app = Application(
        config,
        store=store,
        backends={"worker": backend, "fake": FakeBackend(config.backends["fake"])},
        policy=AllowConfiguredWorkspaces({"demo"}),
        principal=Principal("test:user", "test"),
        background=True,
    )
    return app


def _persisted(path: Path, run_id: str):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        run = json.loads(conn.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()["payload"])
        operation = conn.execute("SELECT status FROM operations WHERE run_id=?", (run_id,)).fetchone()
        events = [json.loads(row["payload"]) for row in conn.execute(
            "SELECT payload FROM events WHERE run_id=? ORDER BY seq", (run_id,))]
        approvals = [json.loads(row["payload"]) for row in conn.execute("SELECT payload FROM approvals")]
        sessions = [json.loads(row["payload"]) for row in conn.execute("SELECT payload FROM sessions")]
        version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    finally:
        conn.close()
    return run, None if operation is None else operation["status"], events, approvals, sessions, version


def _texts(events: list[dict]) -> list[str]:
    return [str(item.get("payload", {}).get("text") or "") for item in events]


def _join(app: Application) -> None:
    if app.executor is None:
        return
    for worker in list(app.executor.workers.values()):
        worker.join(timeout=2)


def test_persist_failure_releases_slot_and_keeps_result(isolated_env, workspace_root):
    """落盘失败不得把运行留在派发中，也不得丢掉已收到的结果或挡住同轮后续任务。"""
    db = isolated_env / "persist.sqlite"
    store = SqliteStore(db)
    backend = StreamBackend(
        None,
        {"first": _result("first"), "second": {**_result("second"), "summary": "second finished"}},
    )
    app = _worker_app(isolated_env, workspace_root, store, backend, concurrency=1)
    try:
        first = app.task_submit({"workspace": "demo", "backend": "worker", "text": "first", "script": "first"})
        assert first.ok and first.data["run"]["status"] == RunStatus.DISPATCHING
        _wait(lambda: app.executor.updates.qsize() >= 1, "第一个结果没有进入执行器队列")
        second = app.task_submit({"workspace": "demo", "backend": "worker", "text": "second", "script": "second"})
        assert second.data["queued"] is True and second.data["dispatched"] is False
        assert backend.dispatch_count == 1

        observed: dict = {}
        original = store.save_operation
        armed = True

        def fail_completed(operation):
            nonlocal armed
            if (
                armed
                and operation.status == OperationStatus.COMPLETED
                and operation.run_id is not None
                and operation.run_id.value == first.ids["run_id"]
            ):
                row = store._conn.execute("SELECT payload FROM runs WHERE id=?", (first.ids["run_id"],)).fetchone()
                observed["in_transaction"] = bool(store._conn.in_transaction)
                observed["visible_status"] = json.loads(row["payload"])["status"]
                raise sqlite3.OperationalError("injected result persist failure")
            return original(operation)

        store.save_operation = fail_completed
        with pytest.raises(sqlite3.OperationalError, match="injected result persist failure"):
            app.poll()
        _join(app)

        assert observed["in_transaction"] is True
        assert observed["visible_status"] == RunStatus.SUCCEEDED
        assert first.ids["run_id"] not in app.scheduler.inflight_runs
        assert second.ids["run_id"] not in app.scheduler.inflight_runs
        assert any(item.run_id.value == second.ids["run_id"] for item in app.scheduler.queue)
        assert backend.dispatch_count == 1
        assert backend.calls == [first.ids["run_id"]]

        run, operation_status, events, _approvals, sessions, version = _persisted(db, first.ids["run_id"])
        assert version == "6"
        assert run["status"] == RunStatus.PENDING_RECONCILE
        assert run["error_code"] == REMOTE_STATE_UNKNOWN
        assert operation_status == OperationStatus.UNKNOWN
        assert run["native"][UNAPPLIED_RESULT_NATIVE_KEY]["summary"] == KEPT_SUMMARY
        assert run["native"][UNAPPLIED_RESULT_NATIVE_KEY]["native"]["backend_session_id"] == KEPT_SESSION
        assert KEPT_MARKER not in _texts(events)
        matched = [item for item in sessions if item.get("task_id") == first.ids["task_id"]]
        assert matched and matched[0]["backend_session_id"] in (None, "")
        public = app._task_view(app.store.get_task(app.store.get_run(RunId(first.ids["run_id"])).task_id), app.store.get_run(RunId(first.ids["run_id"])))
        assert UNAPPLIED_RESULT_NATIVE_KEY not in public["run"]["native"]

        armed = False

        def both_finished() -> bool:
            app.poll()
            _join(app)
            followed = app.store.get_run(RunId(second.ids["run_id"]))
            return followed.status == RunStatus.SUCCEEDED

        _wait(both_finished, "保留的结果或后续任务没有完成")
        finished = app.store.get_run(RunId(first.ids["run_id"]))
        assert finished.status == RunStatus.SUCCEEDED
        assert finished.summary == KEPT_SUMMARY
        assert UNAPPLIED_RESULT_NATIVE_KEY not in finished.native
        assert finished.error_code in (None, "")
        session = app.store.get_session(app.store.get_task(finished.task_id).conversation_id)
        assert session.backend_session_id.value == KEPT_SESSION
        assert KEPT_MARKER in _texts([item.to_dict() for item in app.store.list_events(finished.id)])
        assert backend.dispatch_count == 2
        assert app.scheduler.inflight_for("worker") == 0
        followed = app.store.get_run(RunId(second.ids["run_id"]))
        assert followed.status == RunStatus.SUCCEEDED
        assert followed.summary == "second finished"
    finally:
        _join(app)
        app.close()


def test_result_writes_commit_or_roll_back_together(isolated_env, workspace_root):
    """运行、会话、审批、事件和操作要么一起提交，要么一起回到落盘前，不能留下终态运行配未完成操作。"""
    db = isolated_env / "atomic.sqlite"
    store = SqliteStore(db)
    backend = StreamBackend(None, {"needs_approval": _result("needs_approval")})
    app = _worker_app(isolated_env, workspace_root, store, backend, concurrency=1)
    try:
        submitted = app.task_submit({
            "workspace": "demo", "backend": "worker", "text": "approve", "script": "needs_approval",
        })
        assert submitted.ok
        run_id = submitted.ids["run_id"]
        _wait(lambda: app.executor.updates.qsize() >= 1, "结果没有进入执行器队列")
        observed: dict = {}
        original = store.save_operation

        def crash_before_operation(operation):
            if operation.status == OperationStatus.COMPLETED and operation.run_id is not None and operation.run_id.value == run_id:
                row = store._conn.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()
                observed["in_transaction"] = bool(store._conn.in_transaction)
                observed["visible_status"] = json.loads(row["payload"])["status"]
                approval = store._conn.execute(
                    "SELECT COUNT(*) AS n FROM approvals WHERE json_extract(payload, '$.run_id')=?",
                    (run_id,),
                ).fetchone()["n"]
                observed["visible_approvals"] = int(approval)
                raise sqlite3.OperationalError("injected crash before operation update")
            return original(operation)

        store.save_operation = crash_before_operation
        with pytest.raises(sqlite3.OperationalError, match="injected crash before operation update"):
            app.poll()
        _join(app)

        assert observed["in_transaction"] is True
        assert observed["visible_status"] == RunStatus.SUCCEEDED
        assert observed["visible_approvals"] == 1
        run, operation_status, events, approvals, sessions, version = _persisted(db, run_id)
        assert version == "6"
        assert run["status"] == RunStatus.PENDING_RECONCILE
        assert operation_status == OperationStatus.UNKNOWN
        assert run["status"] != RunStatus.SUCCEEDED
        assert not any(item["run_id"] == run_id and item["state"] == "pending" for item in approvals)
        assert KEPT_MARKER not in _texts(events)
        task_sessions = [item for item in sessions if item.get("task_id") == submitted.ids["task_id"]]
        assert task_sessions and task_sessions[0]["backend_session_id"] in (None, "")
        assert run["native"][UNAPPLIED_RESULT_NATIVE_KEY]["needs_approval"] is True
        assert backend.dispatch_count == 1

        app.close()
        reopened_backend = StreamBackend(None, {"needs_approval": _result("needs_approval")})
        reopened = _worker_app(isolated_env, workspace_root, SqliteStore(db), reopened_backend, concurrency=1)
        try:
            restored = reopened.store.get_run(RunId(run_id))
            assert restored.status == RunStatus.SUCCEEDED
            assert restored.summary == KEPT_SUMMARY
            assert UNAPPLIED_RESULT_NATIVE_KEY not in restored.native
            assert reopened.store.find_operation_for_run(restored.id).status == OperationStatus.COMPLETED
            assert reopened.store.find_pending_approval(restored.id) is not None
            assert KEPT_MARKER in _texts([item.to_dict() for item in reopened.store.list_events(restored.id)])
            session = reopened.store.get_session(reopened.store.get_task(restored.task_id).conversation_id)
            assert session.backend_session_id.value == KEPT_SESSION
            assert reopened_backend.dispatch_count == 0
            assert run_id not in reopened.scheduler.inflight_runs
        finally:
            reopened.close()
    finally:
        if not app._closed:
            _join(app)
            app.close()


def test_restart_without_operation_releases_slot_and_does_not_redispatch(isolated_env, workspace_root):
    """重启时查不到操作记录的运行进入待对账并释放名额，不重新执行。"""
    db = isolated_env / "missing-operation.sqlite"
    config_path = write_config(
        isolated_env / "missing-config.json",
        workspace_root,
        extra={"scheduler": {"max_queue": 4, "per_backend_concurrency": 1}},
    )
    config = load_config(config_path)
    fake = FakeBackend(config.backends["fake"])
    app = Application(
        config,
        store=SqliteStore(db),
        backends={"fake": fake},
        policy=AllowConfiguredWorkspaces({"demo"}),
        principal=Principal("test:user", "test"),
    )
    try:
        held = app.handle("task.submit", {"workspace": "demo", "text": "hold", "script": "cancel_accept_only"})
        assert held.ok and held.data["run"]["status"] == RunStatus.RUNNING
        assert app.scheduler.inflight_for("fake") == 1
        run_id = held.ids["run_id"]
        app.store._conn.execute("DELETE FROM operations WHERE run_id=?", (run_id,))
        assert app.store.find_operation_for_run(RunId(run_id)) is None
        assert app.store.get_run(RunId(run_id)).status == RunStatus.RUNNING
    finally:
        app.close()

    restored_backend = FakeBackend(load_config(config_path).backends["fake"])
    restored = Application(
        load_config(config_path),
        store=SqliteStore(db),
        backends={"fake": restored_backend},
        policy=AllowConfiguredWorkspaces({"demo"}),
        principal=Principal("test:user", "test"),
    )
    try:
        run = restored.store.get_run(RunId(run_id))
        assert run.status == RunStatus.PENDING_RECONCILE
        assert run.error_code == REMOTE_STATE_UNKNOWN
        assert run.terminated is False
        assert restored.scheduler.inflight_for("fake") == 0
        assert run_id not in restored.scheduler.inflight_runs
        assert restored_backend.dispatch_count == 0
        reconciled = restored.handle("task.reconcile", {"task_id": held.ids["task_id"]})
        assert reconciled.ok
        assert reconciled.data["re_dispatched"] is False
        report = reconciled.data["reports"][0]
        assert report["unknown"] is True
        assert report["re_dispatched"] is False
        assert report["run_status"] == RunStatus.PENDING_RECONCILE
        assert report["terminal"] is False
        assert restored_backend.dispatch_count == 0
        reasons = [
            item.payload.get("reason")
            for item in restored.store.list_events(RunId(run_id))
            if item.type.value == "reconciled"
        ]
        assert reasons.count("operation_missing") == 1

        nxt = restored.handle("task.submit", {"workspace": "demo", "text": "next", "script": "success"})
        assert nxt.ok and nxt.data["dispatched"] is True and nxt.data["queued"] is False
        assert restored_backend.dispatch_count == 1
        assert restored_backend.calls[0].payload["run_id"] == nxt.ids["run_id"]
        assert restored.store.get_run(RunId(run_id)).status == RunStatus.PENDING_RECONCILE
        assert restored.scheduler.inflight_for("fake") == 0
    finally:
        restored.close()

    again = Application(
        load_config(config_path),
        store=SqliteStore(db),
        backends={"fake": FakeBackend(load_config(config_path).backends["fake"])},
        policy=AllowConfiguredWorkspaces({"demo"}),
        principal=Principal("test:user", "test"),
    )
    try:
        assert again.store.get_run(RunId(run_id)).status == RunStatus.PENDING_RECONCILE
        assert again.scheduler.inflight_for("fake") == 0
        reasons = [
            item.payload.get("reason")
            for item in again.store.list_events(RunId(run_id))
            if item.type.value == "reconciled"
        ]
        assert reasons.count("operation_missing") == 1
        assert again.backends["fake"].dispatch_count == 0
    finally:
        again.close()


class ExplodingBackend(StreamBackend):
    """先交出带原生引用的进度，再抛错，迫使工作线程补发通用待对账。"""

    def dispatch_stream(self, task_id, run_id, script, text, *, cwd, native, publish, stopping, **kwargs):
        self.dispatch_count += 1
        self.calls.append(run_id.value)
        publish({
            "status": "dispatching",
            "terminated": False,
            "summary": "thread bound",
            "native": {"backend_session_id": KEPT_SESSION, "thread_id": KEPT_SESSION},
        })
        raise RuntimeError("stopped after bound thread")


def test_worker_fallback_does_not_drop_unapplied_native_reference(isolated_env, workspace_root):
    """动作因落盘失败中断后，通用待对账不能盖掉尚未写上的原生引用，也不能再次派发。"""
    db = isolated_env / "fallback.sqlite"
    store = SqliteStore(db)
    backend = ExplodingBackend(None, {})
    app = _worker_app(isolated_env, workspace_root, store, backend, concurrency=1)
    try:
        submitted = app.task_submit({"workspace": "demo", "backend": "worker", "text": "bind", "script": "bind"})
        assert submitted.ok
        run_id = submitted.ids["run_id"]
        _wait(lambda: app.executor.updates.qsize() >= 1, "进度没有进入执行器队列")
        original = store.save_session
        armed = True

        def fail_session(session):
            if armed and getattr(session.backend_session_id, "value", None) == KEPT_SESSION:
                raise sqlite3.OperationalError("injected session persist failure")
            return original(session)

        store.save_session = fail_session
        with pytest.raises(sqlite3.OperationalError, match="injected session persist failure"):
            app.poll()

        def worker_finished() -> bool:
            app.poll()
            return not any(worker.is_alive() for worker in app.executor.workers.values())

        _wait(worker_finished, "工作线程没有在通用待对账后结束")
        run, operation_status, _events, _approvals, sessions, version = _persisted(db, run_id)
        assert version == "6"
        assert run["status"] == RunStatus.PENDING_RECONCILE
        assert operation_status == OperationStatus.UNKNOWN
        assert run["native"][UNAPPLIED_RESULT_NATIVE_KEY]["native"]["backend_session_id"] == KEPT_SESSION
        assert sessions and sessions[0]["backend_session_id"] in (None, "")
        assert run_id not in app.scheduler.inflight_runs
        assert backend.dispatch_count == 1
        assert backend.calls == [run_id]

        armed = False
        reconciled = app.handle("task.reconcile", {"task_id": submitted.ids["task_id"]})
        assert reconciled.ok and reconciled.data["re_dispatched"] is False
        restored = app.store.get_run(RunId(run_id))
        assert restored.status == RunStatus.PENDING_RECONCILE
        assert restored.error_code == REMOTE_STATE_UNKNOWN
        assert UNAPPLIED_RESULT_NATIVE_KEY not in restored.native
        assert restored.native["backend_session_id"] == KEPT_SESSION
        session = app.store.get_session(app.store.get_task(restored.task_id).conversation_id)
        assert session.backend_session_id.value == KEPT_SESSION
        assert backend.dispatch_count == 1
        assert run_id not in app.scheduler.inflight_runs
    finally:
        _join(app)
        app.close()


class ProgressBackend(StreamBackend):
    """返回未结束进度，重放后仍应停在待对账，不能借重启把名额占回去。"""

    def dispatch_stream(self, task_id, run_id, script, text, *, cwd, native, publish, stopping, **kwargs):
        self.dispatch_count += 1
        self.calls.append(run_id.value)
        return {
            "status": "running",
            "terminated": False,
            "summary": "still going",
            "native": {"backend_session_id": KEPT_SESSION, "thread_id": KEPT_SESSION},
        }


def _sync_app(isolated_env: Path, workspace_root: Path, store: SqliteStore) -> Application:
    path = write_config(isolated_env / "sync-config.json", workspace_root)
    config = load_config(path)
    return Application(
        config,
        store=store,
        backends={"fake": FakeBackend(config.backends["fake"])},
        policy=AllowConfiguredWorkspaces({"demo"}),
        principal=Principal("test:user", "test"),
    )


def test_failed_retry_remains_retryable_without_losing_result(isolated_env, workspace_root):
    """重试再次失败也要留在内存里，写不进状态库时不能把结果丢掉，之后仍会落盘。"""
    db = isolated_env / "retry.sqlite"
    store = SqliteStore(db)
    backend = StreamBackend(None, {"first": _result("first")})
    app = _worker_app(isolated_env, workspace_root, store, backend, concurrency=1)
    try:
        submitted = app.task_submit({"workspace": "demo", "backend": "worker", "text": "first", "script": "first"})
        assert submitted.ok
        run_id = submitted.ids["run_id"]
        _wait(lambda: app.executor.updates.qsize() >= 1, "结果没有进入执行器队列")
        original_operation = store.save_operation
        original_run = store.save_run
        operation_failures = {"n": 0}
        run_failures = {"n": 0}
        armed = True

        def fail_completed(operation):
            if (
                armed
                and operation.status == OperationStatus.COMPLETED
                and operation.run_id is not None
                and operation.run_id.value == run_id
                and operation_failures["n"] < 2
            ):
                operation_failures["n"] += 1
                raise sqlite3.OperationalError("injected retry persist failure")
            return original_operation(operation)

        def fail_preserve(run):
            if armed and run.id.value == run_id and run.status == RunStatus.PENDING_RECONCILE and run_failures["n"] < 2:
                run_failures["n"] += 1
                raise sqlite3.OperationalError("injected stash persist failure")
            return original_run(run)

        store.save_operation = fail_completed
        store.save_run = fail_preserve
        with pytest.raises(sqlite3.OperationalError, match="injected retry persist failure"):
            app.poll()
        app.poll()
        retained = app._retained_results.get(run_id)
        assert retained is not None
        payload = retained.result if hasattr(retained, "result") else retained
        assert payload["summary"] == KEPT_SUMMARY
        run, operation_status, events, _approvals, sessions, _version = _persisted(db, run_id)
        assert run["status"] != RunStatus.SUCCEEDED
        assert KEPT_MARKER not in _texts(events)
        assert UNAPPLIED_RESULT_NATIVE_KEY not in run["native"]
        assert operation_failures["n"] == 2
        skipped = app.poll()
        assert skipped is None
        assert operation_failures["n"] == 2
        assert run_id in app._retained_results
        armed = False

        def finished() -> bool:
            app.poll()
            _join(app)
            return app.store.get_run(RunId(run_id)).status == RunStatus.SUCCEEDED

        _wait(finished, "退避结束后没有把保留的结果写完")
        done = app.store.get_run(RunId(run_id))
        assert done.summary == KEPT_SUMMARY
        assert UNAPPLIED_RESULT_NATIVE_KEY not in done.native
        assert KEPT_MARKER in _texts([item.to_dict() for item in app.store.list_events(done.id)])
        session = app.store.get_session(app.store.get_task(done.task_id).conversation_id)
        assert session.backend_session_id.value == KEPT_SESSION
        assert backend.dispatch_count == 1
        assert app.scheduler.inflight_for("worker") == 0
    finally:
        _join(app)
        app.close()


def test_restart_does_not_reoccupy_released_pending_reconcile(isolated_env, workspace_root):
    """已经释放名额的待对账运行，重启后不能再被 scheduler.start 占回去。"""
    db = isolated_env / "released-slot.sqlite"
    store = SqliteStore(db)
    backend = ProgressBackend(None, {})
    app = _worker_app(isolated_env, workspace_root, store, backend, concurrency=1)
    try:
        submitted = app.task_submit({"workspace": "demo", "backend": "worker", "text": "hold", "script": "hold"})
        assert submitted.ok
        run_id = submitted.ids["run_id"]
        _wait(lambda: app.executor.updates.qsize() >= 1, "进度没有进入执行器队列")
        original = store.save_session

        def fail_session(session):
            if getattr(session.backend_session_id, "value", None) == KEPT_SESSION:
                raise sqlite3.OperationalError("injected progress persist failure")
            return original(session)

        store.save_session = fail_session
        with pytest.raises(sqlite3.OperationalError, match="injected progress persist failure"):
            app.poll()
        _join(app)
        assert run_id not in app.scheduler.inflight_runs
        assert app.store.get_run(RunId(run_id)).status == RunStatus.PENDING_RECONCILE
    finally:
        _join(app)
        app.close()

    restored_backend = ProgressBackend(None, {})
    restored = _worker_app(isolated_env, workspace_root, SqliteStore(db), restored_backend, concurrency=1)
    try:
        run = restored.store.get_run(RunId(run_id))
        assert run.status == RunStatus.PENDING_RECONCILE
        assert run.error_code == REMOTE_STATE_UNKNOWN
        assert restored.scheduler.inflight_for("worker") == 0
        assert run_id not in restored.scheduler.inflight_runs
        assert restored_backend.dispatch_count == 0
        public = restored.handle("task.get", {"task_id": submitted.ids["task_id"]})
        assert public.ok
        assert "concurrency_released" not in public.data["run"]["native"]
        assert UNAPPLIED_RESULT_NATIVE_KEY not in public.data["run"]["native"]
        nxt = restored.handle("task.submit", {
            "workspace": "demo", "backend": "worker", "text": "next", "script": "next",
        })
        # 后台执行器受理后立即返回，响应里有 dispatched，没有同步路径的 queued。
        assert nxt.ok and nxt.data["dispatched"] is True and nxt.data.get("queued") is not True
        assert nxt.ids["run_id"] in restored.scheduler.inflight_runs
        assert run_id not in restored.scheduler.inflight_runs
        _wait(lambda: restored_backend.dispatch_count == 1, "释放名额后的下一个任务没有派发")
        assert restored.store.get_run(RunId(run_id)).status == RunStatus.PENDING_RECONCILE
    finally:
        restored.close()


def test_sync_dispatch_result_writes_are_one_transaction(isolated_env, workspace_root):
    """前台派发在后端返回后的运行、会话、审批和操作写入要么一起提交，要么一起回滚。"""
    db = isolated_env / "sync-dispatch.sqlite"
    store = SqliteStore(db)
    app = _sync_app(isolated_env, workspace_root, store)
    try:
        original = store.save_operation
        observed: dict = {}

        def crash(operation):
            if operation.status == OperationStatus.COMPLETED and operation.run_id is not None:
                row = store._conn.execute("SELECT payload FROM runs WHERE id=?", (operation.run_id.value,)).fetchone()
                observed["in_transaction"] = bool(store._conn.in_transaction)
                observed["visible_status"] = json.loads(row["payload"])["status"]
                observed["run_id"] = operation.run_id.value
                raise sqlite3.OperationalError("injected sync dispatch persist failure")
            return original(operation)

        store.save_operation = crash
        with pytest.raises(sqlite3.OperationalError, match="injected sync dispatch persist failure"):
            app.handle("task.submit", {"workspace": "demo", "text": "done", "script": "success"})
        assert observed["in_transaction"] is True
        assert observed["visible_status"] == RunStatus.SUCCEEDED
        run, operation_status, events, _approvals, _sessions, version = _persisted(db, observed["run_id"])
        assert version == "6"
        assert not (run["status"] == RunStatus.SUCCEEDED and operation_status == OperationStatus.DISPATCHED)
        assert run["status"] != RunStatus.SUCCEEDED
        texts = _texts(events)
        assert "done" not in texts
        assert "fake 开始模拟" not in texts
        assert app.backends["fake"].dispatch_count == 1
        store.save_operation = original
        app.poll()

        def finished() -> bool:
            app.poll()
            return app.store.get_run(RunId(observed["run_id"])).status == RunStatus.SUCCEEDED

        _wait(finished, "同步派发回滚后的结果没有重新落盘")
        done, operation_status, events, _approvals, _sessions, _version = _persisted(db, observed["run_id"])
        assert done["summary"] == "done"
        assert operation_status == OperationStatus.COMPLETED
        texts = _texts(events)
        assert "done" in texts
        assert "fake 开始模拟" in texts
        assert app.backends["fake"].dispatch_count == 1
    finally:
        app.close()


def test_cancel_result_writes_are_one_transaction(isolated_env, workspace_root):
    """取消确认后的运行和操作更新在同一个事务里，失败不能留下已取消运行配着仍在派发的操作。"""
    db = isolated_env / "sync-cancel.sqlite"
    store = SqliteStore(db)
    app = _sync_app(isolated_env, workspace_root, store)
    try:
        held = app.handle("task.submit", {"workspace": "demo", "text": "hold", "script": "cancel_and_stop"})
        assert held.ok and held.data["run"]["status"] == RunStatus.RUNNING
        run_id = held.ids["run_id"]
        original = store.save_operation
        observed: dict = {}

        def crash(operation):
            if operation.status == OperationStatus.COMPLETED and operation.run_id is not None and operation.run_id.value == run_id:
                row = store._conn.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()
                observed["in_transaction"] = bool(store._conn.in_transaction)
                observed["visible_status"] = json.loads(row["payload"])["status"]
                raise sqlite3.OperationalError("injected cancel persist failure")
            return original(operation)

        store.save_operation = crash
        with pytest.raises(sqlite3.OperationalError, match="injected cancel persist failure"):
            app.handle("task.cancel", {"task_id": held.ids["task_id"]})
        assert observed["in_transaction"] is True
        assert observed["visible_status"] == RunStatus.CANCELLED
        run, operation_status, events, _approvals, _sessions, _version = _persisted(db, run_id)
        assert not (run["status"] == RunStatus.CANCELLED and operation_status == OperationStatus.DISPATCHED)
        assert run["status"] != RunStatus.CANCELLED
        assert run["cancel_requested"] is True
        assert "fake 已停止运行" not in _texts(events)
        assert sum(1 for call in app.backends["fake"].calls if call.method == "request_cancel") == 1
        store.save_operation = original

        def finished() -> bool:
            app.poll()
            current = app.store.get_run(RunId(run_id))
            return current.status == RunStatus.CANCELLED

        _wait(finished, "取消结果没有在事务失败后重新落盘")
        done = app.store.get_run(RunId(run_id))
        assert done.cancel_requested is True
        assert app.store.find_operation_for_run(done.id).status == OperationStatus.COMPLETED
        assert sum(1 for call in app.backends["fake"].calls if call.method == "request_cancel") == 1
    finally:
        app.close()


def test_approval_result_writes_are_one_transaction(isolated_env, workspace_root):
    """审批回复后的审批、运行和操作写入在同一个事务里，不能出现已通过审批配着仍在等待的运行。"""
    db = isolated_env / "sync-approval.sqlite"
    store = SqliteStore(db)
    app = _sync_app(isolated_env, workspace_root, store)
    try:
        waiting = app.handle("task.submit", {"workspace": "demo", "text": "ask", "script": "wait_input"})
        assert waiting.ok and waiting.data["run"]["status"] == RunStatus.WAITING_INPUT
        approval_id = waiting.data["dispatch"]["approval_id"]
        run_id = waiting.ids["run_id"]
        original = store.save_operation
        observed: dict = {}

        def crash(operation):
            if operation.status == OperationStatus.COMPLETED and operation.run_id is not None and operation.run_id.value == run_id:
                run_row = store._conn.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()
                approval_row = store._conn.execute(
                    "SELECT payload FROM approvals WHERE id=?",
                    (approval_id,),
                ).fetchone()
                observed["in_transaction"] = bool(store._conn.in_transaction)
                observed["visible_status"] = json.loads(run_row["payload"])["status"]
                observed["visible_approval"] = json.loads(approval_row["payload"])["state"]
                raise sqlite3.OperationalError("injected approval persist failure")
            return original(operation)

        store.save_operation = crash
        with pytest.raises(sqlite3.OperationalError, match="injected approval persist failure"):
            app.handle("approval.respond", {"approval_id": approval_id, "decision": "approve"})
        assert observed["in_transaction"] is True
        assert observed["visible_status"] == RunStatus.SUCCEEDED
        assert observed["visible_approval"] == ApprovalState.APPROVED
        run, operation_status, _events, approvals, _sessions, _version = _persisted(db, run_id)
        approval = next(item for item in approvals if item["id"] == approval_id)
        assert not (run["status"] == RunStatus.SUCCEEDED and approval["state"] != ApprovalState.APPROVED)
        assert not (approval["state"] == ApprovalState.APPROVED and run["status"] != RunStatus.SUCCEEDED)
        assert run["status"] != RunStatus.SUCCEEDED
        assert approval["state"] != ApprovalState.APPROVED
        assert sum(1 for call in app.backends["fake"].calls if call.method == "respond_approval") == 1
        store.save_operation = original

        def finished() -> bool:
            app.poll()
            current = app.store.get_run(RunId(run_id))
            stored = app.store.get_approval(ApprovalId(approval_id))
            return current.status == RunStatus.SUCCEEDED and stored.state == ApprovalState.APPROVED

        _wait(finished, "审批结果没有在事务失败后重新落盘")
        assert app.store.find_operation_for_run(RunId(run_id)).status == OperationStatus.COMPLETED
        assert sum(1 for call in app.backends["fake"].calls if call.method == "respond_approval") == 1
    finally:
        app.close()
