from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

from asterun.antigravity_profile import prepare_home
from asterun.application import Application
from asterun.backends.antigravity import AntigravityBackend, DISPATCH_RECEIPT_KEY, discover_antigravity_bin
from asterun.config import BackendConfig
from asterun.contracts import RunStatus
from asterun.ids import BackendId, RunId, TaskId
from tests.conftest import write_config
from tests.test_control_native import submit as control_submit, wait_run, action_for, call


@pytest.fixture
def agy_config(isolated_env, workspace_root, monkeypatch):
    home = isolated_env / "agy-home"
    prepare_home(home)
    fixture = Path(__file__).parent / "fixtures" / "antigravity_cli.py"
    binary = isolated_env / "agy-stub"
    # The independent fixture receives the actual facade's arguments; the shim
    # checks them before entering the raw-protocol fixture's no-argv contract.
    prelude = (
        f"#!{sys.executable}\nimport sys,os\n"
        "assert sys.argv[1:]==['--input-format','stream-json','--output-format','stream-json',"
        "'--model','fixture-model','--disable-slash-commands','--print-timeout','5m',"
        "'--add-dir',os.getcwd()]\n"
        "sys.argv=sys.argv[:1]\n"
    )
    binary.write_text(prelude + fixture.read_text())
    binary.chmod(0o700)
    monkeypatch.setenv("ASTERUN_RUN_ANTIGRAVITY", "1")
    return write_config(isolated_env / "agy-config.json", workspace_root,
        extra_backends={"agy": {"kind": "antigravity", "bin": str(binary), "home": str(home), "model": "fixture-model"}},
        extra={"default_backend": "agy"})


def wait(app, task_id, expected):
    end = time.monotonic() + 5
    while time.monotonic() < end:
        app.poll()
        data = call(app, "task.get", {"task_id": task_id})
        if data["run"]["status"] == expected:
            return data
        time.sleep(0.01)
    pytest.fail(f"终态未到达：{data}")


def test_background_native_id_idempotency_and_restart(agy_config, isolated_env):
    state = isolated_env / "state"
    app = Application.from_paths(agy_config, state, background=True)
    payload = {"workspace": "demo", "backend": "agy", "text": "input", "idempotency_key": "one"}
    try:
        first = app.handle("task.submit", payload)
        assert first.ok, first.error
        final = wait(app, first.ids["task_id"], "succeeded")
        assert final["run"]["summary"] == "fixture response"
        assert final["run"]["native"]["backend_session_id"] == "fixture-conversation-123"
        assert final["run"]["native"][DISPATCH_RECEIPT_KEY]["task_id"] == first.ids["task_id"]
        repeat = app.handle("task.submit", payload)
        assert repeat.ok and repeat.ids["run_id"] == first.ids["run_id"]
        assert app.backends["agy"].dispatch_count == 1
    finally:
        app.close()
    app = Application.from_paths(agy_config, state, background=True)
    try:
        assert app.handle("task.submit", payload).ids["run_id"] == first.ids["run_id"]
        assert app.backends["agy"].dispatch_count == 0
        resumed = app.handle("session.resume", {"conversation_id": first.ids["conversation_id"], "native": True})
        assert not resumed.ok and resumed.error["code"] == "CAPABILITY_UNSUPPORTED"
        continuation = app.handle("task.submit", {**payload, "idempotency_key": "two", "text": "next",
                                                  "conversation_id": first.ids["conversation_id"]})
        assert not continuation.ok and continuation.error["code"] == "CAPABILITY_UNSUPPORTED"
    finally:
        app.close()


def test_standard_control_has_native_reference_artifact_and_one_turn(agy_config, isolated_env):
    app = Application.from_paths(agy_config, isolated_env / "control-state", background=True)
    try:
        task_id, run_id = control_submit(app, "readonly input")
        final = wait_run(app, run_id, lambda item: item["status"] == "SUCCEEDED")
        task = call(app, "control.get", {"kind": "Task", "id": task_id})
        assert call(app, "control.content", {"artifact_id": task["result_artifact_ids"][0]})["text"] == "fixture response"
        action = action_for(app, run_id)
        reserved = call(app, "control.get", {"kind": "Reservation", "id": action["extensions"]["asterun.reservation_id"]})
        assert reserved["actual"] == [{"meter": "turns", "amount": 1, "billing_pool_ref": None}]
        assert final["completion_evidence_ids"]
    finally:
        app.close()


def test_explicit_missing_binary_does_not_fall_back(isolated_env, monkeypatch):
    good = isolated_env / "good"
    good.write_text("#!/bin/sh\nexit 0\n")
    good.chmod(0o700)
    monkeypatch.setenv("ANTIGRAVITY_BIN", str(good))
    assert discover_antigravity_bin(str(isolated_env / "missing")) is None
    assert discover_antigravity_bin("") is None
    monkeypatch.setenv("ANTIGRAVITY_BIN", "")
    assert discover_antigravity_bin() is None


def test_standard_install_location_discovery(isolated_env, monkeypatch):
    p = Path(os.environ["HOME"]) / ".local/bin/agy"
    p.parent.mkdir(parents=True)
    p.write_text("#!/bin/sh\nexit 0\n")
    p.chmod(0o700)
    assert discover_antigravity_bin() == p.resolve()


def test_preflight_rejects_workspace_extensions_without_dispatch(agy_config, isolated_env, workspace_root):
    app = Application.from_paths(agy_config, isolated_env / "state", background=True)
    (workspace_root / ".agents").mkdir()
    try:
        result = app.handle("task.submit", {"workspace": "demo", "backend": "agy", "text": "input"})
        assert result.ok
        final = wait(app, result.ids["task_id"], "failed")
        receipt = final["run"]["native"][DISPATCH_RECEIPT_KEY]
        assert receipt["prompt_attempted"] is False and receipt["delivery"] == "not_sent"
        assert app.backends["agy"]._runtime is None
    finally:
        app.close()


def test_control_preflight_failure_settles_zero_turns(agy_config, isolated_env, workspace_root):
    app = Application.from_paths(agy_config, isolated_env / "control-state", background=True)
    (workspace_root / "AGENTS.md").write_text("untrusted context")
    try:
        _, run_id = control_submit(app, "input")
        wait_run(app, run_id, lambda run: run["status"] == "FAILED")
        action = action_for(app, run_id)
        reservation = call(app, "control.get", {"kind": "Reservation", "id": action["extensions"]["asterun.reservation_id"]})
        assert reservation["actual"] == [{"meter": "turns", "amount": 0, "billing_pool_ref": None}]
    finally:
        app.close()


@pytest.mark.parametrize("mode,expected", [("soft_deny", "FAILED"), ("error", "FAILED"),
                                         ("tool_state_error", "FAILED"), ("eof", "RECONCILING")])
def test_control_native_failure_or_uncertainty_never_succeeds(agy_config, isolated_env, mode, expected):
    binary = Path(json.loads(agy_config.read_text())["backends"]["agy"]["bin"])
    binary.write_text(binary.read_text().replace('MODE = os.environ.get("AGY_FIXTURE_MODE", "happy")', f'MODE = {mode!r}'))
    app = Application.from_paths(agy_config, isolated_env / "control-state", background=True)
    try:
        _, run_id = control_submit(app, "input")
        wait_run(app, run_id, lambda run: run["status"] == expected)
        for _ in range(5):
            app.poll()
        assert app.backends["agy"].dispatch_count == 1
        assert call(app, "control.get", {"kind": "Run", "id": run_id})["status"] == expected
    finally:
        app.close()


@pytest.mark.parametrize("change", [{"task_id": "other"}, {"run_id": "other"}, {"prompt_attempted": True},
                                    {"delivery": "may_have_been_sent"}, {"stage": "prompt"}, {"failure_type": None}])
def test_unbound_or_attempted_receipt_cannot_release_budget(agy_config, isolated_env, change):
    app = Application.from_paths(agy_config, isolated_env / "state", background=True)
    receipt = {"task_id": "task_test", "run_id": "run_test", "stage": "spawn", "prompt_attempted": False,
               "delivery": "not_sent", "failure_type": "OSError"}
    run = SimpleNamespace(backend=BackendId("agy"), status=RunStatus.FAILED, terminated=True,
                          task_id=TaskId("task_test"), id=RunId("run_test"), native={DISPATCH_RECEIPT_KEY: receipt})
    try:
        assert app.control._confirmed_prompt_not_sent(run)
        receipt.update(change)
        assert not app.control._confirmed_prompt_not_sent(run)
    finally:
        app.close()
