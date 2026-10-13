"""Same-boot proof that a grok-code process is gone in this PID namespace."""

import os
import select
import signal
import subprocess
import sys
import time
from copy import deepcopy
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest

import asterun.backends.grok_code as grok_code
from asterun.application import Application
from asterun.backends.grok_code import reconcile_lost_process, run_code, same_boot_liveness
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
        "launch_marker": "run:s1",
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
    # A live process has an environ file; a missing one is a mid-scan vanish.
    write_stat(proc, pid=123, ticks=9999, pgrp=1)
    (proc / "123" / "environ").write_bytes(b"PATH=/x\0")
    result = reconcile_lost_process(same_native(), task_id="task", run_id="run")
    assert result["status"] == "failed"
    assert result["error_code"] == "GROK_PROCESS_LOST"
    assert result["events"][0]["reason"] == "host_process_lost_same_boot"


@pytest.mark.parametrize("ticks", [5555, 9000])
def test_live_group_member_blocks_same_boot_reclaim(monkeypatch, tmp_path, ticks):
    _boot, proc, _machine = install_same(monkeypatch, tmp_path)
    write_stat(proc, pid=456, ticks=ticks, pgrp=123)
    assert reconcile_lost_process(same_native(), task_id="task", run_id="run") is None


def _raise_on_pid(monkeypatch, pid, error):
    real = grok_code._proc_stat

    def wrapped(entry):
        if entry.name == str(pid):
            raise error
        return real(entry)

    monkeypatch.setattr(grok_code, "_proc_stat", wrapped)


def test_setsid_child_with_launch_marker_stays_alive(monkeypatch, tmp_path):
    _boot, proc, _machine = install_same(monkeypatch, tmp_path)
    write_stat(proc, pid=789, ticks=100, pgrp=789)
    (proc / "789" / "environ").write_bytes(b"PATH=/x\0ASTERUN_LAUNCH_MARKER=run:s1\0")
    native = same_native()
    assert reconcile_lost_process(native, task_id="task", run_id="run") is None
    assert same_boot_liveness(native["process"]) == "alive"


def test_unreadable_proc_entry_is_unknown(monkeypatch, tmp_path):
    _boot, proc, _machine = install_same(monkeypatch, tmp_path)
    write_stat(proc, pid=456, ticks=6000, pgrp=123)
    _raise_on_pid(monkeypatch, "456", PermissionError("unreadable"))
    native = same_native()
    assert same_boot_liveness(native["process"]) == "unknown"
    assert reconcile_lost_process(native, task_id="task", run_id="run") is None


def test_vanished_proc_entry_is_still_absent(monkeypatch, tmp_path):
    _boot, proc, _machine = install_same(monkeypatch, tmp_path)
    write_stat(proc, pid=456, ticks=6000, pgrp=123)
    real = grok_code._proc_stat

    def wrapped(entry):
        if entry.name == "456":
            # Drop it so the rescan no longer sees a mid-scan vanish.
            stat_path = entry / "stat"
            if stat_path.exists():
                stat_path.unlink()
            if entry.is_dir():
                entry.rmdir()
            raise FileNotFoundError("gone")
        return real(entry)

    monkeypatch.setattr(grok_code, "_proc_stat", wrapped)
    native = same_native()
    assert same_boot_liveness(native["process"]) == "absent"
    result = reconcile_lost_process(native, task_id="task", run_id="run")
    assert result["status"] == "failed"
    assert result["events"][0]["reason"] == "host_process_lost_same_boot"


def test_child_forked_after_enumeration_is_alive_on_rescan(monkeypatch, tmp_path):
    _boot, proc, _machine = install_same(monkeypatch, tmp_path)
    write_stat(proc, pid=456, ticks=1, pgrp=456)
    real = grok_code._proc_stat
    calls = {"n": 0}

    def wrapped(entry):
        if entry.name == "456":
            calls["n"] += 1
            if calls["n"] == 1:
                write_stat(proc, pid=789, ticks=100, pgrp=789)
                (proc / "789" / "environ").write_bytes(b"PATH=/x\0ASTERUN_LAUNCH_MARKER=run:s1\0")
            raise FileNotFoundError("gone")
        return real(entry)

    monkeypatch.setattr(grok_code, "_proc_stat", wrapped)
    assert same_boot_liveness(same_native()["process"]) == "alive"
    assert calls["n"] >= 1


def test_endless_vanishing_scan_is_unknown(monkeypatch, tmp_path):
    _boot, proc, _machine = install_same(monkeypatch, tmp_path)
    write_stat(proc, pid=456, ticks=6000, pgrp=456)
    real = grok_code._proc_stat
    calls = {"n": 0}

    def wrapped(entry):
        if entry.name == "456":
            calls["n"] += 1
            raise FileNotFoundError("gone")
        return real(entry)

    monkeypatch.setattr(grok_code, "_proc_stat", wrapped)
    native = same_native()
    assert same_boot_liveness(native["process"]) == "unknown"
    assert calls["n"] == grok_code._LIVENESS_SCANS
    assert reconcile_lost_process(native, task_id="task", run_id="run") is None


def test_live_leader_unreadable_environ_is_alive(monkeypatch, tmp_path):
    _boot, proc, _machine = install_same(monkeypatch, tmp_path)
    write_leader(proc, pid=123, ticks=5555)

    def unreadable(_entry):
        raise PermissionError("environ")

    monkeypatch.setattr(grok_code, "_proc_environ", unreadable)
    assert same_boot_liveness(same_native()["process"]) == "alive"


def test_foreign_uid_unreadable_environ_is_absent(monkeypatch, tmp_path):
    _boot, proc, _machine = install_same(monkeypatch, tmp_path)
    write_stat(proc, pid=999, ticks=100, pgrp=999)
    real_uid = grok_code._proc_uid
    real_environ = grok_code._proc_environ
    environ_pids = []

    def uid(entry):
        if entry.name == "999":
            return os.getuid() + 1
        return real_uid(entry)

    def environ(entry):
        environ_pids.append(entry.name)
        if entry.name == "999":
            raise PermissionError("foreign environ")
        return real_environ(entry)

    monkeypatch.setattr(grok_code, "_proc_uid", uid)
    monkeypatch.setattr(grok_code, "_proc_environ", environ)
    native = same_native()
    assert same_boot_liveness(native["process"]) == "absent"
    assert "999" not in environ_pids
    result = reconcile_lost_process(native, task_id="task", run_id="run")
    assert result["status"] == "failed"
    assert result["events"][0]["reason"] == "host_process_lost_same_boot"


def test_same_uid_unreadable_environ_on_unrelated_process_is_unknown(monkeypatch, tmp_path):
    _boot, proc, _machine = install_same(monkeypatch, tmp_path)
    write_stat(proc, pid=456, ticks=100, pgrp=456)
    real = grok_code._proc_environ

    def environ(entry):
        if entry.name == "456":
            raise PermissionError("unreadable")
        return real(entry)

    monkeypatch.setattr(grok_code, "_proc_environ", environ)
    native = same_native()
    assert same_boot_liveness(native["process"]) == "unknown"
    assert reconcile_lost_process(native, task_id="task", run_id="run") is None


@pytest.mark.parametrize("case", ["different_ns", "missing_ns", "missing_start_ticks", "missing_marker"])
def test_incomplete_same_boot_identity_is_not_proof(monkeypatch, tmp_path, case):
    install_same(monkeypatch, tmp_path)
    native = same_native()
    if case == "different_ns":
        native["process"]["pid_ns"] = "pid:[1]"
    elif case == "missing_ns":
        native["process"].pop("pid_ns")
    elif case == "missing_marker":
        native["process"].pop("launch_marker")
    else:
        native["process"].pop("start_ticks")
    assert reconcile_lost_process(native, task_id="task", run_id="run") is None
    assert same_boot_liveness(native["process"]) == "unknown"


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
    assert process["launch_marker"] == "run:s1"
    assert process["boot_id"] == boot_path.read_text()
    assert process["machine_id"] == machine_path.read_text()
    assert "pid" not in process
    assert result["native"]["process"]["pid_ns"] == NS
    assert result["native"]["process"]["launch_marker"] == "run:s1"


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
        run.native["process"]["launch_marker"] = "run:s1"
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


def _count_scans(monkeypatch):
    scans = {"n": 0}
    real = grok_code._scan_once

    def wrapped(*args, **kwargs):
        scans["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(grok_code, "_scan_once", wrapped)
    return scans


def test_zombie_same_uid_entry_is_unknown(monkeypatch, tmp_path):
    _boot, proc, _machine = install_same(monkeypatch, tmp_path)
    directory = proc / "600"
    directory.mkdir()
    (directory / "stat").write_text(
        "600 (p) Z 1 600 600 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 7000 0\n"
    )
    scans = _count_scans(monkeypatch)
    native = same_native()
    assert same_boot_liveness(native["process"]) == "unknown"
    assert scans["n"] == grok_code._LIVENESS_SCANS
    assert reconcile_lost_process(native, task_id="task", run_id="run") is None


def test_zombie_parent_fork_after_enumeration_is_alive_on_rescan(monkeypatch, tmp_path):
    _boot, proc, _machine = install_same(monkeypatch, tmp_path)
    directory = proc / "600"
    directory.mkdir()
    (directory / "stat").write_text(
        "600 (p) Z 1 600 600 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 7000 0\n"
    )
    real = grok_code._proc_stat
    seen = {"n": 0}

    def wrapped(entry):
        if entry.name == "600":
            seen["n"] += 1
            if seen["n"] == 1:
                child = proc / "601"
                child.mkdir()
                (child / "stat").write_text(
                    "601 (c) S 1 601 601 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 7001 0\n"
                )
                (child / "environ").write_bytes(b"PATH=/x\0ASTERUN_LAUNCH_MARKER=run:s1\0")
                return ("Z", 600, 7000)
        return real(entry)

    monkeypatch.setattr(grok_code, "_proc_stat", wrapped)
    assert same_boot_liveness(same_native()["process"]) == "alive"
    assert seen["n"] >= 1


def test_empty_environ_is_unknown_and_unmarked_environ_is_absent(monkeypatch, tmp_path):
    _boot, proc, _machine = install_same(monkeypatch, tmp_path)
    write_stat(proc, pid=456, ticks=100, pgrp=456)
    (proc / "456" / "environ").write_bytes(b"")
    scans = _count_scans(monkeypatch)
    native = same_native()
    assert same_boot_liveness(native["process"]) == "unknown"
    assert scans["n"] == grok_code._LIVENESS_SCANS
    (proc / "456" / "environ").write_bytes(b"PATH=/x\0")
    assert same_boot_liveness(native["process"]) == "absent"


def test_zombie_leader_matching_pid_and_ticks_is_unknown(monkeypatch, tmp_path):
    _boot, proc, _machine = install_same(monkeypatch, tmp_path)
    directory = proc / "123"
    directory.mkdir()
    (directory / "stat").write_text(
        "123 (p) Z 1 123 123 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 5555 0\n"
    )
    scans = _count_scans(monkeypatch)
    assert same_boot_liveness(same_native()["process"]) == "unknown"
    assert scans["n"] == grok_code._LIVENESS_SCANS


# Child leaves the parent's group, prints its pid, then sleeps. Parent exits at once.
_ZOMBIE_PARENT_CODE = "import os,time\np=os.fork()\nif p==0:\n os.setsid(); print(os.getpid(), flush=True); time.sleep(30)\nelse:\n os._exit(0)\n"


def _stat_state(pid):
    text = Path(f"/proc/{pid}/stat").read_text()
    return text.rsplit(")", 1)[1].split()[0]


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or not Path("/proc/self/ns/pid").exists(),
    reason="linux pid namespace required",
)
def test_real_zombie_parent_setsid_child_stays_alive_until_reaped():
    marker = f"realtest:{uuid4()}"
    proc = None
    child_pid = None
    try:
        proc = subprocess.Popen(
            [sys.executable, "-c", _ZOMBIE_PARENT_CODE],
            env={**os.environ, "ASTERUN_LAUNCH_MARKER": marker},
            start_new_session=True,
            stdout=subprocess.PIPE,
        )
        deadline = time.monotonic() + 5
        state = ""
        while time.monotonic() < deadline:
            try:
                state = _stat_state(proc.pid)
            except (OSError, IndexError, ValueError):
                state = ""
            if state == "Z":
                break
            time.sleep(0.02)
        assert state == "Z"
        ticks = grok_code.process_start_ticks(proc.pid)
        assert type(ticks) is int
        record = {
            "pid": proc.pid,
            "pgid": proc.pid,
            "start_ticks": ticks,
            "boot_id": grok_code.host_boot_id(),
            "machine_id": grok_code.host_machine_id(),
            "pid_ns": grok_code.host_pid_namespace(),
            "launch_marker": marker,
        }
        stdout = proc.stdout
        assert stdout is not None
        assert select.select([stdout], [], [], 5)[0]
        child_pid = int(stdout.readline())
        stdout.close()
        assert same_boot_liveness(record) == "alive"
        os.kill(child_pid, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and Path(f"/proc/{child_pid}").exists():
            time.sleep(0.02)
        assert not Path(f"/proc/{child_pid}").exists()
        assert _stat_state(proc.pid) == "Z"
        assert same_boot_liveness(record) == "unknown"
        proc.wait(timeout=5)
        needle = f"ASTERUN_LAUNCH_MARKER={marker}".encode()
        _scan_state, unstable = grok_code._scan_once(
            record["pid"], record["pgid"], record["start_ticks"], needle, os.getuid())
        # A single stable pass must agree; separate calls may see unrelated transient processes.
        # After reaping, the launch must never look alive. "absent" is expected on a quiet
        # host; "unknown" is the fail-closed answer when unrelated same-uid entries are
        # unreadable, zombie or transient (e.g. CI runners).
        assert _scan_state in ("absent", "unknown")
        assert same_boot_liveness(record) in ("absent", "unknown")
    finally:
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except OSError:
                pass
        if proc is not None:
            if proc.stdout is not None and not proc.stdout.closed:
                proc.stdout.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
