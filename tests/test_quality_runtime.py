import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from asterun.application import Application
from asterun.backends.base import BackendCall
from asterun.backends.fake import FakeBackend
from asterun.config import load_config
from asterun.contracts import RunStatus
from asterun.ids import RunId, TaskId
from asterun.policy import Grant
from asterun.quality import parse_review, snapshot
from asterun.errors import AsterunError
from tests.conftest import write_config


class ReviewingFake(FakeBackend):
    def __init__(self, config, *, mode="pass"):
        super().__init__(config)
        self.mode = mode
        self.contexts = []

    def dispatch_review(self, task_id, run_id, script, text, *, cwd=None):
        self.calls.append(BackendCall("dispatch_review", {"run_id": run_id.value}))
        context = json.loads(text.split("\n", 1)[1])
        self.contexts.append(context)
        findings = []
        if self.mode == "finding" or self.mode == "repair" and len(self.contexts) == 1:
            findings = [{"summary": "answer 应返回正确结果", "path": "note.txt", "line": 1}]
        if self.mode == "drift":
            (cwd / "note.txt").write_text("审查期间出现外部变更")
        status = "pending_reconcile" if self.mode == "unknown" else "succeeded"
        summary = json.dumps({"target_hash": context["target"]["hash"], "findings": findings})
        if self.mode == "invalid":
            summary = "我已通过全部检查"
        return {"status": status, "terminated": status == "succeeded", "summary": summary}


class RepairingFake(FakeBackend):
    def dispatch(self, task_id, run_id, script, text, *, cwd=None):
        result = super().dispatch(task_id, run_id, script, text, cwd=cwd)
        if text.startswith("修复以下"):
            (cwd / "note.txt").write_text("answer = 42\n")
            result["summary"] = "已修复文件"
        return result


def make_app(config_path, workspace_root, *, mode="pass", store=None, max_runs=4, max_repairs=2,
             substitute=None, reviewer_enabled=True, background=False):
    path = write_config(config_path, workspace_root, extra_backends={
        "reviewer": {"kind": "fake", "enabled": reviewer_enabled},
        "replacement": {"kind": "fake", "enabled": True}}, extra={
        "workflow": {"require_review": True, "review_backend": "reviewer",
                     "approved_substitute": substitute, "max_repairs": max_repairs},
        "scheduler": {"max_runs_per_task": max_runs}})
    config = load_config(path)
    reviewer = ReviewingFake(config.backends["reviewer"], mode=mode)
    replacement = ReviewingFake(config.backends["replacement"])
    implementer = RepairingFake(config.backends["fake"])
    return Application(config, store=store, backends={"fake": implementer, "reviewer": reviewer,
                       "replacement": replacement}, background=background), reviewer, implementer


def start(app, *, checks=None, auto_repair=True):
    submit = app.handle("task.submit", {"workspace": "demo", "text": "修正 answer", "script": "success"})
    assert submit.ok, submit.error
    payload = {"task_id": submit.ids["task_id"], "idempotency_key": "quality-1",
               "target_paths": ["note.txt"], "checks": checks or [{"kind": "file_exists", "path": "note.txt"}],
               "auto_repair": auto_repair}
    result = app.handle("workflow.start", payload)
    assert result.ok, result.error
    return submit, payload


def settle(app, task_id):
    for _ in range(20):
        app.poll()
        data = app.task_get(task_id).data
        if data["task"]["paused"] or data["task"]["quality"]["phase"] == "completed":
            return data
    pytest.fail("质量流程没有在有界步骤内停止")


def test_independent_review_records_actual_run_and_scoped_version(config_path, workspace_root):
    app, reviewer, _ = make_app(config_path, workspace_root)
    submit, payload = start(app)
    data = settle(app, submit.ids["task_id"])
    task, run = data["task"], data["run"]
    assert task["acceptance"] == "passed" and task["run_count"] == 2
    assert run["role"] == "review" and run["backend"] == "reviewer"
    assert run["conversation_id"] != task["conversation_id"]
    evidence = task["quality"]["reviews"][0]
    assert evidence["implementation_run_id"] == submit.ids["run_id"]
    assert evidence["target_hash"] == snapshot(workspace_root, ["note.txt"])[0]["hash"]
    assert evidence["simulated"] is True and evidence["permission"] == "snapshot_only_no_tools"
    assert len(reviewer.contexts) == 1
    assert app.handle("workflow.start", payload).data["task"]["run_count"] == 2
    changed = {**payload, "auto_repair": False}
    assert app.handle("workflow.start", changed).error["code"] == "IDEMPOTENCY_CONFLICT"
    assert not app.handle("workflow.evaluate", {"task_id": task["id"], "checks": []}).ok


def test_findings_drive_repair_and_are_closed_by_new_review(config_path, workspace_root):
    app, reviewer, implementer = make_app(config_path, workspace_root, mode="repair")
    submit, _ = start(app)
    data = settle(app, submit.ids["task_id"])
    task = data["task"]
    assert task["acceptance"] == "passed" and task["repair_count"] == 1 and task["run_count"] == 4
    calls = [item for item in implementer.calls if item.method == "dispatch"]
    assert "answer 应返回正确结果" in calls[-1].payload["text"]
    assert calls[-1].payload["text"] != submit.data["task"]["text"]
    assert (workspace_root / "note.txt").read_text() == "answer = 42\n"
    first, second = task["quality"]["reviews"]
    assert first["target_hash"] != second["target_hash"]
    closure = task["quality"]["closures"][0]
    assert closure["repair_run_id"] == second["implementation_run_id"]
    assert closure["review_run_id"] == second["run_id"]
    assert reviewer.contexts[1]["previous_findings"]


def test_failed_file_check_repairs_before_review(config_path, workspace_root):
    app, reviewer, _ = make_app(config_path, workspace_root)
    submit, _ = start(app, checks=[{"kind": "file_contains", "path": "note.txt", "text": "answer = 42"}])
    data = settle(app, submit.ids["task_id"])
    assert data["task"]["acceptance"] == "passed"
    assert data["task"]["run_count"] == 3 and len(reviewer.contexts) == 1
    assert data["task"]["quality"]["closures"][0]["finding"]["kind"] == "file_contains"


@pytest.mark.parametrize("mode,reason", [("drift", "INVALID_REQUEST"), ("invalid", "INVALID_REQUEST"),
                                        ("unknown", "REMOTE_STATE_UNKNOWN")])
def test_review_never_passes_or_retries_uncertain_evidence(config_path, workspace_root, mode, reason):
    app, reviewer, _ = make_app(config_path, workspace_root, mode=mode)
    submit, _ = start(app)
    data = settle(app, submit.ids["task_id"])
    assert data["task"]["paused"] and data["task"]["acceptance"] == "pending"
    assert data["task"]["quality"]["blocked_reason"]["code"] == reason
    for _ in range(3):
        app.poll()
    assert len(reviewer.contexts) == 1 and data["task"]["repair_count"] == 0


def test_invalid_review_allows_explicit_new_workflow_without_resetting_budget(config_path, workspace_root):
    app, reviewer, _ = make_app(config_path, workspace_root, mode="invalid")
    submit, payload = start(app)
    data = settle(app, submit.ids["task_id"])
    assert data["task"]["quality"]["phase"] == "blocked"
    reviewer.mode = "pass"
    app.handle("task.handoff", {"task_id": submit.ids["task_id"], "action": "resume"})
    assert len(reviewer.contexts) == 1
    assert app.handle("workflow.start", {**payload, "idempotency_key": "explicit-retry"}).ok
    data = settle(app, submit.ids["task_id"])
    assert data["task"]["acceptance"] == "passed"
    assert data["task"]["run_count"] == 3 and len(reviewer.contexts) == 2
    replay = app.handle("workflow.start", payload)
    assert replay.ok and replay.data["task"]["run_count"] == 3
    assert replay.data["task"]["quality"]["idempotency_key"] == "explicit-retry"
    assert len(reviewer.contexts) == 2
    assert app.handle("workflow.start", {**payload, "auto_repair": False}).error["code"] == "IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize("runs,repairs,expected_runs", [(2, 2, 2), (10, 0, 2), (3, 2, 3)])
def test_review_and_repair_share_budget(config_path, workspace_root, runs, repairs, expected_runs):
    app, _, _ = make_app(config_path, workspace_root, mode="finding", max_runs=runs, max_repairs=repairs)
    submit, _ = start(app)
    data = settle(app, submit.ids["task_id"])
    assert data["task"]["paused"] and data["task"]["run_count"] == expected_runs
    assert data["task"]["quality"]["blocked_reason"]["code"] == "BUDGET_EXHAUSTED"
    assert data["task"]["acceptance"] != "passed"


def test_unavailable_reviewer_only_uses_configured_executable_substitute(config_path, workspace_root):
    app, reviewer, _ = make_app(config_path, workspace_root, reviewer_enabled=False, substitute="replacement")
    submit, _ = start(app)
    data = settle(app, submit.ids["task_id"])
    assert not reviewer.contexts
    assert data["task"]["quality"]["reviews"][0]["used_substitute"] is True
    assert data["task"]["quality"]["reviews"][0]["backend"] == "replacement"
    app, reviewer, _ = make_app(config_path, workspace_root, reviewer_enabled=False, substitute="human-checklist")
    submit, _ = start(app)
    data = settle(app, submit.ids["task_id"])
    assert data["task"]["review_status"] == "unavailable"
    assert not data["task"]["quality"]["reviews"] and data["task"]["run_count"] == 1


def test_changed_file_invalidates_previously_passed_evidence(config_path, workspace_root):
    app, _, _ = make_app(config_path, workspace_root)
    submit, _ = start(app)
    assert settle(app, submit.ids["task_id"])["task"]["acceptance"] == "passed"
    (workspace_root / "note.txt").write_text("新版本")
    result = app.handle("workflow.evaluate", {"task_id": submit.ids["task_id"]})
    assert result.ok and result.data["acceptance"] == "pending"
    assert result.data["task"]["evidence_stale"] is True


def test_review_intent_failure_does_not_consume_budget(config_path, workspace_root):
    app, reviewer, _ = make_app(config_path, workspace_root)
    submit = app.handle("task.submit", {"workspace": "demo"})
    app.store.fail_next_intent = True
    result = app.handle("workflow.start", {"task_id": submit.ids["task_id"], "idempotency_key": "failure",
        "target_paths": ["note.txt"], "checks": [{"kind": "file_exists", "path": "note.txt"}]})
    assert result.ok and result.data["task"]["paused"]
    assert result.data["task"]["run_count"] == 1 and not reviewer.contexts
    assert result.data["run"]["id"] == submit.ids["run_id"]


def test_terminal_review_survives_restart_without_duplicate_dispatch(config_path, workspace_root, isolated_env):
    from asterun.sqlite_store import SqliteStore

    database = isolated_env / "state.sqlite"
    app, reviewer, _ = make_app(config_path, workspace_root, store=SqliteStore(database))
    submit, _ = start(app)
    assert len(reviewer.contexts) == 1
    app.close()
    app, reviewer, _ = make_app(config_path, workspace_root, store=SqliteStore(database))
    try:
        assert settle(app, submit.ids["task_id"])["task"]["acceptance"] == "passed"
        assert not reviewer.contexts
    finally:
        app.close()


def test_unknown_review_survives_restart_without_replay(config_path, workspace_root, isolated_env):
    from asterun.sqlite_store import SqliteStore

    database = isolated_env / "state.sqlite"
    app, _, _ = make_app(config_path, workspace_root, mode="unknown", store=SqliteStore(database))
    submit, _ = start(app)
    app.close()
    app, reviewer, _ = make_app(config_path, workspace_root, store=SqliteStore(database))
    try:
        data = settle(app, submit.ids["task_id"])
        assert data["run"]["status"] == "pending_reconcile"
        assert not reviewer.contexts and data["task"]["run_count"] == 2
    finally:
        app.close()


def test_expired_grant_stops_automatic_repair(config_path, workspace_root):
    app, _, implementer = make_app(config_path, workspace_root, mode="finding")
    submit, _ = start(app)
    app.grant = Grant(expires_at="2000-01-01T00:00:00Z")
    data = settle(app, submit.ids["task_id"])
    assert data["task"]["quality"]["blocked_reason"]["code"] == "AUTH_REQUIRED"
    assert implementer.dispatch_count == 1


def test_handoff_stops_quality_and_manual_repair_cannot_compete(config_path, workspace_root):
    app, _, implementer = make_app(config_path, workspace_root, mode="finding")
    submit, _ = start(app)
    handoff = app.handle("task.handoff", {"task_id": submit.ids["task_id"], "action": "pause"})
    assert handoff.ok
    for _ in range(4):
        app.poll()
    assert implementer.dispatch_count == 1
    assert not app.handle("workflow.repair", {"task_id": submit.ids["task_id"]}).ok
    assert app.handle("task.handoff", {"task_id": submit.ids["task_id"], "action": "resume"}).ok
    assert settle(app, submit.ids["task_id"])["task"]["repair_count"] >= 1


def test_cancel_stops_between_stages_without_rewriting_completed_run(config_path, workspace_root):
    app, reviewer, implementer = make_app(config_path, workspace_root, mode="finding")
    submit, _ = start(app)
    cancelled = app.handle("task.cancel", {"task_id": submit.ids["task_id"]})
    assert cancelled.ok and cancelled.data["workflow_stopped"]
    assert cancelled.data["run"]["status"] == "succeeded"
    for _ in range(4):
        app.poll()
    assert implementer.dispatch_count == 1 and len(reviewer.contexts) == 1


def test_queued_review_rechecks_target_and_cannot_replace_main_session(config_path, workspace_root):
    app, reviewer, implementer = make_app(config_path, workspace_root)
    submit = app.handle("task.submit", {"workspace": "demo", "text": "实现"})
    blocker = app.handle("task.submit", {"workspace": "demo", "script": "cancel_and_stop"})
    begun = app.handle("workflow.start", {"task_id": submit.ids["task_id"], "idempotency_key": "queued",
        "target_paths": ["note.txt"], "checks": [{"kind": "file_exists", "path": "note.txt"}]})
    assert begun.ok and begun.data["run"]["status"] == "queued"
    (workspace_root / "note.txt").write_text("排队期间变动")
    assert app.handle("task.cancel", {"task_id": blocker.ids["task_id"]}).ok
    data = settle(app, submit.ids["task_id"])
    assert data["task"]["acceptance"] == "pending" and not reviewer.contexts
    assert data["run"]["status"] == "failed"
    assert not any(app.scheduler.snapshot()["inflight"].values())


def test_pending_review_retains_distinct_backend_slot_and_is_cancellable(config_path, workspace_root):
    app, _, _ = make_app(config_path, workspace_root)
    submit = app.handle("task.submit", {"workspace": "demo", "text": "实现"})
    blocker = app.handle("task.submit", {"workspace": "demo", "script": "cancel_and_stop"})
    result = app.handle("workflow.start", {"task_id": submit.ids["task_id"], "idempotency_key": "queued",
        "target_paths": ["note.txt"], "checks": [{"kind": "file_exists", "path": "note.txt"}]})
    assert result.data["run"]["backend"] == "reviewer"
    assert app.handle("task.cancel", {"task_id": submit.ids["task_id"]}).ok
    assert app.handle("task.cancel", {"task_id": blocker.ids["task_id"]}).ok
    data = app.handle("task.get", {"task_id": submit.ids["task_id"]}).data
    assert data["task"]["quality"]["phase"] == "blocked"
    assert data["run"]["status"] == "cancelled"


def test_intended_review_resumes_after_restart_with_original_conversation(config_path, workspace_root, isolated_env):
    from asterun.sqlite_store import SqliteStore

    database = isolated_env / "state.sqlite"
    app, reviewer, _ = make_app(config_path, workspace_root, store=SqliteStore(database))
    submit = app.handle("task.submit", {"workspace": "demo"})
    blocker = app.handle("task.submit", {"workspace": "demo", "script": "cancel_and_stop"})
    begun = app.handle("workflow.start", {"task_id": submit.ids["task_id"], "idempotency_key": "restart",
        "target_paths": ["note.txt"], "checks": [{"kind": "file_exists", "path": "note.txt"}]})
    conversation = begun.data["run"]["conversation_id"]
    # 模拟事务落盘后、后端派发前停止；将无副作用的阻塞夹具记为已结束。
    blocker_run = app.store.get_run(RunId(blocker.ids["run_id"]))
    app._set_run(blocker_run, RunStatus.CANCELLED, terminated=True)
    app._finish_operation(blocker_run)
    assert not reviewer.contexts
    app.close()
    app, reviewer, _ = make_app(config_path, workspace_root, store=SqliteStore(database))
    try:
        data = settle(app, submit.ids["task_id"])
        assert data["task"]["acceptance"] == "passed" and len(reviewer.contexts) == 1
        assert data["run"]["conversation_id"] == conversation
        assert data["task"]["run_count"] == 2
    finally:
        app.close()


def test_completed_evidence_revalidates_git_head_on_query(config_path, workspace_root):
    def git(*args):
        return subprocess.run(["git", *args], cwd=workspace_root, check=True, capture_output=True)
    git("init")
    git("add", "note.txt")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "first")
    app, _, _ = make_app(config_path, workspace_root)
    submit, _ = start(app)
    assert settle(app, submit.ids["task_id"])["task"]["acceptance"] == "passed"
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "next")
    assert app.handle("task.get", {"task_id": submit.ids["task_id"]}).data["task"]["acceptance"] == "pending"


@pytest.mark.parametrize("paths", [[], ["../note.txt"], ["/tmp/note.txt"], ["./note.txt"], ["note.txt", "note.txt"], [".git/config"]])
def test_snapshot_rejects_invalid_scope(workspace_root, paths):
    with pytest.raises(AsterunError):
        snapshot(workspace_root, paths)


def test_snapshot_rejects_symlink_binary_state_and_oversize(workspace_root):
    (workspace_root / "link").symlink_to(workspace_root / "note.txt")
    (workspace_root / "binary").write_bytes(b"\x00")
    (workspace_root / "large").write_bytes(b"a" * (256 * 1024 + 1))
    for name in ("link", "binary", "large"):
        with pytest.raises(AsterunError):
            snapshot(workspace_root, [name])
    with pytest.raises(AsterunError):
        snapshot(workspace_root, ["note.txt"], state_dir=workspace_root)


def test_git_head_change_invalidates_unchanged_selected_file(workspace_root):
    def git(*args):
        return subprocess.run(["git", *args], cwd=workspace_root, check=True, capture_output=True)
    git("init")
    git("add", "note.txt")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "first")
    first, _ = snapshot(workspace_root, ["note.txt"])
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "second")
    second, _ = snapshot(workspace_root, ["note.txt"])
    assert first["files"] == second["files"] and first["hash"] != second["hash"]


@pytest.mark.parametrize("reply", ["{}", "null", "[]", '{"target_hash":"x","findings":[]}',
    '{"target_hash":"h","findings":[],"passed":true}',
    '{"target_hash":"x","target_hash":"h","findings":[]}',
    '{"target_hash":"h","findings":[{"summary":"bad","path":"outside.txt","line":1}]}'])
def test_untrusted_review_output_cannot_self_report_passed(reply):
    with pytest.raises(AsterunError):
        parse_review(reply, {"hash": "h", "files": [{"path": "note.txt"}]})


def test_snapshot_only_claude_uses_independent_process_without_tools(config_path, workspace_root,
                                                                   isolated_env, monkeypatch):
    binary = isolated_env / "claude-review"
    fixture = Path(__file__).parent / "fixtures" / "claude_review.py"
    binary.write_text(f"#!{sys.executable}\n" + fixture.read_text())
    binary.chmod(0o700)
    monkeypatch.setenv("ASTERUN_RUN_CLAUDE", "1")
    write_config(config_path, workspace_root, extra_backends={"reviewer": {"kind": "claude", "bin": str(binary)}},
                 extra={"workflow": {"review_backend": "reviewer", "require_review": True}})
    app = Application.from_paths(config_path, isolated_env / "state", background=True)
    try:
        submit, _ = start(app)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            app.poll()
            data = app.task_get(submit.ids["task_id"]).data
            if data["task"]["quality"]["phase"] == "completed" or data["task"]["paused"]:
                break
            time.sleep(0.02)
        assert data["task"]["acceptance"] == "passed", data
        assert data["run"]["native"]["backend_session_id"] == "isolated-review"
        original = app._session_for_task(app.store.get_task(TaskId(submit.ids["task_id"])))
        assert original["backend_session_id"] is None
    finally:
        app.close()
