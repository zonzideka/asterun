"""标准控制到本机 Codex 协议替身进程的接线验证；无真实模型或账户。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

import pytest

from asterun.application import Application
from asterun.control_protocol import validate
from tests.conftest import write_config


@pytest.fixture
def native_control_app(isolated_env, workspace_root, monkeypatch):
    fixture = Path(__file__).parent / "fixtures" / "codex_server.py"
    binary = isolated_env / "codex-control-protocol-stub"
    binary.write_text(f"#!{sys.executable}\n" + fixture.read_text(encoding="utf-8"), encoding="utf-8")
    binary.chmod(0o700)
    # isolated_env 已隔离 HOME、ASTERUN_HOME、PATH 与账号引用；这里只允许显式替身 binary。
    monkeypatch.setenv("ASTERUN_RUN_CODEX", "1")
    config = write_config(
        isolated_env / "control-native-config.json", workspace_root,
        extra_backends={"codex": {"kind": "codex", "bin": str(binary)}},
        extra={"default_backend": "codex"},
    )
    app = Application.from_paths(config, isolated_env / "control-native-state", background=True)
    try:
        yield app
    finally:
        app.close()


def call(app, method, params=None):
    result = app.handle(method, params or {})
    assert result.ok, result.error
    return result.data


def send(app, method, params, revision=0):
    expires = datetime.now(timezone.utc) + timedelta(minutes=5)
    return call(app, "control.command", {"command": {
        "schema_version": "runner-control/v1", "request_id": "req_" + uuid4().hex,
        "idempotency_key": uuid4().hex, "expected_revision": revision,
        "expires_at": expires.isoformat().replace("+00:00", "Z"),
        "method": method, "params": params,
    }})


def submit(app, objective):
    resource = call(app, "control.resources")["resources"][0]
    response = send(app, "task.create", {
        "title": "隔离协议进程验证", "objective": objective, "bot_instance_id": None,
        "resource_scopes": [{key: resource[key] for key in ("resource_id", "operations", "selector")}],
        "constraints": [],
        "acceptance": [{"id": "acc_native_output", "validator_id": "val_output", "input_artifact_ids": [],
                        "required": True, "subject_revision_sha256": None}],
        "budget_limits": [{"meter": "turns", "limit": 1, "enforcement": "hard", "billing_pool_ref": None}],
    })
    task = call(app, "control.get", {"kind": "Task", "id": response["result_ref"]})
    resources = call(app, "control.resources")
    started = send(app, "run.start", {
        "task_id": task["id"], "workflow_id": "wf_execute", "workflow_revision": 1,
        "configuration_sha256": resources["configuration_sha256"],
    }, task["revision"])
    return task["id"], started["result_ref"]


def wait_run(app, run_id, condition, timeout=8):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        app.poll()
        last = call(app, "control.get", {"kind": "Run", "id": run_id})
        if condition(last):
            return last
        time.sleep(0.02)
    pytest.fail(f"未等到标准 Run 状态：{last}")


def action_for(app, run_id):
    actions = [item for item in app.control.store.list("Action", app.control.namespace)
               if item["run_id"] == run_id]
    assert len(actions) == 1
    return actions[0]


def test_standard_run_reaches_native_process_and_binds_verified_execution_evidence(native_control_app, workspace_root):
    app = native_control_app
    task_id, run_id = submit(app, "finish")
    final = wait_run(app, run_id, lambda run: run["status"] == "SUCCEEDED")
    task = call(app, "control.get", {"kind": "Task", "id": task_id})
    assert task["status"] == "SUCCEEDED"
    assignment = call(app, "control.get", {"kind": "Assignment", "id": final["assignment_ids"][0]})
    session = call(app, "control.get", {"kind": "Session", "id": assignment["session_ids"][0]})
    validate(session, "Session")
    assert session["status"] == "IDLE"
    assert session["native_binding"]["provider_session_id"] == "native-thread-1"
    assert session["native_binding"]["provider_turn_id"] == "native-turn-1"
    assert session["native_binding"]["desktop_visibility"] == "unverified"
    action = action_for(app, run_id)
    assert action["status"] == "SUCCEEDED"
    assert action["dispatched_at"] and action["receipt_evidence_ids"]
    reservation = call(app, "control.get", {"kind": "Reservation", "id": action["extensions"]["asterun.reservation_id"]})
    assert reservation["status"] == "SETTLED"
    assert reservation["actual"] == [{"meter": "turns", "amount": 1, "billing_pool_ref": None}]
    evidence = [call(app, "control.get", {"kind": "Evidence", "id": item}) for item in final["completion_evidence_ids"]]
    assert evidence and all(item["validator_id"] == "val_output" and item["verdict"] == "PASS" for item in evidence)
    artifact = call(app, "control.content", {"artifact_id": task["result_artifact_ids"][0]})
    assert artifact["text"] == "隔离进程已完成"
    audit = [json.loads(line) for line in (workspace_root / "native-calls.jsonl").read_text().splitlines()]
    assert sum(item.get("method") == "thread/start" for item in audit) == 1
    assert sum(item.get("method") == "turn/start" for item in audit) == 1


def test_standard_waiting_run_accepts_only_bound_native_approval(native_control_app, workspace_root):
    app = native_control_app
    _, run_id = submit(app, "approval")
    waiting = wait_run(app, run_id, lambda run: run["status"] == "WAITING")
    assert waiting["blocked_reason"] == "APPROVAL_REQUIRED"
    action = action_for(app, run_id)
    reservation_id = action["extensions"]["asterun.reservation_id"]
    assert call(app, "control.get", {"kind": "Reservation", "id": reservation_id})["status"] == "HELD"
    legacy_id = action["extensions"]["asterun.legacy_task_id"]
    legacy = call(app, "task.get", {"task_id": legacy_id})
    approval = legacy["approval"]
    assert approval["native_request"]["id"] == "native-approval"
    wrong = app.handle("approval.respond", {"approval_id": approval["id"], "decision": "approve",
                                            "expected_target_hash": "incorrect-target"})
    assert not wrong.ok
    call(app, "approval.respond", {"approval_id": approval["id"], "decision": "approve",
                                   "expected_target_hash": approval["target_hash"]})
    repeated = app.handle("approval.respond", {"approval_id": approval["id"], "decision": "approve"})
    assert not repeated.ok
    final = wait_run(app, run_id, lambda run: run["status"] == "SUCCEEDED")
    assert final["completion_evidence_ids"]
    assert call(app, "control.get", {"kind": "Reservation", "id": reservation_id})["status"] == "SETTLED"
    audit = [json.loads(line) for line in (workspace_root / "native-calls.jsonl").read_text().splitlines()]
    approvals = [item for item in audit if item.get("id") == "native-approval" and "result" in item]
    assert len(approvals) == 1
    assert approvals[0]["result"]["decision"] == "accept"
    assert sum(item.get("method") == "turn/start" for item in audit) == 1
