from __future__ import annotations

import json
from contextlib import closing
import os
from pathlib import Path
import plistlib
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from asterun.contracts import Envelope
from asterun.errors import AsterunError, INSTANCE_LOCKED, INVALID_REQUEST
from asterun.service_management import (LaunchctlRunner, apply_service_plan,
                                        prepare_service_plan, rollback_service_plan)
import asterun.service_management as management
from asterun.sqlite_store import SqliteStore
from tests.conftest import write_config


SECRET = "SENTINEL_SECRET_MUST_NOT_APPEAR"


@pytest.fixture
def service_fixture(isolated_env, workspace_root, monkeypatch):
    monkeypatch.setattr("asterun.service_management.subprocess.run", lambda *a, **k: pytest.fail("测试不得启动真实进程"))
    folder = isolated_env / "instance"
    folder.mkdir(mode=0o700)
    state = folder / "state"
    state.mkdir(mode=0o700)
    binary = folder / "codex"
    bin_dir = folder / "bin"
    bin_dir.mkdir()
    interpreter = bin_dir / "python"
    executable = bin_dir / "asterun"
    for path in (binary, interpreter):
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o700)
    executable.write_text(f"#!{interpreter}\nfrom asterun.cli import main\nmain()\n")
    executable.chmod(0o700)
    purelib = folder / "site-packages"
    package = purelib / "asterun"
    package.mkdir(parents=True)
    module = package / "service_management.py"
    module.write_text("# source fixture\n")
    (package / "application.py").write_text("# application fixture\n")
    monkeypatch.setattr("asterun.service_management.__file__", str(module))
    monkeypatch.setattr("asterun.service_management.sys.executable", str(interpreter))
    monkeypatch.setattr("asterun.service_management.sysconfig.get_path", lambda _: str(purelib))
    config = write_config(folder / "config.json", workspace_root)
    store = SqliteStore(state / "asterun.sqlite")
    store.bootstrap_applied_revision(1)
    store.close()
    plist = folder / "com.asterun.test.plist"
    raw = {"Label": "com.asterun.test", "ProgramArguments": ["/usr/bin/env", "-i", f"HOME={isolated_env / 'home'}",
            "PATH=/usr/bin:/bin", "ASTERUN_RUN_GROK=1", f"EXAMPLE_TOKEN={SECRET}", str(executable),
            "--config", str(config), "--state-dir", str(state), "serve"], "RunAtLoad": True, "KeepAlive": True}
    plist.write_bytes(plistlib.dumps(raw))
    release = folder / "release"
    release.mkdir()
    current = folder / "current"
    current.symlink_to(release.name)
    return SimpleNamespace(folder=folder, state=state, binary=binary, executable=executable, config=config,
                           plist=plist, current=current, workspace=workspace_root, package=package,
                           interpreter=interpreter, calls=[])


def prepare(f, **kwargs):
    return prepare_service_plan(config_path=f.config, state_dir=f.state, plist_path=f.plist,
                                plan_dir=f.folder / "plan", codex_bin=f.binary, current_path=f.current, **kwargs)


class Client:
    def __init__(self, f, *, conflict=False, ready=True):
        self.f, self.conflict, self.ready = f, conflict, ready
        self.calls = []

    def handle(self, method, payload):
        self.calls.append((method, payload))
        config = json.loads(self.f.config.read_text())
        if method == "config.validate":
            return Envelope(ok=self.ready, data={"config": {"revision": config["revision"]}})
        if method == "config.apply":
            if self.conflict:
                return Envelope(ok=False, error={"code": "REVISION_CONFLICT", "message": SECRET})
            db = sqlite3.connect(self.f.state / "asterun.sqlite")
            assert db.execute("SELECT value FROM meta WHERE key='applied_config_revision'").fetchone()[0] == str(payload["expected_revision"])
            db.execute("UPDATE meta SET value=? WHERE key='applied_config_revision'", (str(config["revision"]),))
            db.commit()
            db.close()
            return Envelope(ok=True, data={"applied_config_revision": config["revision"]})
        assert method == "connection.verify"
        return Envelope(ok=True, data={"verified": True, "is_real_connection": True, "account": SECRET})

    def close(self):
        pass


def apply(f, summary, **kwargs):
    client = kwargs.pop("client", Client(f))
    return apply_service_plan(Path(summary["plan_path"]), expected_plan_sha256=summary["plan_sha256"],
                              runner=kwargs.pop("runner", f.calls.append), client_factory=lambda _: client,
                              ready_timeout=0.01, **kwargs)


def mutate_db(f, sql, args=()):
    with closing(sqlite3.connect(f.state / "asterun.sqlite")) as db, db:
        db.execute(sql, args)


def test_prepare_is_private_reviewable_no_spawn_and_no_secret_summary(service_fixture):
    f = service_fixture
    original_config, original_plist = f.config.read_bytes(), f.plist.read_bytes()
    summary = prepare(f, workspace_alias="review", workspace_root=f.workspace)
    assert summary["target_revision"] == 2 and summary["expected_applied_revision"] == 1
    assert summary["dropped_environment_names"] == ["EXAMPLE_TOKEN"]
    assert SECRET not in json.dumps(summary)
    assert f.config.read_bytes() == original_config and f.plist.read_bytes() == original_plist
    assert (f.folder / "plan").stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in (f.folder / "plan").iterdir())
    candidate = json.loads((f.folder / "plan/config.candidate.json").read_text())
    assert candidate["default_backend"] == "fake"
    assert candidate["backends"]["fake"] == {"kind": "fake", "enabled": True}
    assert candidate["backends"]["codex"] == {"kind": "codex", "enabled": True, "bin": str(f.binary)}
    assert candidate["workspaces"]["review"]["root"] == str(f.workspace)
    candidate_plist = (f.folder / "plan/launchagent.candidate.plist").read_bytes()
    assert SECRET.encode() not in candidate_plist
    argv = plistlib.loads(candidate_plist)["ProgramArguments"]
    assert argv[:2] == ["/usr/bin/env", "-i"] and "ASTERUN_RUN_GROK=1" in argv
    assert "ASTERUN_RUN_CODEX=1" in argv
    assert f.calls == []


@pytest.mark.parametrize("source", ["arguments", "environment_variables"])
@pytest.mark.parametrize("gate_value", ["0", "1"])
def test_codex_service_plan_preserves_known_antigravity_gate_only(service_fixture, source, gate_value):
    f = service_fixture
    config = json.loads(f.config.read_text())
    config["backends"]["codex"] = {"kind": "codex", "enabled": True, "bin": str(f.binary)}
    f.config.write_text(json.dumps(config))
    raw = plistlib.loads(f.plist.read_bytes())
    additions = {"ASTERUN_RUN_ANTIGRAVITY": gate_value,
                 "ASTERUN_RUN_UNREVIEWED": "1", "ANTIGRAVITY_BIN": "/unapproved/agy"}
    if source == "arguments":
        raw["ProgramArguments"][2:2] = [f"{name}={value}" for name, value in additions.items()]
    else:
        raw["EnvironmentVariables"] = additions
    f.plist.write_bytes(plistlib.dumps(raw))
    summary = prepare(f)
    assert summary["dropped_environment_names"] == ["ANTIGRAVITY_BIN", "ASTERUN_RUN_UNREVIEWED", "EXAMPLE_TOKEN"]
    candidate = plistlib.loads((f.folder / "plan/launchagent.candidate.plist").read_bytes())
    assert "EnvironmentVariables" not in candidate
    argv = candidate["ProgramArguments"]
    assert f"ASTERUN_RUN_ANTIGRAVITY={gate_value}" in argv
    assert "ASTERUN_RUN_CODEX=1" in argv and "ASTERUN_RUN_GROK=1" in argv
    assert not any(value.startswith(("ASTERUN_RUN_UNREVIEWED=", "ANTIGRAVITY_BIN=")) for value in argv)
    assert apply(f, summary)["status"] == "applied"
    assert plistlib.loads(f.plist.read_bytes())["ProgramArguments"] == argv


def test_apply_checks_lock_restarts_cas_and_explicit_handshake_without_prompt(service_fixture):
    f = service_fixture
    summary = prepare(f)
    client = Client(f)
    def runner(argv):
        f.calls.append(argv)
        if argv[0] == "bootout":
            contender = sqlite3.connect(f.state / "asterun.sqlite", timeout=0.01)
            try:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    contender.execute("UPDATE meta SET value='9' WHERE key='applied_config_revision'")
            finally:
                contender.close()
    result = apply(f, summary, runner=runner, client=client, verify_connection=True)
    assert result["status"] == "applied"
    assert result["connection_verification"] == "verified"
    assert result["model_called"] is False
    assert [c[0] for c in f.calls] == ["bootout", "bootstrap"]
    assert [c[0] for c in client.calls] == ["config.validate", "config.apply", "connection.verify"]
    assert client.calls[1][1] == {"expected_revision": 1}
    assert SECRET not in json.dumps(result)
    assert SECRET not in (f.folder / "plan/apply-result.json").read_text()


def test_apply_default_does_not_even_verify_connection(service_fixture):
    f = service_fixture
    summary = prepare(f)
    client = Client(f)
    assert apply(f, summary, client=client)["status"] == "applied"
    assert all(method != "connection.verify" for method, _ in client.calls)


@pytest.mark.parametrize("kind", ["legacy_run", "legacy_operation", "control_run", "outbox"])
def test_unresolved_state_blocks_stop(service_fixture, kind):
    f = service_fixture
    if kind == "legacy_run":
        mutate_db(f, "INSERT INTO runs VALUES('run_test', ?)", (json.dumps({"status": "pending_reconcile"}),))
    elif kind == "legacy_operation":
        mutate_db(f, "INSERT INTO operations VALUES('op_test','key','p','a','t','h','unknown','today',NULL,NULL)")
    elif kind == "control_run":
        mutate_db(f, "INSERT INTO control_entities VALUES('n','Run','run_test',?)", (json.dumps({"status": "UNKNOWN"}),))
    else:
        mutate_db(f, "INSERT INTO control_outbox VALUES('n','a','{}','DELIVERING')")
    summary = prepare(f)
    assert len(summary["blockers"]) == 1
    result = apply(f, summary)
    assert result["status"] == "refused" and f.calls == []


@pytest.mark.parametrize("target", ["config", "plist", "binary", "current", "state"])
def test_post_prepare_drift_refuses_without_stop(service_fixture, target):
    f = service_fixture
    summary = prepare(f)
    if target in {"config", "plist", "binary"}:
        path = getattr(f, target)
        path.write_bytes(path.read_bytes() + b"\n")
    elif target == "current":
        f.current.unlink()
        f.current.symlink_to(f.workspace)
    else:
        mutate_db(f, "UPDATE meta SET value='2' WHERE key='applied_config_revision'")
    assert apply(f, summary)["status"] == "refused"
    assert f.calls == []


def test_incorrect_review_hash_and_candidate_tampering_refused(service_fixture):
    f = service_fixture
    summary = prepare(f)
    with pytest.raises(AsterunError, match="哈希"):
        apply_service_plan(Path(summary["plan_path"]), expected_plan_sha256="0" * 64, runner=f.calls.append)
    candidate = f.folder / "plan/config.candidate.json"
    candidate.write_text(candidate.read_text() + "\n")
    with pytest.raises(AsterunError, match="文件已变化"):
        apply(f, summary)
    assert not f.calls


@pytest.mark.parametrize("failure", ["bootstrap", "cas", "socket", "secret_exception"])
def test_failure_is_recorded_partial_and_not_retried(service_fixture, failure):
    f = service_fixture
    summary = prepare(f)
    client = Client(f, conflict=failure == "cas", ready=failure != "socket")
    def runner(argv):
        f.calls.append(argv)
        if argv[0] == "bootstrap" and failure in {"bootstrap", "secret_exception"}:
            raise RuntimeError(SECRET)
    result = apply(f, summary, client=client, runner=runner)
    assert result["status"] == "partial"
    assert SECRET not in json.dumps(result)
    assert f.calls[0][0] == "bootout"
    assert len(f.calls) == 2
    assert result["state_after"]["applied_revision"] == 1
    with pytest.raises(AsterunError, match="已有应用记录"):
        apply(f, summary)


def test_rollback_restores_exact_bytes_mode_and_revision_without_old_start(service_fixture):
    f = service_fixture
    original_config, original_plist = f.config.read_bytes(), f.plist.read_bytes()
    mode = f.plist.stat().st_mode & 0o777
    summary = prepare(f)
    assert apply(f, summary)["status"] == "applied"
    f.calls.clear()
    result = rollback_service_plan(Path(summary["plan_path"]), expected_plan_sha256=summary["plan_sha256"], runner=f.calls.append, job_probe=lambda _: True)
    assert result["status"] == "rolled_back_service_stopped"
    assert result["old_service_started"] is False
    assert f.config.read_bytes() == original_config and f.plist.read_bytes() == original_plist
    assert f.plist.stat().st_mode & 0o777 == mode
    assert [c[0] for c in f.calls] == ["bootout"]
    with closing(sqlite3.connect(f.state / "asterun.sqlite")) as db:
        assert db.execute("SELECT value FROM meta WHERE key='applied_config_revision'").fetchone()[0] == "1"


@pytest.mark.parametrize("target", ["config", "state"])
def test_rollback_does_not_overwrite_concurrent_changes(service_fixture, target):
    f = service_fixture
    summary = prepare(f)
    assert apply(f, summary)["status"] == "applied"
    if target == "config":
        f.config.write_text(f.config.read_text() + "\n")
    else:
        mutate_db(f, "INSERT INTO runs VALUES('run_new', ?)", (json.dumps({"status": "running"}),))
    f.calls.clear()
    result = rollback_service_plan(Path(summary["plan_path"]), runner=f.calls.append, job_probe=lambda _: True)
    assert result["status"] == "refused" and f.calls == []


def test_prepare_rejects_pending_revision_and_workspace_rebinding(service_fixture):
    f = service_fixture
    other = f.folder / "other"
    other.mkdir()
    with pytest.raises(AsterunError, match="拒绝改绑"):
        prepare(f, workspace_alias="demo", workspace_root=other)
    mutate_db(f, "UPDATE meta SET value='2' WHERE key='applied_config_revision'")
    with pytest.raises(AsterunError, match="revision"):
        prepare(f)


def test_launchctl_runner_discards_raw_output_and_exception(monkeypatch):
    monkeypatch.setattr("asterun.service_management.sys.platform", "darwin")
    calls = []
    def fake(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=1, stdout=SECRET, stderr=SECRET)
    monkeypatch.setattr("asterun.service_management.subprocess.run", fake)
    with pytest.raises(AsterunError) as error:
        LaunchctlRunner()(["bootout", "gui/501/com.asterun.test"])
    assert SECRET not in json.dumps(error.value.to_dict())
    assert calls[0][1]["env"] == {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    assert calls[0][0][1] != "print"
    def timeout(*a, **k):
        raise subprocess.TimeoutExpired(SECRET, 1, output=SECRET, stderr=SECRET)
    monkeypatch.setattr("asterun.service_management.subprocess.run", timeout)
    with pytest.raises(AsterunError) as error:
        LaunchctlRunner()(["bootstrap", "gui/501", "/unused.plist"])
    assert SECRET not in json.dumps(error.value.to_dict())


def test_failed_bootstrap_with_absent_job_still_restores_files(service_fixture):
    f = service_fixture
    before = f.config.read_bytes(), f.plist.read_bytes()
    summary = prepare(f)
    def runner(argv):
        f.calls.append(argv)
        if argv[0] == "bootstrap":
            raise RuntimeError("bootstrap did not load job")
    assert apply(f, summary, runner=runner)["status"] == "partial"
    calls_before = list(f.calls)
    result = rollback_service_plan(Path(summary["plan_path"]), runner=runner, job_probe=lambda _: False)
    assert result["status"] == "rolled_back_service_stopped"
    assert "core_already_unloaded" in result["steps"]
    assert f.calls == calls_before
    assert (f.config.read_bytes(), f.plist.read_bytes()) == before


class ProcessCrash(BaseException):
    pass


def crash_after_config_rename(f, summary, monkeypatch):
    record = management._record
    def crash(folder, name, result, step=None):
        if name == "apply-result.json" and step == "config_written":
            raise ProcessCrash()
        return record(folder, name, result, step)
    with monkeypatch.context() as patch:
        patch.setattr(management, "_record", crash)
        with pytest.raises(ProcessCrash):
            apply(f, summary)


def test_crash_after_rename_before_journal_has_safe_recovery(service_fixture, monkeypatch):
    f = service_fixture
    before = f.config.read_bytes(), f.plist.read_bytes()
    summary = prepare(f)
    crash_after_config_rename(f, summary, monkeypatch)
    journal = json.loads((f.folder / "plan/apply-result.json").read_text())
    assert journal["status"] == "in_progress" and "config_write_requested" in journal["steps"]
    assert not journal["changed_files"] and "state_after" not in journal
    result = rollback_service_plan(Path(summary["plan_path"]), runner=f.calls.append, job_probe=lambda _: False)
    assert result["status"] == "rolled_back_service_stopped"
    assert result["recovered_interrupted_apply"] is True
    assert (f.config.read_bytes(), f.plist.read_bytes()) == before


def test_crash_after_cas_before_receipt_only_allows_own_revision_change(service_fixture):
    f = service_fixture
    before = f.config.read_bytes(), f.plist.read_bytes()
    summary = prepare(f)
    class CrashClient(Client):
        def handle(self, method, payload):
            response = super().handle(method, payload)
            if method == "config.apply":
                raise ProcessCrash()
            return response
    with pytest.raises(ProcessCrash):
        apply(f, summary, client=CrashClient(f))
    journal = json.loads((f.folder / "plan/apply-result.json").read_text())
    assert journal["status"] == "in_progress" and "config_apply_requested" in journal["steps"]
    result = rollback_service_plan(Path(summary["plan_path"]), runner=f.calls.append, job_probe=lambda _: True)
    assert result["status"] == "rolled_back_service_stopped"
    assert (f.config.read_bytes(), f.plist.read_bytes()) == before
    with closing(sqlite3.connect(f.state / "asterun.sqlite")) as db:
        assert db.execute("SELECT value FROM meta WHERE key='applied_config_revision'").fetchone()[0] == "1"


@pytest.mark.parametrize("target", ["config", "state"])
def test_interrupted_apply_does_not_overwrite_drift(service_fixture, monkeypatch, target):
    f = service_fixture
    summary = prepare(f)
    crash_after_config_rename(f, summary, monkeypatch)
    if target == "config":
        f.config.write_text(f.config.read_text() + "\n")
    else:
        mutate_db(f, "INSERT INTO runs VALUES('new', ?)", (json.dumps({"status": "succeeded"}),))
    f.calls.clear()
    result = rollback_service_plan(Path(summary["plan_path"]), runner=f.calls.append, job_probe=lambda _: False)
    assert result["status"] == "refused" and f.calls == []


def test_target_install_sources_and_interpreter_are_bound(service_fixture):
    f = service_fixture
    summary = prepare(f)
    (f.package / "application.py").write_text("# modified actual service package\n")
    assert apply(f, summary)["status"] == "refused"
    assert f.calls == []


def test_cross_install_console_script_is_rejected_without_spawning(service_fixture):
    f = service_fixture
    other = f.folder / "other-bin"
    other.mkdir()
    interpreter = other / "python"
    interpreter.write_bytes(f.interpreter.read_bytes())
    interpreter.chmod(0o700)
    f.executable.write_text(f"#!{interpreter}\nfrom asterun.cli import main\nmain()\n")
    with pytest.raises(AsterunError, match="同一安装"):
        prepare(f)
    assert not (f.folder / "plan").exists() and f.calls == []


def test_empty_console_script_is_explicitly_rejected_before_preparing_files(service_fixture):
    f = service_fixture
    f.executable.write_bytes(b"")
    original = f.config.read_bytes(), f.plist.read_bytes()
    with pytest.raises(AsterunError, match="console script 为空") as error:
        prepare(f)
    assert error.value.code == "INVALID_CONFIG"
    assert not (f.folder / "plan").exists() and f.calls == []
    assert (f.config.read_bytes(), f.plist.read_bytes()) == original


def test_job_probe_distinguishes_missing_from_other_errors_without_raw_output(monkeypatch):
    monkeypatch.setattr("asterun.service_management.sys.platform", "darwin")
    target = f"gui/{os.getuid()}/com.asterun.test"
    missing = f'Bad request.\nCould not find service "com.asterun.test" in domain for user gui: {os.getuid()}\n'.encode()
    monkeypatch.setattr("asterun.service_management.subprocess.run", lambda *a, **k: SimpleNamespace(returncode=113, stdout=b"", stderr=missing))
    assert LaunchctlRunner().is_loaded(target) is False
    monkeypatch.setattr("asterun.service_management.subprocess.run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=SECRET.encode(), stderr=b""))
    assert LaunchctlRunner().is_loaded(target) is True
    monkeypatch.setattr("asterun.service_management.subprocess.run", lambda *a, **k: SimpleNamespace(returncode=113, stdout=b"", stderr=SECRET.encode()))
    with pytest.raises(AsterunError) as error:
        LaunchctlRunner().is_loaded(target)
    assert SECRET not in json.dumps(error.value.to_dict())


def stopped_operation(f, summary, operation):
    if operation == "apply":
        return lambda: apply(f, summary)
    assert apply(f, summary)["status"] == "applied"
    f.calls.clear()
    return lambda: rollback_service_plan(Path(summary["plan_path"]), runner=f.calls.append, job_probe=lambda _: True)


def lock_wait_clock(monkeypatch, on_sleep=None):
    clock = SimpleNamespace(now=0.0, sleeps=[])
    monkeypatch.setattr(management.time, "monotonic", lambda: clock.now)
    def sleep(seconds):
        clock.sleeps.append(seconds)
        clock.now += seconds
        if on_sleep:
            on_sleep(clock)
    monkeypatch.setattr(management.time, "sleep", sleep)
    return clock


@pytest.mark.parametrize("operation", ["apply", "rollback"])
def test_bootout_waits_for_delayed_execution_lock_while_holding_sqlite(service_fixture, monkeypatch, operation):
    f = service_fixture
    original_files = f.config.read_bytes(), f.plist.read_bytes()
    summary = prepare(f)
    action = stopped_operation(f, summary, operation)
    held = management.InstanceLock(f.state / "asterun.lock")
    held.acquire()
    checked = []
    def on_sleep(clock):
        if not checked:
            contender = sqlite3.connect(f.state / "asterun.sqlite", timeout=0.001)
            try:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    contender.execute("BEGIN IMMEDIATE")
                checked.append(True)
            finally:
                contender.close()
        if clock.now >= 0.2:
            held.release()
    clock = lock_wait_clock(monkeypatch, on_sleep)
    try:
        result = action()
    finally:
        held.release()
    assert result["status"] == ("applied" if operation == "apply" else "rolled_back_service_stopped")
    assert 0.2 <= clock.now < 10 and checked == [True]
    assert len([call for call in f.calls if call[0] == "bootout"]) == 1
    if operation == "rollback":
        assert (f.config.read_bytes(), f.plist.read_bytes()) == original_files


@pytest.mark.parametrize("operation", ["apply", "rollback"])
def test_execution_lock_deadline_does_not_modify_files_or_revision(service_fixture, monkeypatch, operation):
    f = service_fixture
    summary = prepare(f)
    action = stopped_operation(f, summary, operation)
    original_files = f.config.read_bytes(), f.plist.read_bytes()
    with closing(sqlite3.connect(f.state / "asterun.sqlite")) as db:
        before_revision = db.execute("SELECT value FROM meta WHERE key='applied_config_revision'").fetchone()[0]
    held = management.InstanceLock(f.state / "asterun.lock")
    held.acquire()
    clock = lock_wait_clock(monkeypatch)
    try:
        result = action()
    finally:
        held.release()
    assert result["status"] == "partial" and result["error_code"] == INSTANCE_LOCKED
    assert clock.now == 10 and clock.sleeps
    assert "core_stopped" not in result["steps"]
    assert (f.config.read_bytes(), f.plist.read_bytes()) == original_files
    assert not any(call[0] == "bootstrap" for call in f.calls)
    with closing(sqlite3.connect(f.state / "asterun.sqlite")) as db:
        assert db.execute("SELECT value FROM meta WHERE key='applied_config_revision'").fetchone()[0] == before_revision


@pytest.mark.parametrize("operation", ["apply", "rollback"])
def test_execution_lock_other_errors_are_not_retried(service_fixture, monkeypatch, operation):
    f = service_fixture
    summary = prepare(f)
    action = stopped_operation(f, summary, operation)
    original_files = f.config.read_bytes(), f.plist.read_bytes()
    original_acquire = management.InstanceLock.acquire
    calls = []
    def fail(lock):
        if lock.path.name == "asterun.lock":
            calls.append(lock.path)
            raise AsterunError(INVALID_REQUEST, SECRET)
        return original_acquire(lock)
    monkeypatch.setattr(management.InstanceLock, "acquire", fail)
    clock = lock_wait_clock(monkeypatch)
    result = action()
    assert result["status"] == "partial" and result["error_code"] == INVALID_REQUEST
    assert len(calls) == 1 and not clock.sleeps
    assert (f.config.read_bytes(), f.plist.read_bytes()) == original_files
    assert SECRET not in json.dumps(result)
