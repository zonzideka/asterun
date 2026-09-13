"""Control 生命周期跨入口及故障边界回归；使用离线 fake 和原生协议替身。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time
from uuid import uuid4

import pytest

from asterun.application import Application
from asterun.control import ControlRuntime, identifier
from asterun.contracts import OperationStatus, RunStatus
from asterun.errors import AsterunError
from asterun.ids import AccountRef, BackendSessionId, RuntimeRef
from tests.conftest import write_config
from tests.test_grok_environment import write_failure_grok_stub


@pytest.fixture
def control(app):
    runtime = getattr(app, "control", None)
    if runtime is None:
        runtime = ControlRuntime(app)
        app.control = runtime
    yield runtime
    app.close()


def command(method, params, revision=0):
    return {"schema_version": "runner-control/v1", "request_id": "req_" + uuid4().hex,
            "idempotency_key": "test_recovery_" + uuid4().hex,
            "expected_revision": revision,
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            "method": method, "params": params}


def create_and_start(control):
    resource = control.resources()["resources"][0]
    created = control.command(command("task.create", {
        "title": "控制层恢复测试", "objective": "执行一次离线任务", "bot_instance_id": None,
        "resource_scopes": [{"resource_id": resource["resource_id"], "operations": ["agent.execute"], "selector": None}],
        "constraints": [], "acceptance": [{"id": "acc_execution", "validator_id": "val_execution",
            "input_artifact_ids": [], "required": True, "subject_revision_sha256": None}],
        "budget_limits": [{"meter": "turns", "limit": 1, "enforcement": "hard", "billing_pool_ref": None}],
    }))
    task = control.get("Task", created["result_ref"])
    started = control.command(command("run.start", {"task_id": task["id"], "workflow_id": "wf_execute",
        "workflow_revision": 1, "configuration_sha256": control.configuration_sha256}, task["revision"]))
    return task["id"], started["result_ref"]


def test_control_owned_legacy_task_cannot_spend_an_unbudgeted_repair(control):
    task_id, run_id = create_and_start(control)
    control.advance()
    assert control.get("Run", run_id)["status"] == "SUCCEEDED"
    action = next(item for item in control.store.list("Action", control.namespace) if item["run_id"] == run_id)
    legacy_id = action["extensions"]["asterun.legacy_task_id"]
    before = control.app.backends["fake"].dispatch_count
    result = control.app.handle("workflow.repair", {"task_id": legacy_id, "idempotency_key": "unbudgeted-legacy-repair"})
    assert not result.ok
    assert control.app.backends["fake"].dispatch_count == before
    assert control.get("Task", task_id)["status"] == "SUCCEEDED"


def test_stale_lease_cannot_commit_control_terminal_state(control, monkeypatch):
    _, run_id = create_and_start(control)
    original = control._observe
    monkeypatch.setattr(control, "_observe", lambda *args: None)
    control.advance()
    monkeypatch.setattr(control, "_observe", original)
    run = control.get("Run", run_id)
    lease = run["lease"]
    control.store.release_lease(control.namespace, run_id, lease["holder_id"], lease["fencing_token"])
    control.store.claim_lease(control.namespace, run_id, "wrk_replacement", lease["expires_at"])
    try:
        control.advance()
    except AsterunError as error:
        assert error.code == "LEASE_LOST"
    assert control.get("Run", run_id)["status"] != "SUCCEEDED"
    assert not any(item["validator_id"] == "val_execution" for item in control.store.list("Evidence", control.namespace))


def test_confirmed_legacy_non_delivery_can_cancel_after_control_delivery_mark(control, monkeypatch):
    _, run_id = create_and_start(control)
    original = control.app.task_submit

    def reject_before_legacy_intent(payload):
        raise AsterunError("REVISION_CONFLICT", "模拟底层受理前的确定性拒绝")

    monkeypatch.setattr(control.app, "task_submit", reject_before_legacy_intent)
    control.advance()
    monkeypatch.setattr(control.app, "task_submit", original)
    run = control.get("Run", run_id)
    assert control.app.backends["fake"].dispatch_count == 0
    # 调用底层本地受理明确拒绝且没有 legacy intent，不应永久伪装远端副作用未知。
    assert run["status"] == "FAILED"
    action = next(item for item in control.store.list("Action", control.namespace) if item["run_id"] == run_id)
    reservation = control.get("Reservation", action["extensions"]["asterun.reservation_id"])
    assert reservation["actual"][0]["amount"] == 0


def test_cancel_legacy_intent_before_backend_delivery_does_not_consume_turn(control, monkeypatch):
    _, run_id = create_and_start(control)

    def stop_before_dispatch(task, run, *args):
        return control.app.task_get(task.id.value)

    monkeypatch.setattr(control.app, "_dispatch_committed", stop_before_dispatch)
    control.advance()
    assert control.app.backends["fake"].dispatch_count == 0
    run = control.get("Run", run_id)
    control.command(command("run.cancel", {"run_id": run_id, "reason": "尚未执行，取消"}, run["revision"]))
    control.advance()
    assert control.get("Run", run_id)["status"] == "CANCELED"
    action = next(item for item in control.store.list("Action", control.namespace) if item["run_id"] == run_id)
    reservation = control.get("Reservation", action["extensions"]["asterun.reservation_id"])
    assert reservation["actual"][0]["amount"] == 0
    assert action["status"] == "CANCELED"


def test_restart_after_delivery_marker_proves_absent_local_intent_without_resubmission(config_path, tmp_path, monkeypatch):
    state = tmp_path / "before-submit"
    app = Application.from_paths(config_path, state)
    control = app.control
    _, run_id = create_and_start(control)

    def crash_before_submit(payload):
        raise SystemExit("模拟交付标记之后的进程终止")

    monkeypatch.setattr(app, "task_submit", crash_before_submit)
    try:
        with pytest.raises(SystemExit):
            control.advance()
    finally:
        app.close()
    recovered = Application.from_paths(config_path, state)
    try:
        recovered.control.advance()
        assert recovered.backends["fake"].dispatch_count == 0
        assert recovered.control.get("Run", run_id)["status"] == "FAILED"
        action = next(item for item in recovered.control.store.list("Action", "ns_local") if item["run_id"] == run_id)
        assert action["status"] == "FAILED"
        reservation = recovered.control.get("Reservation", action["extensions"]["asterun.reservation_id"])
        assert reservation["status"] == "SETTLED"
        assert reservation["actual"][0]["amount"] == 0
    finally:
        recovered.close()


def test_restart_after_backend_receipt_settles_without_resubmission(config_path, tmp_path, monkeypatch):
    state = tmp_path / "after-submit"
    app = Application.from_paths(config_path, state)
    control = app.control
    _, run_id = create_and_start(control)

    def crash_before_receipt(*args):
        raise SystemExit("模拟后端已结束、控制层回执未保存")

    monkeypatch.setattr(control, "_observe", crash_before_receipt)
    try:
        with pytest.raises(SystemExit):
            control.advance()
        assert app.backends["fake"].dispatch_count == 1
    finally:
        app.close()
    recovered = Application.from_paths(config_path, state)
    try:
        recovered.control.advance()
        assert recovered.backends["fake"].dispatch_count == 0
        assert recovered.control.get("Run", run_id)["status"] == "SUCCEEDED"
        action = next(item for item in recovered.control.store.list("Action", "ns_local") if item["run_id"] == run_id)
        reservation = recovered.control.get("Reservation", action["extensions"]["asterun.reservation_id"])
        assert reservation["status"] == "SETTLED"
        assert reservation["actual"][0]["amount"] == 1
    finally:
        recovered.close()


def test_pause_recovered_legacy_intent_preserves_it_for_resume(config_path, tmp_path, monkeypatch):
    state = tmp_path / "queued-intent"
    app = Application.from_paths(config_path, state)
    control = app.control
    _, run_id = create_and_start(control)

    def crash_before_backend(*args):
        raise SystemExit("模拟 legacy intent 落盘但后端尚未派发")

    monkeypatch.setattr(app, "_dispatch_committed", crash_before_backend)
    try:
        with pytest.raises(SystemExit):
            control.advance()
    finally:
        app.close()
    recovered = Application.from_paths(config_path, state)
    try:
        run = recovered.control.get("Run", run_id)
        recovered.control.command(command("run.pause", {"run_id": run_id, "reason": "恢复前先暂停"}, run["revision"]))
        recovered.poll()
        assert recovered.backends["fake"].dispatch_count == 0
        legacy = recovered.store.get_task(recovered.store.list_task_ids()[0])
        legacy_run = recovered.store.get_run(legacy.current_run_id)
        operation = recovered.store.find_operation_for_run(legacy_run.id)
        assert operation.status == OperationStatus.INTENDED
        assert legacy_run.status != RunStatus.FAILED
        run = recovered.control.get("Run", run_id)
        assert run["status"] == "PAUSED"
        recovered.control.command(command("run.resume", {"run_id": run_id, "capsule_id": None}, run["revision"]))
        recovered.poll()
        recovered.poll()
        assert recovered.backends["fake"].dispatch_count == 1
        assert recovered.control.get("Run", run_id)["status"] == "SUCCEEDED"
    finally:
        recovered.close()


@pytest.mark.parametrize("paused", [False, True])
def test_legacy_approval_cannot_resume_side_effects_after_control_pause(control, monkeypatch, paused):
    backend = control.app.backends["fake"]
    original = backend.dispatch

    def wait_for_approval(task_id, run_id, script, text, **kwargs):
        return original(task_id, run_id, "wait_input", text, **kwargs)

    monkeypatch.setattr(backend, "dispatch", wait_for_approval)
    _, run_id = create_and_start(control)
    control.advance()
    run = control.get("Run", run_id)
    assert run["status"] == "WAITING"
    if paused:
        control.command(command("run.pause", {"run_id": run_id, "reason": "先暂停，禁止继续工具执行"}, run["revision"]))
    legacy = control.app.store.get_task(control.app.store.list_task_ids()[0])
    approval = control.app.store.find_pending_approval(legacy.current_run_id)
    result = control.app.handle("approval.respond", {"approval_id": approval.id.value, "decision": "approve"})
    assert result.ok is not paused
    assert any(call.method == "respond_approval" for call in backend.calls) is not paused
    assert control.get("Run", run_id)["desired_state"] == ("PAUSED" if paused else "RUNNING")


def test_native_binding_projection_keeps_persisted_account_and_environment(control, monkeypatch):
    _, run_id = create_and_start(control)
    original = control._observe
    monkeypatch.setattr(control, "_observe", lambda *args: None)
    control.advance()
    legacy = control.app.store.get_task(control.app.store.list_task_ids()[0])
    saved = control.app.store.get_session(legacy.conversation_id)
    saved.backend_session_id = BackendSessionId("native_original")
    saved.latest_turn_id = "turn_original"
    control.app.store.save_session(saved)
    original_account, original_runtime = saved.account_ref.value, saved.runtime_ref.value
    control.app.account_ref = AccountRef("account:replacement")
    control.app.runtime_ref = RuntimeRef("runtime:replacement")
    monkeypatch.setattr(control, "_observe", original)
    control.advance()
    session = next(item for item in control.store.list("Session", control.namespace)
                   if item["native_binding"])
    assert session["native_binding"]["account_ref"] == identifier("acc", original_account)
    assert session["native_binding"]["environment_ref"] == identifier("env", original_runtime)


def grok_failure_config(isolated_env, workspace_root, monkeypatch, failure):
    from asterun.grok_profile import prepare_home

    home = isolated_env / "grok-home"
    prepare_home(home)
    capture = isolated_env / "grok-methods.log"
    binary = write_failure_grok_stub(isolated_env / "grok-stub", capture, failure)
    monkeypatch.setenv("ASTERUN_RUN_GROK", "1")
    path = write_config(isolated_env / "grok-config.json", workspace_root,
        extra_backends={"grok": {"kind": "grok", "bin": str(binary), "home": str(home)}},
        extra={"default_backend": "grok"})
    return path, capture


def poll_control_status(app, run_id, expected):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        app.poll()
        if app.control.get("Run", run_id)["status"] == expected:
            return
        time.sleep(0.02)
    raise AssertionError(app.control.get("Run", run_id))


@pytest.mark.parametrize("background", [False, True])
def test_grok_pre_prompt_failure_settles_zero_and_never_relaunches_after_restart(
    isolated_env, workspace_root, monkeypatch, background,
):
    config, capture = grok_failure_config(isolated_env, workspace_root, monkeypatch, "initialize")
    state = isolated_env / "grok-state"
    app = Application.from_paths(config, state, background=background)
    try:
        _, run_id = create_and_start(app.control)
        poll_control_status(app, run_id, "FAILED")
        action = next(item for item in app.control.store.list("Action", "ns_local") if item["run_id"] == run_id)
        reservation = app.control.get("Reservation", action["extensions"]["asterun.reservation_id"])
        assert action["status"] == "FAILED"
        assert reservation["status"] == "SETTLED" and reservation["actual"][0]["amount"] == 0
        assert action["receipt_evidence_ids"]
        assert capture.read_text().splitlines() == ["spawn", "initialize"]
    finally:
        app.close()
    recovered = Application.from_paths(config, state, background=background)
    try:
        recovered.poll()
        assert recovered.control.get("Run", run_id)["status"] == "FAILED"
        assert recovered.backends["grok"].dispatch_count == 0
        assert capture.read_text().splitlines() == ["spawn", "initialize"]
        reservation = recovered.control.get("Reservation", action["extensions"]["asterun.reservation_id"])
        assert reservation["actual"][0]["amount"] == 0
    finally:
        recovered.close()


def test_grok_disconnect_after_prompt_keeps_reservation_and_native_reference_across_restart(
    isolated_env, workspace_root, monkeypatch,
):
    config, capture = grok_failure_config(isolated_env, workspace_root, monkeypatch, "prompt-disconnect")
    state = isolated_env / "grok-state"
    app = Application.from_paths(config, state, background=True)
    try:
        _, run_id = create_and_start(app.control)
        poll_control_status(app, run_id, "RECONCILING")
        action = next(item for item in app.control.store.list("Action", "ns_local") if item["run_id"] == run_id)
        reservation = app.control.get("Reservation", action["extensions"]["asterun.reservation_id"])
        assert action["status"] == "UNKNOWN_REMOTE"
        assert reservation["status"] == "UNCERTAIN" and reservation["actual"] is None
        legacy = app.store.get_task(app.store.list_task_ids()[0])
        saved = app.store.get_session(legacy.conversation_id)
        assert saved.backend_session_id.value == "session-before-failure"
    finally:
        app.close()
    recovered = Application.from_paths(config, state, background=True)
    try:
        recovered.poll()
        assert recovered.control.get("Run", run_id)["status"] == "RECONCILING"
        assert recovered.backends["grok"].dispatch_count == 0
        assert capture.read_text().splitlines().count("session/prompt") == 1
        reservation = recovered.control.get("Reservation", action["extensions"]["asterun.reservation_id"])
        assert reservation["status"] == "UNCERTAIN" and reservation["actual"] is None
    finally:
        recovered.close()


def test_client_state_cannot_claim_no_delivery_and_release_turn_budget(control):
    from asterun.backends.grok import DISPATCH_RECEIPT_KEY

    task_id, run_id = create_and_start(control)
    control.command(command("state.put", {"task_id": task_id, "key": "domain.grok_receipt",
        "value": {DISPATCH_RECEIPT_KEY: {"delivery": "not_sent", "prompt_attempted": False, "stage": "initialize"}},
        "assertion_class": "decision", "evidence_ids": []}))
    control.advance()
    action = next(item for item in control.store.list("Action", "ns_local") if item["run_id"] == run_id)
    reservation = control.get("Reservation", action["extensions"]["asterun.reservation_id"])
    assert action["status"] == "SUCCEEDED"
    assert reservation["actual"][0]["amount"] == 1


def test_non_grok_backend_cannot_forge_grok_not_sent_receipt(control, monkeypatch):
    from asterun.backends.grok import DISPATCH_RECEIPT_KEY

    backend = control.app.backends["fake"]
    original = backend.dispatch

    def forged(task_id, run_id, script, text, **kwargs):
        result = original(task_id, run_id, "failure", text, **kwargs)
        result["native"] = {DISPATCH_RECEIPT_KEY: {
            "task_id": task_id.value, "run_id": run_id.value, "stage": "initialize",
            "prompt_attempted": False, "delivery": "not_sent", "failure_type": "RuntimeError",
        }}
        return result

    monkeypatch.setattr(backend, "dispatch", forged)
    _, run_id = create_and_start(control)
    control.advance()
    action = next(item for item in control.store.list("Action", "ns_local") if item["run_id"] == run_id)
    reservation = control.get("Reservation", action["extensions"]["asterun.reservation_id"])
    assert action["status"] == "SUCCEEDED"
    assert reservation["actual"][0]["amount"] == 1
