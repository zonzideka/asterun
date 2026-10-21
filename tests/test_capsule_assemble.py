"""只读组装续段胶囊：契约原文、工作区、事件进度与校验。"""

import hashlib
import os
import subprocess

from asterun.capsule.assemble import (
    assemble_capsule,
    mandatory_for,
    progress_for,
    verify_capsule,
    workspace_for,
)
from asterun.capsule.model import canonical, sha
from asterun.context_transition import contract
from asterun.contracts import ApprovalRequest, ApprovalState, Event, EventType, Run, RunStatus
from asterun.ids import ApprovalId, RunId, TaskId
from tests.test_handoff_snapshot import setup

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "tester",
    "GIT_AUTHOR_EMAIL": "tester@example.com",
    "GIT_COMMITTER_NAME": "tester",
    "GIT_COMMITTER_EMAIL": "tester@example.com",
}


def _git(root, *args):
    env = os.environ.copy()
    env.update(_GIT_ENV)
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, env=env)


def _init_repo(root):
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-b", "main")


def _commit(root, files, message="init"):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        _git(root, "add", "--", name)
    _git(root, "commit", "-m", message)


def _message(app, run_id, payload):
    app.store.append_event(
        run_id,
        Event(
            seq=0,
            type=EventType.MESSAGE,
            source="test",
            timestamp="2026-01-01T00:00:00Z",
            schema_version=1,
            payload=payload,
        ),
    )


def test_mandatory_matches_task_text_and_keeps_every_finding(isolated_env, workspace_root):
    app, bundle, _directory = setup(isolated_env, workspace_root)
    try:
        task = app.store.get_task(TaskId(bundle["task_id"]))
        original = task.text
        missing = {"status": "open", "detail": "没有单独的 text"}
        task.findings = [
            {"id": "f-1", "status": "open", "text": "发现正文甲\n逐字节"},
            missing,
            {"status": "open", "text": "只有正文"},
        ]
        task.quality = {"checks": [{"name": "unit", "argv": ["pytest", "-q"]}]}
        task.external_require_review = True
        task.review_status = "required"
        task.repair_count = 1
        app.store.save_task(task)
        task = app.store.get_task(task.id)

        mandatory = mandatory_for(app, task)
        assert mandatory["objective"] == original
        assert mandatory["objective"].encode("utf-8") == task.text.encode("utf-8")
        assert "不能改公共接口" in mandatory["objective"]
        assert mandatory["constraints"] == []
        assert mandatory["acceptance"] == [{"name": "unit", "argv": ["pytest", "-q"]}]
        assert mandatory["acceptance"] is not task.quality["checks"]
        assert mandatory["review_requirements"] == {
            "require_review": True,
            "review_status": "required",
        }
        assert mandatory["limits"] == {
            "max_runs_per_task": app.config.scheduler.max_runs_per_task,
            "max_repairs": app.config.workflow.max_repairs,
            "repair_count": 1,
        }
        assert [item["id"] for item in mandatory["findings"]] == ["f-1", "idx1", "idx2"]
        assert mandatory["findings"][0]["text"].encode("utf-8") == "发现正文甲\n逐字节".encode("utf-8")
        assert mandatory["findings"][1]["text"] == canonical(missing).decode("utf-8")
        assert mandatory["findings"][1]["status"] == "open"
        assert mandatory["findings"][2]["text"] == "只有正文"
        mandatory["findings"][0]["text"] = "changed"
        assert task.findings[0]["text"] == "发现正文甲\n逐字节"
    finally:
        app.close()


def test_workspace_for_reads_git_head_and_dirty_files(tmp_path):
    root = tmp_path / "repo"
    _init_repo(root)
    _commit(root, {"kept.txt": "base\n", "gone.txt": "gone\n"})
    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()
    (root / "kept.txt").write_text("base\nchanged\n", encoding="utf-8")
    (root / "new.txt").write_text("new\n", encoding="utf-8")
    (root / "gone.txt").unlink()

    info = workspace_for(root)

    assert "error" not in info
    assert info["head"] == head_before
    assert info["branch"] == "main"
    by_path = {item["path"]: item for item in info["dirty_files"]}
    assert set(by_path) == {"kept.txt", "new.txt", "gone.txt"}
    assert "M" in by_path["kept.txt"]["status"]
    assert by_path["kept.txt"]["sha256"] == hashlib.sha256(b"base\nchanged\n").hexdigest()
    assert by_path["new.txt"]["status"] == "??"
    assert by_path["new.txt"]["sha256"] == hashlib.sha256(b"new\n").hexdigest()
    assert "D" in by_path["gone.txt"]["status"]
    assert "sha256" not in by_path["gone.txt"]
    assert info["diff_stat"] == {"files": 2, "insertions": 1, "deletions": 1}
    body = {key: info[key] for key in ("head", "branch", "dirty_files", "diff_stat")}
    assert info["binding_hash"] == sha(body)
    assert (root / "kept.txt").read_text(encoding="utf-8") == "base\nchanged\n"
    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert head_after == head_before


def test_workspace_for_rename_does_not_swallow_the_next_entry(tmp_path):
    root = tmp_path / "rename"
    _init_repo(root)
    _commit(root, {"old.txt": "old\n"})
    _git(root, "mv", "old.txt", "new.txt")
    (root / "extra.txt").write_text("extra\n", encoding="utf-8")

    info = workspace_for(root)

    by_path = {item["path"]: item for item in info["dirty_files"]}
    assert "new.txt" in by_path
    assert "extra.txt" in by_path
    assert "old.txt" not in by_path
    assert by_path["new.txt"]["status"].startswith("R")
    assert by_path["new.txt"]["sha256"] == hashlib.sha256(b"old\n").hexdigest()
    assert by_path["extra.txt"]["sha256"] == hashlib.sha256(b"extra\n").hexdigest()


def test_workspace_for_non_git_directory(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert workspace_for(plain) == {
        "head": None,
        "branch": None,
        "dirty_files": None,
        "diff_stat": None,
        "error": "not_git",
    }
    missing = tmp_path / "missing"
    assert workspace_for(missing)["error"] == "not_git"


def test_workspace_for_git_without_commit_is_git_failed(tmp_path):
    root = tmp_path / "empty"
    _init_repo(root)
    info = workspace_for(root)
    assert info["error"] == "git_failed"
    assert info["head"] is None
    assert info["dirty_files"] is None
    assert "binding_hash" not in info


def test_assemble_verifies_until_contract_changes(isolated_env, workspace_root):
    app, bundle, _directory = setup(isolated_env, workspace_root)
    try:
        task = app.store.get_task(TaskId(bundle["task_id"]))
        run = app.store.get_run(RunId(bundle["expected_run_id"]))
        before_seq = app.store.last_event_sequence(run.id)
        capsule = assemble_capsule(app, task, run, trigger="segment_end")
        assert verify_capsule(app, task, capsule) == []
        assert assemble_capsule(app, task, run, trigger="segment_end") == capsule
        assert app.store.last_event_sequence(run.id) == before_seq
        assert capsule["created_at"] == run.created_at
        assert capsule["segment_index"] == len(app.store.list_runs(task.id)) - 1
        assert capsule["task_id"] == task.id.value
        assert capsule["source_run_id"] == run.id.value
        assert capsule["backend_kind"] == "fake"
        assert capsule["budget_ref"] == {"budget_id": None, "ledger_revision": 0}
        assert capsule["authority"]["contract_sha256"] == contract(task)
        assert capsule["authority"]["config_revision"] == task.config_revision
        assert capsule["workspace"]["error"] == "not_git"
        assert capsule["mandatory"]["objective"] == task.text
        assert capsule["progress"]["checks"] == []
        assert capsule["open_items"]["open_questions"] == []

        task.text += "新增限制"
        problems = verify_capsule(app, task, capsule)
        assert "contract_changed" in problems
        assert "mandatory_mismatch" in problems
        assert "mandatory_sha256_mismatch" in problems
    finally:
        app.close()


def test_notes_collect_asterun_note_lines(isolated_env, workspace_root):
    app, bundle, _directory = setup(isolated_env, workspace_root)
    try:
        task = app.store.get_task(TaskId(bundle["task_id"]))
        run = app.store.get_run(RunId(bundle["expected_run_id"]))
        for index in range(120):
            _message(app, run.id, {"kind": "text", "text": f"填充 {index}"})
        _message(
            app,
            run.id,
            {"kind": "text", "text": "普通回复\n  ASTERUN-NOTE: 记下这一步\nASTERUN-NOTE: 第二行\n结尾"},
        )
        capsule = assemble_capsule(app, task, run, trigger="manual")
        assert capsule["notes"]["source"] == "progress_markers"
        assert capsule["notes"]["text"] == "记下这一步\n第二行"
        assert verify_capsule(app, task, capsule) == []
    finally:
        app.close()


def test_progress_redacts_commands_and_flags_unconfirmed_tools(isolated_env, workspace_root):
    app, bundle, _directory = setup(isolated_env, workspace_root)
    try:
        task = app.store.get_task(TaskId(bundle["task_id"]))
        run = app.store.get_run(RunId(bundle["expected_run_id"]))
        run.native = {**run.native, "num_turns": 4, "stop_reason": "end_turn"}
        app.store.save_run(run)
        older = Run(
            id=RunId("run_older"),
            task_id=task.id,
            backend=run.backend,
            status=RunStatus.FAILED,
            created_at="2000-01-01T00:00:00Z",
            native={"num_turns": 2, "stop_reason": "max_turns"},
            error_code="FAKE_SCRIPT_FAILURE",
        )
        app.store.save_run(older)
        secret = "sk-" + ("a" * 24)
        command = secret + " && echo hello " + ("x" * 240)
        tool_input = {"command": command, "path": "src/app.py"}
        _message(app, run.id, {
            "kind": "tool_call",
            "toolName": "bash",
            "toolCallId": "call-open",
            "status": "in_progress",
            "input": tool_input,
        })
        _message(app, run.id, {
            "kind": "tool_call",
            "toolName": "write",
            "toolCallId": "call-done",
            "status": "in_progress",
            "input": {"target_file": "src/out.py", "file_path": "src/also.py"},
        })
        _message(app, run.id, {
            "kind": "tool_result",
            "toolCallId": "call-done",
            "status": "completed",
        })
        _message(app, run.id, {
            "kind": "tool_call",
            "toolName": "read",
            "toolCallId": "call-failed",
            "status": "in_progress",
            "input": {"path": "src/missing.py"},
        })
        _message(app, run.id, {
            "kind": "tool_call_update",
            "toolCallId": "call-failed",
            "status": "failed",
        })
        _message(app, run.id, {
            "kind": "tool_call",
            "toolName": "already",
            "toolCallId": "call-own-done",
            "status": "completed",
            "input": {"path": "src/done.py"},
        })
        approval = ApprovalRequest(
            id=ApprovalId("approval-1"),
            task_id=task.id,
            run_id=run.id,
            summary="need ok",
            state=ApprovalState.PENDING,
            created_at="2026-01-01T00:00:00Z",
        )
        app.store.save_approval(approval)

        progress, open_items, texts = progress_for(app, task, run)

        assert [item["run_id"] for item in progress["segments"]] == ["run_older", run.id.value]
        assert progress["segments"][0]["index"] == 0
        assert progress["segments"][0]["num_turns"] == 2
        assert progress["segments"][0]["stop_reason"] == "max_turns"
        assert progress["segments"][0]["status"] == "failed"
        assert progress["segments"][1]["num_turns"] == 4
        assert progress["segments"][1]["stop_reason"] == "end_turn"
        assert progress["segments"][1]["status"] == "succeeded"
        assert progress["files_touched"] == [
            "src/app.py",
            "src/also.py",
            "src/out.py",
            "src/missing.py",
            "src/done.py",
        ]
        assert progress["checks"] == []
        assert len(progress["commands"]) == 1
        recorded = progress["commands"][0]
        assert recorded["sha256"] == hashlib.sha256(command.encode("utf-8")).hexdigest()
        assert len(recorded["summary"]) <= 200
        assert secret not in recorded["summary"]
        assert "[redacted]" in recorded["summary"]
        assert open_items["unconfirmed_actions"] == [{
            "tool": "bash",
            "input_digest": sha(tool_input),
            "reason": "no_result",
        }]
        found = app.store.find_pending_approval(run.id)
        assert open_items["approvals_not_revived"] == [{
            "action_sha256": sha(found.to_dict()),
            "summary": "pending approval",
        }]
        assert open_items["open_questions"] == []
        assert texts == []
        capsule = assemble_capsule(app, task, run, trigger="observe_only")
        assert capsule["segment_index"] == 1
        assert verify_capsule(app, task, capsule) == []
    finally:
        app.close()
