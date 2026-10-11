from copy import deepcopy

import pytest

from asterun.application import Application
from asterun.backends.grok_code import RECEIPT_KEY, process_provably_gone, reconcile_lost_process
from asterun.contracts import (
    SLOT_HELD_NATIVE_KEY,
    SLOT_RELEASED_NATIVE_KEY,
    TERMINAL_RUN_STATUSES,
    OperationStatus,
    RunStatus,
)
from asterun.ids import RunId
from tests.conftest import write_config
from tests.test_grok_resume import options


def install_host(monkeypatch, root, boot):
    boot_path = root / "boot_id"
    proc = root / "proc"
    proc.mkdir(exist_ok=True)
    boot_path.write_text(boot)
    monkeypatch.setattr("asterun.backends.grok_code._BOOT_ID_PATH", boot_path)
    monkeypatch.setattr("asterun.backends.grok_code._PROC_ROOT", proc)
    return boot_path, proc


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
        "process": {"pid": 123, "pgid": 123, "boot_id": "old-boot", "start_ticks": 5555},
    }


@pytest.mark.parametrize("case", [
    "boot_changed",
    "leader_alive",
    "stat_missing",
    "ticks_differ",
    "killpg_succeeds",
    "permission",
    "boot_missing",
    "non_dict",
    "missing_pid",
    "bool_pid",
    "pid_one",
    "missing_boot_id",
])
def test_process_provably_gone(monkeypatch, tmp_path, case):
    boot = "new-boot" if case == "boot_changed" else "old-boot"
    boot_path, proc = install_host(monkeypatch, tmp_path, boot)
    record = {"pid": 123, "pgid": 123, "boot_id": "old-boot", "start_ticks": 5555}
    kill_error = ProcessLookupError
    expect = False
    if case == "boot_changed":
        # A different boot is enough even when the group cannot be signaled.
        kill_error = PermissionError
        expect = True
    elif case == "leader_alive":
        write_leader(proc)
        expect = False
    elif case == "stat_missing":
        expect = True
    elif case == "ticks_differ":
        write_leader(proc, ticks=1)
        expect = True
    elif case == "killpg_succeeds":
        write_leader(proc, ticks=1)
        kill_error = None
        expect = False
    elif case == "permission":
        kill_error = PermissionError
        expect = False
    elif case == "boot_missing":
        boot_path.unlink()
        expect = False
    elif case == "non_dict":
        record = None
        boot_path.write_text("new-boot")
    elif case == "missing_pid":
        record.pop("pid")
        boot_path.write_text("new-boot")
    elif case == "bool_pid":
        record["pid"] = True
        boot_path.write_text("new-boot")
    elif case == "pid_one":
        record["pid"] = 1
        boot_path.write_text("new-boot")
    elif case == "missing_boot_id":
        record.pop("boot_id")
        boot_path.write_text("new-boot")
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
    _boot_path, proc = install_host(monkeypatch, tmp_path, boot)
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
    boot_path, proc = install_host(monkeypatch, isolated_env, "old-boot")
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
    boot_path, _proc = install_host(monkeypatch, isolated_env, "live-boot")
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
