from __future__ import annotations

import json

import pytest

from asterun.application import Application
from tests.conftest import write_config
from tests.test_fixed_read_submission import scope
from tests.test_native_lifecycle import wait_for


@pytest.mark.parametrize("reviewer_kind,substitute", [
    ("codex", False), ("claude", False), ("claude", True),
])
def test_fixed_read_task_keeps_independent_quality_review_permissions(
    isolated_env, workspace_root, reviewer_kind, substitute,
):
    """走核心质量编排；所有后端执行/续接边界均为本地替身，不启动原生程序。"""
    configured_reviewer = "unavailable" if substitute else "reviewer"
    config = write_config(isolated_env / "config.json", workspace_root, extra_backends={
        "reader": {"kind": "codex"},
        "reviewer": {"kind": reviewer_kind},
        "unavailable": {"kind": "codex", "enabled": False},
    }, extra={"workflow": {
        "require_review": True, "review_backend": configured_reviewer,
        "approved_substitute": "reviewer" if substitute else None,
    }})
    app = Application.from_paths(config, isolated_env / "state", background=True)
    calls, resumes = [], []

    def execution(backend_name):
        def dispatch(task_id, run_id, script, text, **kwargs):
            calls.append({"method": "stream", "backend": backend_name,
                          "read_scope": kwargs.get("read_scope")})
            return {"status": "succeeded", "terminated": True, "summary": "离线读取完成",
                    "native": {"thread_id": f"{backend_name}-thread", "turn_id": "turn-1"}}
        return dispatch

    def review(task_id, run_id, script, text, *, cwd):
        context = json.loads(text.split("\n", 1)[1])
        calls.append({"method": "review", "backend": "reviewer", "context": context})
        return {"status": "succeeded", "terminated": True,
                "summary": json.dumps({"target_hash": context["target"]["hash"], "findings": []}),
                "native": {"thread_id": "independent-review-thread", "turn_id": "review-turn-1"}}

    def resume(backend_name):
        def inspect(native, cwd, **kwargs):
            resumes.append({"backend": backend_name, "native": native, **kwargs})
            return {"native_checked": True, "native_resumed": True}
        return inspect

    for backend_name in ("reader", "reviewer"):
        backend = app.backends[backend_name]
        backend.can_dispatch = lambda: True
        backend.dispatch_stream = execution(backend_name)
        backend.resume_native = resume(backend_name)
    app.backends["reviewer"].dispatch_review = review
    binding = scope(workspace_root)
    try:
        submitted = app.handle("task.submit", {
            "workspace": "demo", "backend": "reader", "text": "固定范围读取",
            "read_scope": binding, "idempotency_key": "read-before-quality",
        })
        assert submitted.ok, submitted.error
        wait_for(app, submitted.ids["task_id"], lambda data: data["run"]["status"] == "succeeded")
        started = app.handle("workflow.start", {
            "task_id": submitted.ids["task_id"], "idempotency_key": "independent-quality",
            "target_paths": ["note.txt"], "checks": [{"kind": "file_exists", "path": "note.txt"}],
            "auto_repair": False,
        })
        assert started.ok, started.error
        result = wait_for(app, submitted.ids["task_id"],
                          lambda data: data["task"]["quality"]["phase"] == "completed")
        task, run = result["task"], result["run"]
        assert task["acceptance"] == "passed" and task["read_scope"] == binding
        assert [(call["method"], call["backend"]) for call in calls] == [
            ("stream", "reader"), ("review", "reviewer")]
        assert calls[0]["read_scope"].binding == binding
        assert {row["path"] for row in calls[1]["context"]["files"]} == {"note.txt"}
        evidence = task["quality"]["reviews"][0]
        assert evidence["permission"] == "snapshot_only_no_tools"
        assert evidence["used_substitute"] is substitute
        assert run["conversation_id"] != task["conversation_id"]

        # 原读取会话仍使用原范围；独立质量会话不能继承原任务的动态工具。
        for conversation_id in (task["conversation_id"], run["conversation_id"]):
            checked = app.handle("session.resume", {"conversation_id": conversation_id, "native": True})
            assert checked.ok and checked.data["native_resumed"], checked.error
        assert resumes[0]["read_scope"].binding == binding
        assert resumes[0]["native"]["backend_session_id"] == "reader-thread"
        assert "read_scope" not in resumes[1]
        assert resumes[1]["native"]["backend_session_id"] == "independent-review-thread"

        # 独立审查会话后续普通提交也保持自己的无固定范围身份。
        continued = app.handle("task.submit", {
            "workspace": "demo", "backend": "reviewer", "text": "后续普通任务",
            "conversation_id": run["conversation_id"], "idempotency_key": "continue-independent-review",
        })
        assert continued.ok, continued.error
        wait_for(app, continued.ids["task_id"], lambda data: data["run"]["status"] == "succeeded")
        assert calls[-1]["method"] == "stream" and calls[-1]["backend"] == "reviewer"
        assert calls[-1]["read_scope"] is None
    finally:
        app.close()
