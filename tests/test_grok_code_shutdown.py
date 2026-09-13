from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from threading import Event, Thread

from asterun.backends.grok import DISPATCH_RECEIPT_KEY, GrokBackend
from asterun.config import BackendConfig
from asterun.grok_profile import CODE_PROFILE, prepare_home
from asterun.ids import RunId, TaskId


NATIVE = '''import os, pathlib, signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
pathlib.Path("native.pid").write_text(str(os.getpid()))
while True:
    time.sleep(1)
'''

HOST = '''import json, pathlib, sys, time
from threading import Event, Thread
from asterun.backends.grok import GrokBackend
from asterun.config import BackendConfig
from asterun.ids import TaskId, RunId
options = json.loads(sys.argv[1])
workspace = pathlib.Path(sys.argv[2])
backend = GrokBackend(BackendConfig(name="grok", **options))
stopping = Event()
results = []
worker = Thread(target=lambda: results.append(backend.dispatch_stream(
    TaskId("task"), RunId("run"), "", "fixture", cwd=workspace,
    stopping=stopping)), daemon=True)
worker.start()
deadline = time.monotonic() + 5
while not (workspace / "native.pid").exists():
    assert time.monotonic() < deadline, "native fixture did not start"
    time.sleep(.005)
# Match Application.close: backend cleanup precedes Executor's short daemon join.
stopping.set()
backend.close()
worker.join(timeout=.2)
print(json.dumps({"worker_alive": worker.is_alive(), "results": results}))
'''


def configured_backend(tmp_path, monkeypatch):
    home = tmp_path / "native-home"
    prepare_home(home, execution_profile=CODE_PROFILE)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binary = tmp_path / "native-fixture"
    binary.write_text(f"#!{sys.executable}\n" + NATIVE)
    binary.chmod(0o755)
    monkeypatch.setenv("ASTERUN_RUN_GROK", "1")
    options = {"kind": "grok", "bin": str(binary), "home": str(home),
               "execution_profile": CODE_PROFILE, "timeout_seconds": 10}
    return GrokBackend(BackendConfig(name="grok", **options)), options, workspace


def test_host_exit_synchronously_kills_native_process_that_ignores_sigterm(tmp_path, monkeypatch):
    _, options, workspace = configured_backend(tmp_path, monkeypatch)
    env = {"PATH": os.environ.get("PATH", ""), "ASTERUN_RUN_GROK": "1",
           "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    try:
        host = subprocess.run([sys.executable, "-c", HOST, json.dumps(options), str(workspace)],
                              env=env, capture_output=True, text=True, timeout=10)
        assert host.returncode == 0, host.stderr
        pid = int((workspace / "native.pid").read_text())
        result = json.loads(host.stdout)
        assert not result["worker_alive"]
        assert result["results"][0]["status"] == "pending_reconcile"
        assert result["results"][0]["terminated"] is False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError("native process survived the host's normal shutdown")
    finally:
        # Own fixture cleanup also runs when testing an unfixed implementation.
        marker = workspace / "native.pid"
        if marker.exists():
            try:
                os.killpg(int(marker.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_close_before_spawn_closes_registered_handle_without_starting_process(tmp_path, monkeypatch):
    backend, _, workspace = configured_backend(tmp_path, monkeypatch)
    before_spawn, release = Event(), Event()
    results = []

    def publish(update):
        if update["status"] == "dispatching":
            before_spawn.set()
            assert release.wait(5)

    worker = Thread(target=lambda: results.append(backend.dispatch_stream(
        TaskId("task"), RunId("run"), "", "fixture", cwd=workspace, publish=publish)))
    worker.start()
    try:
        assert before_spawn.wait(3)
        backend.close()
    finally:
        release.set()
        worker.join(timeout=3)
    assert not worker.is_alive()
    assert not (workspace / "native.pid").exists()
    assert results[0]["status"] == "failed"
    assert results[0]["native"][DISPATCH_RECEIPT_KEY]["delivery"] == "not_sent"


def test_closed_backend_rejects_new_coding_dispatch(tmp_path, monkeypatch):
    backend, _, workspace = configured_backend(tmp_path, monkeypatch)
    backend.close()
    result = backend.dispatch(TaskId("task"), RunId("run"), "", "fixture", cwd=workspace)
    assert result["status"] == "failed"
    assert result["native"][DISPATCH_RECEIPT_KEY]["delivery"] == "not_sent"
    assert not (workspace / "native.pid").exists()
