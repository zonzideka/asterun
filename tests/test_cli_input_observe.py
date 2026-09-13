from __future__ import annotations

import io
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time

import pytest

from asterun import cli, observe
from asterun.cli_input import MAX_STDIN_BYTES, check_ipc_size, merge_task_input, read_stdin
from asterun.contracts import Envelope
from asterun.errors import AsterunError, INVALID_REQUEST


def stdin(monkeypatch: pytest.MonkeyPatch, value: bytes) -> None:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(value), encoding="utf-8"))


def test_stdin_boundary_is_utf8_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    value = "界" * (MAX_STDIN_BYTES // 3) + "x" * (MAX_STDIN_BYTES % 3)
    stdin(monkeypatch, value.encode())
    assert read_stdin() == value
    stdin(monkeypatch, value.encode() + b"x")
    with pytest.raises(AsterunError, match="512 KiB"):
        read_stdin()


def test_stdin_rejects_invalid_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    stdin(monkeypatch, b"safe\xff")
    with pytest.raises(AsterunError, match="UTF-8"):
        read_stdin()


@pytest.mark.parametrize(("payload", "text", "input_path"), [
    ({}, "", Path("-")), ({"text": "request"}, "cli", None),
    ({"path": "note.txt"}, "cli", None), ({"text": "request"}, None, Path("-")),
    ({"path": "note.txt"}, None, Path("other.txt")),
    ({"text": "", "path": "note.txt"}, None, None),
])
def test_conflicting_inputs_rejected_before_stdin(payload, text, input_path, monkeypatch) -> None:
    class Unreadable:
        def read(self, size):
            pytest.fail("冲突请求不应读取 stdin")
    monkeypatch.setattr(sys, "stdin", Unreadable())
    with pytest.raises(AsterunError, match="互斥"):
        merge_task_input(payload, text=text, input_path=input_path)


def test_existing_file_input_is_not_read_by_client() -> None:
    assert merge_task_input({}, text=None, input_path=Path("missing.txt")) == {"path": "missing.txt"}


def test_encoded_ipc_size_accounts_for_json_escaping() -> None:
    check_ipc_size("task.submit", {"workspace": "demo", "text": "界" * 170000})
    with pytest.raises(AsterunError, match="最终编码请求"):
        check_ipc_size("task.submit", {"workspace": "demo", "text": "\x00" * 180000})


def test_invalid_input_does_not_open_state(config_path, isolated_env, monkeypatch, capsys) -> None:
    stdin(monkeypatch, b"\x00" * 180000)
    state = isolated_env / "uncreated-state"
    assert cli.main(["--config", str(config_path), "--state-dir", str(state), "task-submit",
                     "--workspace", "demo", "--input", "-"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == INVALID_REQUEST
    assert not state.exists()


def test_stdin_fake_submit_reuses_original_key(config_path, isolated_env, workspace_root, monkeypatch, capsys) -> None:
    before = sorted(workspace_root.iterdir())
    args = ["--config", str(config_path), "--state-dir", str(isolated_env / "state"), "task-submit",
            "--workspace", "demo", "--script", "success", "--input", "-", "--idempotency-key", "stdin-retry"]
    stdin(monkeypatch, "审查中文\n第二行".encode())
    assert cli.main(args) == 0
    first = json.loads(capsys.readouterr().out)
    stdin(monkeypatch, "审查中文\n第二行".encode())
    assert cli.main(args) == 0
    second = json.loads(capsys.readouterr().out)
    assert first["ids"] == second["ids"]
    assert first["data"]["task"]["input_hash"] == second["data"]["task"]["input_hash"]
    assert sorted(workspace_root.iterdir()) == before


@pytest.mark.parametrize("option", ["--expected-applied-revision", "--revision"])
def test_config_apply_revision_aliases(option) -> None:
    args = cli.build_parser().parse_args(["config-apply", option, "1"])
    assert cli._load_request(args) == {"expected_revision": 1}


def test_config_apply_revision_options_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["config-apply", "--expected-applied-revision", "1", "--revision", "1"])


class Clock:
    def __init__(self):
        self.now = 10.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def install_clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(observe.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(observe.time, "sleep", clock.sleep)
    return clock


def snapshot(status="running", run_id="run_1"):
    return Envelope(ok=True, data={"run": {"id": run_id, "status": status}, "task": {"id": "tsk_1"}})


def events(cursor=0, count=0, run_id="run_1"):
    return Envelope(ok=True, data={"run_id": run_id, "cursor": cursor,
                                  "events": [{"seq": cursor + index + 1} for index in range(count)],
                                  "next_cursor": cursor + count})


@pytest.mark.parametrize(("status", "reason"), [("succeeded", "terminal"), ("failed", "terminal"),
    ("cancelled", "terminal"), ("waiting_input", "waiting_input"), ("waiting_auth", "waiting_auth"),
    ("paused", "paused"), ("pending_reconcile", "unknown"), ("new_unknown_state", "unknown")])
def test_watch_returns_on_terminal_or_attention_without_mutation(monkeypatch, status, reason):
    install_clock(monkeypatch)
    methods = []
    def read(path, method, payload, deadline):
        methods.append(method)
        return snapshot(status) if method == "task.get" else events(0, 1)
    monkeypatch.setattr(observe, "_read", read)
    emitted = []
    result = observe.watch_task(Path("unused.sock"), "tsk_1", on_events=emitted.append)
    assert result.data["reason"] == reason
    assert result.data["cursor"] == 1
    assert result.ok is (reason != "unknown")
    assert methods == ["task.get", "task.events"]
    assert len(emitted) == 1


def test_watch_fixed_deadline_and_clipped_request_timeout(monkeypatch):
    clock = install_clock(monkeypatch)
    calls = []
    def read(path, method, payload, deadline):
        calls.append((clock.now, deadline, method))
        clock.now += min(0.1, deadline - clock.now)
        return snapshot() if method == "task.get" else events()
    monkeypatch.setattr(observe, "_read", read)
    result = observe.watch_task(Path("unused.sock"), "tsk_1", timeout=1, request_timeout=0.3, interval=0.2)
    assert result.data["reason"] == "deadline"
    assert clock.now == 11.0
    assert all(deadline <= 11 and deadline - started <= 0.300001 for started, deadline, _ in calls)
    assert result.data["last_snapshot"]["run"]["status"] == "running"


def test_watch_pages_incrementally_and_resets_cursor_when_run_changes(monkeypatch):
    install_clock(monkeypatch)
    responses = [snapshot(), events(3, 2), events(5, 0), snapshot("succeeded", "run_2"), events(0, 1, "run_2")]
    payloads = []
    def read(path, method, payload, deadline):
        payloads.append((method, payload))
        return responses.pop(0)
    monkeypatch.setattr(observe, "_read", read)
    result = observe.watch_task(Path("unused.sock"), "tsk_1", cursor=3, run_id="run_1", page_size=2)
    assert [payload["cursor"] for method, payload in payloads if method == "task.events"] == [3, 5, 0]
    assert result.data["run_id"] == "run_2"
    assert result.data["cursor"] == 1


def test_watch_rechecks_when_run_changes_between_get_and_events(monkeypatch):
    install_clock(monkeypatch)
    responses = [snapshot("succeeded"), events(0, 1, "run_2"), snapshot("waiting_input", "run_2"), events(0, 1, "run_2")]
    monkeypatch.setattr(observe, "_read", lambda *args: responses.pop(0))
    result = observe.watch_task(Path("unused.sock"), "tsk_1")
    assert result.data["reason"] == "waiting_input"
    assert result.data["run_id"] == "run_2"


def test_watch_returns_pending_approval_even_if_run_still_running(monkeypatch):
    install_clock(monkeypatch)
    current = snapshot()
    current.data["approval"] = {"state": "pending", "id": "apr_1"}
    responses = [current, events()]
    monkeypatch.setattr(observe, "_read", lambda *args: responses.pop(0))
    result = observe.watch_task(Path("unused.sock"), "tsk_1")
    assert result.data["reason"] == "waiting_input"
    assert result.data["last_snapshot"]["approval"]["id"] == "apr_1"


def test_watch_retains_snapshot_on_disconnect_and_never_retries(monkeypatch):
    install_clock(monkeypatch)
    calls = []
    def read(path, method, payload, deadline):
        calls.append(method)
        if method == "task.get":
            return snapshot()
        raise OSError("disconnect")
    monkeypatch.setattr(observe, "_read", read)
    result = observe.watch_task(Path("unused.sock"), "tsk_1")
    assert result.data["reason"] == "read_error"
    assert result.data["last_snapshot"]["run"]["status"] == "running"
    assert calls == ["task.get", "task.events"]


def test_watch_interrupt_returns_last_snapshot(monkeypatch):
    install_clock(monkeypatch)
    def read(path, method, payload, deadline):
        if method == "task.events":
            raise KeyboardInterrupt()
        return snapshot()
    monkeypatch.setattr(observe, "_read", read)
    result = observe.watch_task(Path("unused.sock"), "tsk_1")
    assert result.data["reason"] == "interrupted"
    assert result.data["last_snapshot"]["run"]["id"] == "run_1"


@pytest.mark.parametrize("options", [{"timeout": float("nan")}, {"request_timeout": float("inf")},
    {"interval": 0}, {"cursor": 1}, {"page_size": 501}])
def test_watch_rejects_invalid_deadline_or_cursor(options):
    with pytest.raises(AsterunError):
        observe.watch_task(Path("unused.sock"), "tsk_1", **options)


def test_watch_cli_requires_resident_connection(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_app", lambda *args, **kwargs: pytest.fail("不应初始化应用"))
    assert cli.main(["task-watch", "tsk_1"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == INVALID_REQUEST


def test_watch_socket_deadline_does_not_reset_on_partial_response():
    # 持续分片比逐 recv timeout 更快；固定总时限仍必须结束。
    stop = Event()
    with TemporaryDirectory(prefix="aw-", dir="/tmp") as directory, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        path = Path(directory) / "watch.sock"
        listener.bind(str(path))
        path.chmod(0o600)
        listener.listen(1)
        def server():
            conn, _ = listener.accept()
            with conn:
                conn.recv(65536)
                while not stop.wait(0.01):
                    try:
                        conn.sendall(b" ")
                    except OSError:
                        break
        worker = Thread(target=server)
        worker.start()
        try:
            start = time.monotonic()
            result = observe.watch_task(path, "tsk_1", timeout=0.15, request_timeout=1)
            elapsed = time.monotonic() - start
            assert result.data["reason"] == "deadline"
            assert 0.1 <= elapsed < 1.0
            assert result.data["requests"] == 1
        finally:
            stop.set()
            worker.join(timeout=1)
