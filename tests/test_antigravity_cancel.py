from __future__ import annotations

import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from asterun.application import Application
from asterun.backends.antigravity import DISPATCH_RECEIPT_KEY
from asterun.backends.antigravity_runtime import AntigravityRuntime
from asterun.contracts import RunStatus
from asterun.ids import BackendId, RunId, TaskId
from tests.test_antigravity_backend import agy_config
from tests.test_control_native import action_for, call, send, submit, wait_run


def test_cancel_before_native_spawn_settles_zero_turns(agy_config, isolated_env, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = AntigravityRuntime.run

    def gated(self, *args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(AntigravityRuntime, "run", gated)
    app = Application.from_paths(agy_config, isolated_env / "cancel-state", background=True)
    try:
        _, run_id = submit(app, "cancel without a native process")
        assert entered.wait(2)
        run = call(app, "control.get", {"kind": "Run", "id": run_id})
        send(app, "run.cancel", {"run_id": run_id, "reason": "尚未启动"}, run["revision"])
        app.poll()
        release.set()
        wait_run(app, run_id, lambda run: run["status"] == "CANCELED")
        action = action_for(app, run_id)
        assert action["status"] == "CANCELED"
        reservation = call(app, "control.get", {
            "kind": "Reservation", "id": action["extensions"]["asterun.reservation_id"],
        })
        assert reservation["actual"] == [{"meter": "turns", "amount": 0, "billing_pool_ref": None}]
        legacy = app.store.get_run(RunId(action["extensions"]["asterun.legacy_run_id"]))
        receipt = legacy.native[DISPATCH_RECEIPT_KEY]
        assert receipt["failure_type"] == "cancelled_before_spawn"
        assert receipt["process_started"] is False
        assert receipt["process_exit_code"] is None
        assert receipt["prompt_attempted"] is False
        assert receipt["events_seen"] == 0
    finally:
        release.set()
        app.close()


@pytest.mark.parametrize("change", [
    {"task_id": "other"}, {"run_id": "other"}, {"stage": "spawn"},
    {"prompt_attempted": True}, {"delivery": "may_have_been_sent"},
    {"process_started": True}, {"process_started": None}, {"process_exit_code": 0},
    {"failure_type": None}, {"failure_type": "cancel_unconfirmed"},
])
def test_only_bound_pre_spawn_cancel_receipt_releases_turn(agy_config, isolated_env, change):
    app = Application.from_paths(agy_config, isolated_env / "receipt-state", background=True)
    receipt = {"task_id": "tsk_test", "run_id": "run_test", "stage": "preflight",
               "prompt_attempted": False, "delivery": "not_sent", "process_started": False,
               "process_exit_code": None, "failure_type": "cancelled_before_spawn"}
    run = SimpleNamespace(backend=BackendId("agy"), status=RunStatus.CANCELLED, terminated=True,
                          cancel_requested=True, task_id=TaskId("tsk_test"), id=RunId("run_test"),
                          native={DISPATCH_RECEIPT_KEY: receipt})
    try:
        assert app.control._confirmed_cancel_before_spawn(run)
        receipt.update(change)
        assert not app.control._confirmed_cancel_before_spawn(run)
    finally:
        app.close()


@pytest.mark.parametrize("change", ["wrong_backend", "running", "not_terminated", "not_requested", "native_session"])
def test_cancel_receipt_cannot_override_actual_run_context(agy_config, isolated_env, change):
    app = Application.from_paths(agy_config, isolated_env / "receipt-state", background=True)
    receipt = {"task_id": "tsk_test", "run_id": "run_test", "stage": "preflight",
               "prompt_attempted": False, "delivery": "not_sent", "process_started": False,
               "process_exit_code": None, "failure_type": "cancelled_before_spawn"}
    run = SimpleNamespace(backend=BackendId("agy"), status=RunStatus.CANCELLED, terminated=True,
                          cancel_requested=True, task_id=TaskId("tsk_test"), id=RunId("run_test"),
                          native={DISPATCH_RECEIPT_KEY: receipt})
    try:
        if change == "wrong_backend":
            run.backend = BackendId("fake")
        elif change == "running":
            run.status = RunStatus.RUNNING
        elif change == "not_terminated":
            run.terminated = False
        elif change == "not_requested":
            run.cancel_requested = False
        else:
            run.native["backend_session_id"] = "native-session"
        assert not app.control._confirmed_cancel_before_spawn(run)
    finally:
        app.close()


def test_native_cancel_after_prompt_keeps_one_turn(agy_config, isolated_env):
    ready = isolated_env / "native-ready"
    binary = Path(json.loads(agy_config.read_text())["backends"]["agy"]["bin"])
    source = binary.read_text().replace('MODE = os.environ.get("AGY_FIXTURE_MODE", "happy")', 'MODE = "cancel_ack"')
    source = source.replace('marker = os.environ.get("AGY_FIXTURE_READY")', f'marker = {str(ready)!r}')
    binary.write_text(source)
    app = Application.from_paths(agy_config, isolated_env / "active-cancel-state", background=True)
    try:
        _, run_id = submit(app, "cancel an already delivered turn")
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            app.poll()
            time.sleep(0.01)
        assert ready.exists()
        run = call(app, "control.get", {"kind": "Run", "id": run_id})
        send(app, "run.cancel", {"run_id": run_id, "reason": "确认原生中断"}, run["revision"])
        wait_run(app, run_id, lambda run: run["status"] == "CANCELED")
        action = action_for(app, run_id)
        assert action["status"] == "SUCCEEDED"
        reservation = call(app, "control.get", {
            "kind": "Reservation", "id": action["extensions"]["asterun.reservation_id"],
        })
        assert reservation["actual"] == [{"meter": "turns", "amount": 1, "billing_pool_ref": None}]
        legacy = app.store.get_run(RunId(action["extensions"]["asterun.legacy_run_id"]))
        assert legacy.native[DISPATCH_RECEIPT_KEY]["process_started"] is True
        assert legacy.native[DISPATCH_RECEIPT_KEY]["prompt_attempted"] is True
        assert not app.control._confirmed_cancel_before_spawn(legacy)
    finally:
        app.close()
