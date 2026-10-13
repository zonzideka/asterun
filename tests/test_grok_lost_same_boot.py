"""Same-boot proof that a grok-code process is gone in this PID namespace."""

import os
import sys
from copy import deepcopy
from threading import Event

import pytest

from asterun.application import Application
from asterun.backends.grok_code import reconcile_lost_process, run_code
from asterun.contracts import (
    SLOT_HELD_NATIVE_KEY,
    SLOT_RELEASED_NATIVE_KEY,
    OperationStatus,
    RunStatus,
)
from asterun.ids import RunId
from tests.conftest import write_config
from tests.test_grok_lost_process import MACHINE_ID, install_host, lost_native, write_leader
from tests.test_grok_resume import options

NS = "pid:[4026531836]"


def write_pid_ns(proc, ns=NS):
    directory = proc / "self" / "ns"
    directory.mkdir(parents=True, exist_ok=True)
    link = directory / "pid"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(ns)


def write_stat(proc, pid, ticks, pgrp):
    """proc(5) stat: index 2 after ')' is pgrp, index 19 is starttime."""
    directory = proc / str(pid)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "stat").write_text(
        f"{pid} (cmd) S 1 {pgrp} {pgrp} 0 -1 4194560 1 0 0 0 0 0 0 0 20 0 1 0 {ticks} 0\n"
    )


def install_same(monkeypatch, root):
    boot_path, proc, machine_path = install_host(monkeypatch, root, "old-boot")
    write_pid_ns(proc)
    return boot_path, proc, machine_path


def same_native(**updates):
    native = lost_native()
    native["process"] = {
        "pid": 123,
        "pgid": 123,
        "boot_id": "old-boot",
        "machine_id": MACHINE_ID,
        "start_ticks": 5555,
        "pid_ns": NS,
    }
    native["process"].update(updates)
    return native


def test_same_boot_missing_pid_reclaims_and_cancel_cancels(monkeypatch, tmp_path):
    install_same(monkeypatch, tmp_path)
    native = same_native()
    original = deepcopy(native)
    result = reconcile_lost_process(native, task_id="task", run_id="run")
    assert result["status"] == "failed"
    assert result["error_code"] == "GROK_PROCESS_LOST"
    assert result["terminated"] is True
    assert result["events"] == [
        {"type": "reconciled", "re_dispatched": False, "reason": "host_process_lost_same_boot"}
    ]
    assert result["native"]["recorded_terminal_reconciliation"]["source"] == "host_process_lost_same_boot"
    assert native == original

    cancelled_native = same_native()
    cancelled = reconcile_lost_process(
        cancelled_native, task_id="task", run_id="run", cancel_requested=True)
    assert cancelled["status"] == "cancelled"
    assert cancelled["error_code"] is None
    assert cancelled["terminated"] is True
    assert cancelled["events"][0]["reason"] == "host_process_lost_same_boot"
    assert cancelled["native"]["recorded_terminal_reconciliation"]["source"] == "host_process_lost_same_boot"
    assert cancelled_native["process"]["pid_ns"] == NS


def test_same_boot_live_pid_stays_unknown(monkeypatch, tmp_path):
    _boot, proc, _machine = install_same(monkeypatch, tmp_path)
    write_leader(proc, pid=123, ticks=5555)
    assert reconcile_lost_process(same_native(), task_id="task", run_id="run") is None


def test_reused_pid_without_group_member_is_reclaimed(monkeypatch, tmp_path):
    _boot, proc, _machine = install_same(monkeypatch, tmp_path)
    # Different start ticks, and not a member of the recorded group (pgrp != pgid).
    write_stat(proc, pid=123, ticks=9999, pgrp=1)
    result = reconcile_lost_process(same_native(), task_id="task", run_id="run")
    assert result["status"] == "failed"
    assert result["error_code"] == "GROK_PROCESS_LOST"
    assert result["events"][0]["reason"] == "host_process_lost_same_boot"


@pytest.mark.parametrize("ticks", [5555, 9000])
def test_live_group_member_blocks_same_boot_reclaim(monkeypatch, tmp_path, ticks):
    _boot, proc, _machine = install_same(monkeypatch, tmp_path)
    write_stat(proc, pid=456, ticks=ticks, pgrp=123)
    assert reconcile_lost_process(same_native(), task_id="task", run_id="run") is None


@pytest.mark.parametrize("case", ["different_ns", "missing_ns", "missing_start_ticks"])
def test_incomplete_same_boot_identity_is_not_proof(monkeypatch, tmp_path, case):
    install_same(monkeypatch, tmp_path)
    native = same_native()
    if case == "different_ns":
        native["process"]["pid_ns"] = "pid:[1]"
    elif case == "missing_ns":
        native["process"].pop("pid_ns")
    else:
        native["process"].pop("start_ticks")
    assert reconcile_lost_process(native, task_id="task", run_id="run") is None


def test_unlistable_proc_root_is_not_proof(monkeypatch, tmp_path):
    _boot, proc, _machine = install_same(monkeypatch, tmp_path)
    # Search permission keeps readlink working; without read, listing fails.
    proc.chmod(0o111)
    try:
        assert reconcile_lost_process(same_native(), task_id="task", run_id="run") is None
    finally:
        proc.chmod(0o755)


def test_boot_change_keeps_host_process_lost_reason(monkeypatch, tmp_path):
    boot_path, proc, machine_path = install_host(monkeypatch, tmp_path, "new-boot")
    write_pid_ns(proc)
    native = same_native()
    result = reconcile_lost_process(native, task_id="task", run_id="run")
    assert result["status"] == "failed"
    assert result["error_code"] == "GROK_PROCESS_LOST"
    assert result["events"][0]["reason"] == "host_process_lost"
    assert result["native"]["recorded_terminal_reconciliation"]["source"] == "host_process_lost"
    assert boot_path.read_text() == "new-boot"
    assert machine_path.read_text() == MACHINE_ID


def test_run_code_records_pid_ns_before_spawn(monkeypatch, tmp_path):
    boot_path, proc, machine_path = install_host(monkeypatch, tmp_path, "live-boot")
    write_pid_ns(proc)
    script = tmp_path / "end_turn.py"
    script.write_text(
        "import json, sys\n"
        "print(json.dumps({'type': 'end', 'sessionId': sys.argv[1], 'stopReason': 'end_turn'}))\n"
    )
    snapshots = []

    def publish(snapshot):
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
    assert process["pid_ns"] == NS
    assert process["boot_id"] == boot_path.read_text()
    assert process["machine_id"] == machine_path.read_text()
    assert "pid" not in process
    assert result["native"]["process"]["pid_ns"] == NS


def test_reopen_same_boot_absent_process_releases_slot(isolated_env, workspace_root, monkeypatch):
    _boot_path, proc, _machine_path = install_same(monkeypatch, isolated_env)
    config = write_config(isolated_env / "cfg.json", workspace_root,
                          {"grok": options(isolated_env, monkeypatch)})
    state = isolated_env / "state"
    app = Application.from_paths(config, state)
    try:
        submitted = app.handle("task.submit", {"workspace": "demo", "backend": "grok", "text": "start"})
        assert submitted.ok, submitted.error
        run = app.store.get_run(RunId(submitted.ids["run_id"]))
        run.native = lost_native(submitted.ids["task_id"], submitted.ids["run_id"], run.native["session_id"])
        run.native["process"]["pid_ns"] = NS
        run.native["process"]["boot_id"] = "old-boot"
        run.native.pop(SLOT_HELD_NATIVE_KEY, None)
        run.native.pop(SLOT_RELEASED_NATIVE_KEY, None)
        run.status = RunStatus.PENDING_RECONCILE
        run.error_code = "REMOTE_STATE_UNKNOWN"
        run.terminated = False
        app.store.save_run(run)
        operation = app.store.find_operation_for_run(run.id)
        operation.status = OperationStatus.UNKNOWN
        app.store.save_operation(operation)
        run_id = submitted.ids["run_id"]
    finally:
        app.close()
    assert not (proc / "123").exists()
    app = Application.from_paths(config, state)
    try:
        run = app.store.get_run(RunId(run_id))
        assert run.status == RunStatus.FAILED
        assert run.error_code == "GROK_PROCESS_LOST"
        assert run.terminated is True
        assert run.native["recorded_terminal_reconciliation"]["source"] == "host_process_lost_same_boot"
        assert app.scheduler.snapshot()["inflight"] == {}
    finally:
        app.close()
