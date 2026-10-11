import os
import sys
from copy import deepcopy
from threading import Event

import pytest

from asterun.application import Application
from asterun.backends.grok_code import (
    RECEIPT_KEY,
    host_boot_id,
    host_machine_id,
    process_provably_gone,
    reconcile_lost_process,
    run_code,
)
from asterun.contracts import (
    SLOT_HELD_NATIVE_KEY,
    SLOT_RELEASED_NATIVE_KEY,
    TERMINAL_RUN_STATUSES,
    OperationStatus,
    RunStatus,
)
from asterun.ids import RunId, TaskId
from asterun.scheduler import Scheduler
from tests.conftest import write_config
from tests.test_grok_resume import options

MACHINE_ID = "machine-a"


def install_host(monkeypatch, root, boot, machine=MACHINE_ID):
    boot_path = root / "boot_id"
    machine_path = root / "machine-id"
    proc = root / "proc"
    proc.mkdir(exist_ok=True)
    boot_path.write_text(boot)
    machine_path.write_text(machine)
    monkeypatch.setattr("asterun.backends.grok_code._BOOT_ID_PATH", boot_path)
    monkeypatch.setattr("asterun.backends.grok_code._MACHINE_ID_PATH", machine_path)
    monkeypatch.setattr("asterun.backends.grok_code._PROC_ROOT", proc)
    return boot_path, proc, machine_path


def write_leader(proc, pid=123, ticks=5555):
    directory = proc / str(pid)
    directory.mkdir(parents=True, exist_ok=True)
    # proc(5) field 22 is the 20th field after the last ')'.
    (directory / "stat").write_text(
        f"{pid} (grok x) S 1 {pid} {pid} 0 -1 4194560 1 0 0 0 0 0 0 0 20 0 1 0 {ticks} 0\n"
    )


def patch_killpg(monkeypatch, error):
    def killpg(pgid, sig):
        if error is not None:
            raise error
        return None

    monkeypatch.setattr("asterun.backends.grok_code.os.killpg", killpg)


def lost_native(task_id="task", run_id="run", session_id="s1"):
    return {
        "execution_profile": "workspace-code-v1",
        "transport": "headless",
        "session_id": session_id,
        RECEIPT_KEY: {
            "task_id": task_id,
            "run_id": run_id,
            "stage": "prompt",
            "prompt_attempted": True,
            "delivery": "may_have_been_sent",
            "failure_type": None,
            "process_exit_code": None,
        },
        "process": {"pid": 123, "pgid": 123, "boot_id": "old-boot", "machine_id": MACHINE_ID, "start_ticks": 5555},
    }


@pytest.mark.parametrize("case", [
    "boot_changed",
    "leader_alive",
    "leader_gone",
    "leader_gone_killpg_esrch",
    "machine_differs",
    "machine_unreadable",
    "boot_unreadable",
    "missing_machine_id",
    "missing_boot_id",
    "non_dict",
])
def test_process_provably_gone(monkeypatch, tmp_path, case):
    same_boot = case in {"leader_alive", "leader_gone", "leader_gone_killpg_esrch"}
    boot_path, proc, machine_path = install_host(
        monkeypatch, tmp_path, "old-boot" if same_boot else "new-boot")
    record = {"pid": 123, "pgid": 123, "boot_id": "old-boot", "machine_id": MACHINE_ID, "start_ticks": 5555}
    kill_error = ProcessLookupError
    expect = False
    if case == "boot_changed":
        kill_error = PermissionError
        expect = True
    elif case == "leader_alive":
        write_leader(proc)
        kill_error = None
    elif case == "leader_gone":
        kill_error = None
    elif case == "leader_gone_killpg_esrch":
        kill_error = ProcessLookupError
    elif case == "machine_differs":
        record["machine_id"] = "other-machine"
    elif case == "machine_unreadable":
        machine_path.unlink()
    elif case == "boot_unreadable":
        boot_path.unlink()
    elif case == "missing_machine_id":
        record.pop("machine_id")
    elif case == "missing_boot_id":
        record.pop("boot_id")
    elif case == "non_dict":
        record = None
    patch_killpg(monkeypatch, kill_error)
    assert process_provably_gone(record) is expect


def test_reconcile_lost_process_boot_change_fails_without_mutating_native(monkeypatch, tmp_path):
    install_host(monkeypatch, tmp_path, "new-boot")
    patch_killpg(monkeypatch, PermissionError)
    native = lost_native()
    original = deepcopy(native)
    result = reconcile_lost_process(native, task_id="task", run_id="run")
    assert result["status"] == "failed"
    assert result["error_code"] == "GROK_PROCESS_LOST"
    assert result["terminated"] is True
    assert native == original


def test_reconcile_lost_process_cancel_requested_cancels_without_mutating_native(monkeypatch, tmp_path):
    install_host(monkeypatch, tmp_path, "new-boot")
    patch_killpg(monkeypatch, PermissionError)
    native = lost_native()
    original = deepcopy(native)
    result = reconcile_lost_process(native, task_id="task", run_id="run", cancel_requested=True)
    assert result["status"] == "cancelled"
    assert result["error_code"] is None
    assert result["terminated"] is True
    assert native == original


@pytest.mark.parametrize("field,value", [
    ("task_id", "other"),
    ("run_id", "other"),
    ("stage", "spawn"),
    ("prompt_attempted", False),
    ("failure_type", "runtime_timeout"),
    ("process_exit_code", 1),
    ("execution_profile", "text-only-v1"),
    ("transport", "acp"),
    ("session_id", ""),
    ("process", None),
    ("alive", True),
])
def test_reconcile_lost_process_leaves_unproven_runs_unknown(monkeypatch, tmp_path, field, value):
    boot = "old-boot" if field == "alive" else "new-boot"
    _boot_path, proc, _machine_path = install_host(monkeypatch, tmp_path, boot)
    if field == "alive":
        write_leader(proc)
    patch_killpg(monkeypatch, ProcessLookupError)
    native = lost_native()
    if field == "alive":
        pass
    elif field in {"task_id", "run_id", "stage", "prompt_attempted", "failure_type", "process_exit_code"}:
        native[RECEIPT_KEY][field] = value
    elif field == "process":
        native.pop("process")
    else:
        native[field] = value
    assert reconcile_lost_process(native, task_id="task", run_id="run") is None


def park_pending(isolated_env, workspace_root, monkeypatch):
    boot_path, proc, _machine_path = install_host(monkeypatch, isolated_env, "old-boot")
    write_leader(proc)
    config = write_config(isolated_env / "cfg.json", workspace_root,
                          {"grok": options(isolated_env, monkeypatch)})
    state = isolated_env / "state"
    app = Application.from_paths(config, state)
    try:
        submitted = app.handle("task.submit", {"workspace": "demo", "backend": "grok", "text": "start"})
        assert submitted.ok, submitted.error
        run = app.store.get_run(RunId(submitted.ids["run_id"]))
        run.native = lost_native(submitted.ids["task_id"], submitted.ids["run_id"], run.native["session_id"])
        run.native.pop(SLOT_HELD_NATIVE_KEY, None)
        run.native.pop(SLOT_RELEASED_NATIVE_KEY, None)
        run.status = RunStatus.PENDING_RECONCILE
        run.error_code = "REMOTE_STATE_UNKNOWN"
        run.terminated = False
        app.store.save_run(run)
        operation = app.store.find_operation_for_run(run.id)
        operation.status = OperationStatus.UNKNOWN
        app.store.save_operation(operation)
        task_id = submitted.ids["task_id"]
        run_id = submitted.ids["run_id"]
    finally:
        app.close()
    return config, state, task_id, run_id, boot_path


def test_reopen_with_live_leader_stays_pending(isolated_env, workspace_root, monkeypatch):
    config, state, task_id, run_id, _boot_path = park_pending(isolated_env, workspace_root, monkeypatch)
    app = Application.from_paths(config, state)
    try:
        assert app.scheduler.snapshot()["inflight"] == {"grok": 1}
        run = app.store.get_run(RunId(run_id))
        assert run.status == RunStatus.PENDING_RECONCILE
        reconciled = app.handle("task.reconcile", {"task_id": task_id})
        assert reconciled.ok, reconciled.error
        assert reconciled.data["unknown_states"]
        assert not reconciled.data["reports"][0]["terminal"]
        run = app.store.get_run(RunId(run_id))
        assert run.status == RunStatus.PENDING_RECONCILE
        assert run.terminated is False
        assert app.scheduler.snapshot()["inflight"] == {"grok": 1}
    finally:
        app.close()


def test_reopen_after_boot_change_releases_slot(isolated_env, workspace_root, monkeypatch):
    config, state, _task_id, run_id, boot_path = park_pending(isolated_env, workspace_root, monkeypatch)
    boot_path.write_text("new-boot")
    app = Application.from_paths(config, state)
    try:
        run = app.store.get_run(RunId(run_id))
        assert run.status == RunStatus.FAILED
        assert run.error_code == "GROK_PROCESS_LOST"
        assert run.terminated is True
        operation = app.store.find_operation_for_run(RunId(run_id))
        # A failed run is recorded as a failed operation. The slot is still released.
        assert operation.status == OperationStatus.FAILED
        assert app.scheduler.snapshot()["inflight"] == {}
        second = app.handle("task.submit", {"workspace": "demo", "backend": "grok", "text": "continue"})
        assert second.ok, second.error
        assert second.data["queued"] is False
        assert app.store.get_run(RunId(second.ids["run_id"])).status == RunStatus.SUCCEEDED
    finally:
        app.close()


def test_cancel_after_boot_change_terminates_lost_process(isolated_env, workspace_root, monkeypatch):
    config, state, task_id, run_id, boot_path = park_pending(isolated_env, workspace_root, monkeypatch)
    app = Application.from_paths(config, state)
    try:
        assert app.scheduler.snapshot()["inflight"] == {"grok": 1}
        boot_path.write_text("new-boot")
        cancelled = app.handle("task.cancel", {"task_id": task_id})
        assert cancelled.ok, cancelled.error
        run = app.store.get_run(RunId(run_id))
        assert run.status == RunStatus.CANCELLED
        assert run.terminated is True
        assert app.scheduler.snapshot()["inflight"] == {}
    finally:
        app.close()


def test_cancel_leaves_live_leader_nonterminal(isolated_env, workspace_root, monkeypatch):
    config, state, task_id, run_id, _boot_path = park_pending(isolated_env, workspace_root, monkeypatch)
    app = Application.from_paths(config, state)
    try:
        assert app.scheduler.snapshot()["inflight"] == {"grok": 1}
        cancelled = app.handle("task.cancel", {"task_id": task_id})
        run = app.store.get_run(RunId(run_id))
        assert (not cancelled.ok) or (not run.terminated)
        assert run.status not in TERMINAL_RUN_STATUSES
        assert run.terminated is False
        assert app.scheduler.snapshot()["inflight"] == {"grok": 1}
    finally:
        app.close()


def test_dispatch_persists_process_identity(isolated_env, workspace_root, monkeypatch):
    boot_path, _proc, _machine_path = install_host(monkeypatch, isolated_env, "live-boot")
    config = write_config(isolated_env / "cfg.json", workspace_root,
                          {"grok": options(isolated_env, monkeypatch)})
    app = Application.from_paths(config, isolated_env / "state")
    try:
        submitted = app.handle("task.submit", {"workspace": "demo", "backend": "grok", "text": "start"})
        assert submitted.ok, submitted.error
        process = app.store.get_run(RunId(submitted.ids["run_id"])).native["process"]
        assert type(process["pid"]) is int
        assert type(process["pgid"]) is int
        assert process["pid"] == process["pgid"]
        assert process["boot_id"] == boot_path.read_text()
    finally:
        app.close()


def test_reconcile_lost_process_spawn_before_prompt_fails_without_mutating_native(monkeypatch, tmp_path):
    install_host(monkeypatch, tmp_path, "new-boot")
    native = lost_native()
    native[RECEIPT_KEY]["stage"] = "spawn"
    native[RECEIPT_KEY]["prompt_attempted"] = False
    original = deepcopy(native)
    result = reconcile_lost_process(native, task_id="task", run_id="run")
    assert result["status"] == "failed"
    assert result["error_code"] == "GROK_PROCESS_LOST"
    assert result["terminated"] is True
    assert native == original


def test_restore_does_not_start_queued_work_before_live_slot(isolated_env, workspace_root, monkeypatch):
    other = isolated_env / "other"
    other.mkdir()
    install_host(monkeypatch, isolated_env, "new-boot")
    config = write_config(
        isolated_env / "cfg.json",
        workspace_root,
        {"grok": options(isolated_env, monkeypatch)},
        extra={"workspaces": {
            "demo": {"root": str(workspace_root), "allow_non_git": True},
            "other": {"root": str(other), "allow_non_git": True},
        }},
    )
    app = Application.from_paths(config, isolated_env / "state")
    try:
        submitted = [
            app.handle("task.submit", {"workspace": name, "backend": "grok", "text": "start"})
            for name in ("demo", "other", "demo")
        ]
        assert all(item.ok for item in submitted), [item.error for item in submitted]
        ids = []
        for index, item in enumerate(submitted):
            run = app.store.get_run(RunId(item.ids["run_id"]))
            ids.append(TaskId(item.ids["task_id"]))
            run.terminated = False
            if index == 0:
                run.status = RunStatus.QUEUED
            else:
                run.status = RunStatus.PENDING_RECONCILE
                run.native = lost_native(item.ids["task_id"], item.ids["run_id"], run.native["session_id"])
                run.native["process"]["machine_id"] = host_machine_id()
                run.native["process"]["boot_id"] = "old-boot" if index == 1 else host_boot_id()
                run.native[SLOT_HELD_NATIVE_KEY] = True
            app.store.save_run(run)
            operation = app.store.find_operation_for_run(run.id)
            operation.status = OperationStatus.INTENDED if index == 0 else OperationStatus.UNKNOWN
            app.store.save_operation(operation)
        monkeypatch.setattr(app.store, "list_task_ids", lambda: ids)
        app.scheduler = Scheduler(app.config.scheduler)
        app._restore_scheduler()
        queued = app.store.get_run(RunId(submitted[0].ids["run_id"]))
        lost = app.store.get_run(RunId(submitted[1].ids["run_id"]))
        live = app.store.get_run(RunId(submitted[2].ids["run_id"]))
        assert queued.status == RunStatus.QUEUED
        assert lost.status == RunStatus.FAILED
        assert lost.error_code == "GROK_PROCESS_LOST"
        assert live.status == RunStatus.PENDING_RECONCILE
        assert app.scheduler.snapshot()["inflight"] == {"grok": 1}
        assert app.scheduler.inflight_runs == {submitted[2].ids["run_id"]}
    finally:
        app.close()


def test_dispatching_snapshot_has_host_identity_before_pid(monkeypatch, tmp_path):
    boot_path, _proc, machine_path = install_host(monkeypatch, tmp_path, "live-boot")
    script = tmp_path / "end_turn.py"
    script.write_text(
        "import json, sys\n"
        "print(json.dumps({'type': 'end', 'sessionId': sys.argv[1], 'stopReason': 'end_turn'}))\n"
    )
    snapshots = []

    def publish(snapshot):
        # Freeze the acknowledgement. Later pid fields are written into the same dict.
        snapshots.append(deepcopy(snapshot))

    result = run_code(
        command=[sys.executable, str(script), "s1"],
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"},
        cwd=tmp_path,
        session_id="s1",
        task_id="task",
        run_id="run",
        timeout_seconds=5,
        publish=publish,
        stopping=Event(),
    )
    assert result["status"] == "succeeded", result.get("error_code")
    dispatching = next(item for item in snapshots if item["status"] == "dispatching")
    process = dispatching["native"]["process"]
    assert process["boot_id"] == boot_path.read_text()
    assert process["machine_id"] == machine_path.read_text()
    assert "pid" not in process
