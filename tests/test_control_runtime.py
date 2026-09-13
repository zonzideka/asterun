"""runner-control behavior against temporary SQLite and the offline fake backend."""
from __future__ import annotations

from copy import deepcopy
import hashlib
from importlib.resources import files
from uuid import uuid4

import pytest

from asterun.application import Application
from asterun import control_protocol as protocol
from asterun.control import ControlRuntime
from asterun.errors import AsterunError
from asterun.policy import DenyAll, Principal


@pytest.fixture
def runtime(config_path, isolated_env):
    app = Application.from_paths(config_path, isolated_env / "control-state")
    control = getattr(app, "control", None) or ControlRuntime(app)
    try:
        yield control
    finally:
        if getattr(app, "control", None) is not control:
            control.close()
        app.close()


def command(method, params, revision=0, *, key=None):
    return {"schema_version": protocol.SCHEMA_VERSION, "request_id": "req_" + uuid4().hex,
            "idempotency_key": key or uuid4().hex, "expected_revision": revision,
            "expires_at": "2099-01-01T00:00:00Z", "method": method, "params": params}


def create_command(runtime):
    resource = runtime.resources()["resources"][0]
    return command("task.create", {
        "title": "离线规范验收", "objective": "输出离线协议验证结果", "bot_instance_id": None,
        "resource_scopes": [{key: resource[key] for key in ("resource_id", "operations", "selector")}],
        "constraints": [],
        "acceptance": [{"id": "acc_execution", "validator_id": "val_execution", "input_artifact_ids": [],
                        "required": True, "subject_revision_sha256": None}],
        "budget_limits": [{"meter": "turns", "limit": 2, "enforcement": "hard", "billing_pool_ref": None}],
    })


def create_task(runtime):
    response = runtime.command(create_command(runtime))
    return runtime.get("Task", response["result_ref"])


def start_command(runtime, task):
    return command("run.start", {
        "task_id": task["id"], "workflow_id": "wf_execute", "workflow_revision": 1,
        "configuration_sha256": runtime.configuration_sha256,
    }, task["revision"])


def state_command(task, value, revision=0):
    return command("state.put", {"task_id": task["id"], "key": "domain.decision", "value": value,
                                 "assertion_class": "decision", "evidence_ids": []}, revision)


def assert_error(code, call):
    with pytest.raises(AsterunError) as caught:
        call()
    assert caught.value.code == code


def test_transitions_keep_attachment_bytes_and_are_copied():
    raw = files("asterun").joinpath("schemas/state-machines.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == "99ceb7f0b8a30160d06085d0c2b645e618506a393941a7ebf78ff5c3bfa5ebb6"
    first = protocol.transitions()
    assert first["Run"]["PAUSING"] == ["PAUSED", "RECONCILING", "CANCELING"]
    first["Run"]["SUCCEEDED"].append("RUNNING")
    assert protocol.transitions()["Run"]["SUCCEEDED"] == []


def test_create_is_durable_and_events_are_ordered(runtime):
    request = create_command(runtime)
    response = runtime.command(request)
    task = runtime.get("Task", response["result_ref"])
    assert task["status"] == "READY"
    assert task["revision"] == 2
    assert task["owner_id"] == runtime.principal
    assert task["bot_instance_id"] is None
    protocol.validate(task, "Task")
    operation = runtime.get("Operation", response["operation_id"])
    assert operation["payload_sha256"] == protocol.command_digest(request)
    assert operation["result_ref"] == task["id"]
    events = runtime.events(task["id"])
    assert [item["event_type"] for item in events["events"]] == ["task.created", "task.state_changed"]
    assert [item["sequence"] for item in events["events"]] == [1, 2]
    assert runtime.events(task["id"], after=1)["events"] == [events["events"][1]]
    assert runtime.app.backends["fake"].dispatch_count == 0


def test_state_cas_conflict_keeps_value_revision_and_events(runtime):
    task = create_task(runtime)
    first = runtime.command(state_command(task, {"choice": "a"}))
    state = runtime.get("State", first["result_ref"])
    assert state["revision"] == 1
    runtime.command(state_command(task, {"choice": "b"}, revision=1))
    updated = runtime.get("State", state["id"])
    count = runtime.events(task["id"])["next_sequence"]
    assert updated["revision"] == 2
    assert updated["value_sha256"] == protocol.digest({"choice": "b"})
    assert_error("REVISION_CONFLICT", lambda: runtime.command(state_command(task, {"choice": "c"}, revision=1)))
    assert_error("REVISION_CONFLICT", lambda: runtime.command(state_command(task, {"choice": "c"}, revision=0)))
    assert runtime.get("State", state["id"]) == updated
    assert runtime.events(task["id"])["next_sequence"] == count


def test_idempotent_replay_precedes_expiry_and_cas_but_not_authorization(runtime, monkeypatch):
    task = create_task(runtime)
    request = state_command(task, {"answer": 1})
    response = runtime.command(request)
    runtime.command(state_command(task, {"answer": 2}, revision=1))
    monkeypatch.setattr("asterun.control.now", lambda: "2100-01-01T00:00:00Z")
    replay = deepcopy(request)
    replay["request_id"] = "req_replayed"
    result = runtime.command(replay)
    assert result["operation_id"] == response["operation_id"]
    assert result["request_id"] == "req_replayed"
    assert runtime.get("State", response["result_ref"])["value"] == {"answer": 2}
    monkeypatch.setattr(runtime.app, "policy", DenyAll())
    assert_error("SCOPE_DENIED", lambda: runtime.command(replay))


def test_same_key_changed_semantics_conflicts_without_mutation(runtime):
    request = create_command(runtime)
    original = runtime.command(request)
    request["params"]["objective"] = "不同目标"
    assert_error("IDEMPOTENCY_CONFLICT", lambda: runtime.command(request))
    assert len(runtime.store.list("Task", runtime.namespace)) == 1
    assert runtime.get("Task", original["result_ref"])["objective"] == "输出离线协议验证结果"


def test_new_expired_commands_do_not_create_objects(runtime):
    request = create_command(runtime)
    request["expires_at"] = "2000-01-01T00:00:00Z"
    assert_error("INVALID_ARGUMENT", lambda: runtime.command(request))
    assert runtime.store.list("Task", runtime.namespace) == []
    assert runtime.store.list("Operation", runtime.namespace) == []


def test_different_authenticated_principal_cannot_read_or_write(runtime, monkeypatch):
    task = create_task(runtime)
    first = runtime.command(state_command(task, "private"))
    monkeypatch.setattr(runtime.app, "principal", Principal("other-user", "test"))
    assert_error("FORBIDDEN", lambda: runtime.get("Task", task["id"]))
    assert_error("FORBIDDEN", lambda: runtime.get("State", first["result_ref"]))
    assert_error("FORBIDDEN", lambda: runtime.events(task["id"]))
    assert_error("FORBIDDEN", lambda: runtime.command(state_command(task, "overwritten", revision=1)))
    assert runtime.app.backends["fake"].dispatch_count == 0


def test_fake_run_requires_current_configuration_and_emits_acceptance_evidence(runtime):
    task = create_task(runtime)
    invalid = start_command(runtime, task)
    invalid["params"]["configuration_sha256"] = "f" * 64
    assert_error("REVISION_CONFLICT", lambda: runtime.command(invalid))
    assert runtime.store.list("Run", runtime.namespace) == []
    accepted = runtime.command(start_command(runtime, task))
    run = runtime.get("Run", accepted["result_ref"])
    assert run["status"] == "QUEUED"
    assert runtime.app.backends["fake"].dispatch_count == 0
    runtime.advance()
    run = runtime.get("Run", run["id"])
    assert run["status"] == "SUCCEEDED"
    assert runtime.get("Task", task["id"])["status"] == "SUCCEEDED"
    assert runtime.app.backends["fake"].dispatch_count == 1
    evidence = [runtime.get("Evidence", item) for item in run["completion_evidence_ids"]]
    assert evidence and all(item["verdict"] == "PASS" for item in evidence)
    assert all(item["validator_id"] == "val_execution" for item in evidence)
    reservations = runtime.store.list("Reservation", runtime.namespace)
    assert len(reservations) == 1
    assert reservations[0]["status"] == "SETTLED"
    assert reservations[0]["actual"] == [{"meter": "turns", "amount": 1, "billing_pool_ref": None}]
    runtime.advance()
    assert runtime.app.backends["fake"].dispatch_count == 1


def test_required_output_validation_can_fail_after_backend_success(runtime, monkeypatch):
    request = create_command(runtime)
    request["params"]["acceptance"][0]["validator_id"] = "val_output"
    response = runtime.command(request)
    task = runtime.get("Task", response["result_ref"])
    backend = runtime.app.backends["fake"]
    dispatch = backend.dispatch

    def empty_output(*args, **kwargs):
        result = dispatch(*args, **kwargs)
        result["summary"] = " "
        return result

    monkeypatch.setattr(backend, "dispatch", empty_output)
    accepted = runtime.command(start_command(runtime, task))
    runtime.advance()
    run = runtime.get("Run", accepted["result_ref"])
    assert run["status"] == "FAILED"
    assert runtime.get("Task", task["id"])["status"] == "FAILED"
    evidence = [runtime.get("Evidence", item) for item in run["completion_evidence_ids"]]
    assert evidence[0]["verdict"] == "FAIL"
    assert runtime.store.list("Action", runtime.namespace)[0]["status"] == "SUCCEEDED"


def artifact_command(task, upload):
    return command("artifact.register", {
        "task_id": task["id"], "run_id": None, "assignment_id": None, "upload_ref": upload["id"],
        "artifact_type": "document", "media_type": "text/plain", "sha256": upload["sha256"],
        "size_bytes": upload["size_bytes"], "sensitivity": "internal",
    })


def test_upload_registration_verifies_bytes_without_accepting_paths(runtime):
    task = create_task(runtime)
    upload = runtime.upload(task["id"], "离线产物😀")
    assert upload["size_bytes"] == len("离线产物😀".encode())
    invalid = artifact_command(task, upload)
    invalid["params"]["sha256"] = "0" * 64
    assert_error("INVALID_ARGUMENT", lambda: runtime.command(invalid))
    assert runtime.store.list("Artifact", runtime.namespace) == []
    valid = artifact_command(task, upload)
    response = runtime.command(valid)
    artifact = runtime.get("Artifact", response["result_ref"])
    assert artifact["verification"] == "unverified"
    assert artifact["storage_ref"].startswith("sqlite:sha256:")
    assert runtime.artifact_content(artifact["id"])["text"] == "离线产物😀"
    assert runtime.command(valid)["operation_id"] == response["operation_id"]
    assert len(runtime.store.list("Artifact", runtime.namespace)) == 1
    invalid = artifact_command(task, upload)
    invalid["params"]["storage_ref"] = "/tmp/forged-output.txt"
    assert_error("INVALID_REQUEST", lambda: runtime.command(invalid))


def test_upload_from_another_task_cannot_be_registered(runtime):
    first, second = create_task(runtime), create_task(runtime)
    upload = runtime.upload(first["id"], "restricted source")
    assert_error("FORBIDDEN", lambda: runtime.command(artifact_command(second, upload)))
    assert runtime.store.list("Artifact", runtime.namespace) == []


def test_unsupported_commands_and_bot_profiles_are_not_advertised(runtime):
    advertised = runtime.discovery()["supported_commands"]
    for method in ("message.send", "action.propose", "approval.request", "setup.answer", "setup.verify"):
        assert method not in advertised
    request = command("setup.answer", {"bot_instance_id": "bot_example", "step_id": "stp_example", "answer": "yes"})
    assert_error("CAPABILITY_UNSUPPORTED", lambda: runtime.command(request))
    request = create_command(runtime)
    request["params"]["bot_instance_id"] = "bot_example"
    assert_error("CAPABILITY_UNSUPPORTED", lambda: runtime.command(request))
    assert runtime.store.list("Operation", runtime.namespace) == []
    assert runtime.store.list("Task", runtime.namespace) == []


def test_queued_cancel_is_confirmed_before_releasing_budget(runtime):
    task = create_task(runtime)
    accepted = runtime.command(start_command(runtime, task))
    run = runtime.get("Run", accepted["result_ref"])
    runtime.command(command("run.cancel", {"run_id": run["id"], "reason": "用户取消"}, run["revision"]))
    assert runtime.get("Run", run["id"])["status"] == "CANCELING"
    assert runtime.store.list("Reservation", runtime.namespace)[0]["status"] == "HELD"
    runtime.advance()
    assert runtime.get("Run", run["id"])["status"] == "CANCELED"
    assert runtime.get("Task", task["id"])["status"] == "CANCELED"
    assert runtime.store.list("Reservation", runtime.namespace)[0]["actual"][0]["amount"] == 0
    assert runtime.app.backends["fake"].dispatch_count == 0


def test_queued_takeover_and_return_preserve_single_dispatch(runtime):
    task = create_task(runtime)
    accepted = runtime.command(start_command(runtime, task))
    run = runtime.get("Run", accepted["result_ref"])
    runtime.command(command("run.takeover", {"run_id": run["id"], "requested_owner": "human"}, run["revision"]))
    runtime.advance()
    paused = runtime.get("Run", run["id"])
    assert paused["status"] == "PAUSED"
    assert paused["control_owner"]["kind"] == "human"
    assert runtime.app.backends["fake"].dispatch_count == 0
    runtime.command(command("run.return_control", {"run_id": run["id"]}, paused["revision"]))
    runtime.advance()
    assert runtime.get("Run", run["id"])["status"] == "SUCCEEDED"
    assert runtime.app.backends["fake"].dispatch_count == 1


def test_revoked_grant_denies_delivery_before_backend_call(runtime):
    task = create_task(runtime)
    accepted = runtime.command(start_command(runtime, task))
    grant = runtime.store.list("Grant", runtime.namespace)[0]
    runtime.command(command("grant.revoke", {"grant_id": grant["id"], "reason": "撤销授权"}, grant["revision"]))
    runtime.advance()
    assert runtime.get("Run", accepted["result_ref"])["status"] == "FAILED"
    assert runtime.app.backends["fake"].dispatch_count == 0
    assert runtime.store.list("Action", runtime.namespace)[0]["status"] == "DENIED"
    assert runtime.store.list("Reservation", runtime.namespace)[0]["actual"][0]["amount"] == 0


def test_restart_preserves_admission_and_replay_before_single_delivery(runtime, config_path):
    task = create_task(runtime)
    request = start_command(runtime, task)
    accepted = runtime.command(request)
    state_dir = runtime.app.state_dir
    runtime.app.close()
    reopened = Application.from_paths(config_path, state_dir)
    control = getattr(reopened, "control", None) or ControlRuntime(reopened)
    try:
        replay = control.command(request)
        assert replay["operation_id"] == accepted["operation_id"]
        assert replay["result_ref"] == accepted["result_ref"]
        assert len(control.store.list("Run", control.namespace)) == 1
        control.advance()
        assert control.get("Run", accepted["result_ref"])["status"] == "SUCCEEDED"
        assert reopened.backends["fake"].dispatch_count == 1
        assert control.command(request)["operation_id"] == accepted["operation_id"]
        control.advance()
        assert reopened.backends["fake"].dispatch_count == 1
    finally:
        if getattr(reopened, "control", None) is not control:
            control.close()
        reopened.close()


def test_unknown_remote_keeps_reservation_and_never_resubmits(runtime, monkeypatch):
    task = create_task(runtime)
    backend = runtime.app.backends["fake"]
    dispatch = backend.dispatch

    def lost_reply(task_id, run_id, script, text, **kwargs):
        return dispatch(task_id, run_id, "lost_reply", text, **kwargs)

    monkeypatch.setattr(backend, "dispatch", lost_reply)
    accepted = runtime.command(start_command(runtime, task))
    for _ in range(3):
        runtime.advance()
    assert runtime.get("Run", accepted["result_ref"])["status"] == "RECONCILING"
    assert runtime.store.list("Action", runtime.namespace)[0]["status"] == "UNKNOWN_REMOTE"
    assert runtime.store.list("Reservation", runtime.namespace)[0]["status"] == "UNCERTAIN"
    assert backend.dispatch_count == 1


def test_failed_retry_cannot_exceed_hard_turn_budget(runtime, monkeypatch):
    request = create_command(runtime)
    request["params"]["budget_limits"][0]["limit"] = 1
    response = runtime.command(request)
    task = runtime.get("Task", response["result_ref"])
    backend = runtime.app.backends["fake"]
    dispatch = backend.dispatch

    def failed(task_id, run_id, script, text, **kwargs):
        return dispatch(task_id, run_id, "failure", text, **kwargs)

    monkeypatch.setattr(backend, "dispatch", failed)
    accepted = runtime.command(start_command(runtime, task))
    runtime.advance()
    task = runtime.get("Task", task["id"])
    assert task["status"] == "FAILED"
    retry = start_command(runtime, task)
    retry["method"] = "run.retry"
    retry["params"]["previous_run_id"] = accepted["result_ref"]
    assert_error("BUDGET_EXHAUSTED", lambda: runtime.command(retry))
    assert len(runtime.store.list("Run", runtime.namespace)) == 1
    assert runtime.get("Task", task["id"]) == task
    assert backend.dispatch_count == 1
