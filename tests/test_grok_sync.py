import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from asterun.errors import AsterunError
from asterun.grok_profile import main
from asterun.grok_sync import FILES, MARKER, sync_sessions


def native(home, sid="session-1"):
    session = home / "sessions" / "%2Fworkspace" / sid
    session.mkdir(parents=True)
    (session / "summary.json").write_text(json.dumps({"current_model_id": "grok-test"}))
    (session / "updates.jsonl").write_text(json.dumps({"timestamp": 123,
        "method": "session/update", "params": {"sessionId": sid, "update": {
            "sessionUpdate": "turn_completed", "usage": {"inputTokens": 10, "outputTokens": 2}}}}) + "\n")
    (session / "system_prompt.txt").write_text("not exported")
    (home / "auth.json").write_text("not exported")
    return session


def test_full_incremental_sync_and_existing_identical_trial(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    session = native(src)
    result = sync_sessions(src, dst)
    assert result["copied"] == 1 and not result["skipped"]
    target = dst / session.relative_to(src)
    assert set(p.name for p in target.iterdir()) == {*FILES, MARKER}
    assert not (dst / "auth.json").exists()
    before = (target / "updates.jsonl").stat().st_mtime_ns
    assert sync_sessions(src, dst)["unchanged"] == 1
    assert (target / "updates.jsonl").stat().st_mtime_ns == before
    (target / MARKER).unlink()  # Adopt the exact same two files from a manual trial.
    assert sync_sessions(src, dst)["unchanged"] == 1
    with (session / "updates.jsonl").open("a") as f:
        f.write(json.dumps({"params": {"sessionId": session.name}, "timestamp": 124}) + "\n")
    assert sync_sessions(src, dst)["copied"] == 1
    assert (target / "updates.jsonl").read_bytes() == (session / "updates.jsonl").read_bytes()


@pytest.mark.parametrize("change", ["content", "native_file", "other_source"])
def test_never_overwrite_native_or_modified_destination(tmp_path, change):
    src, dst = tmp_path / "src", tmp_path / "dst"
    session = native(src)
    sync_sessions(src, dst)
    target = dst / session.relative_to(src)
    if change == "content":
        (target / "updates.jsonl").write_text("user-owned history\n")
    elif change == "native_file":
        (target / "chat_history.jsonl").write_text("resumed natively")
    else:
        src = tmp_path / "other"
        native(src)
    snapshot = {p.name: p.read_bytes() for p in target.iterdir()}
    assert sync_sessions(src, dst)["conflicts"] == 1
    assert {p.name: p.read_bytes() for p in target.iterdir()} == snapshot


@pytest.mark.parametrize("where", ["source_file", "source_session", "target_file", "target_group", "lock"])
def test_no_symlink_following(tmp_path, where):
    src, dst = tmp_path / "src", tmp_path / "dst"
    session = native(src)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "secret"
    sentinel.write_text("private")
    if where == "source_file":
        (session / "updates.jsonl").unlink()
        (session / "updates.jsonl").symlink_to(sentinel)
    elif where == "source_session":
        (session.parent / "linked-session").symlink_to(outside, target_is_directory=True)
    else:
        sync_sessions(src, dst)
        target = dst / session.relative_to(src)
        if where == "target_file":
            (target / "updates.jsonl").unlink()
            (target / "updates.jsonl").symlink_to(sentinel)
        elif where == "target_group":
            target.parent.rename(dst / "saved-group")
            target.parent.symlink_to(outside, target_is_directory=True)
        else:
            (dst / "sessions/.asterun-sync.lock").unlink()
            (dst / "sessions/.asterun-sync.lock").symlink_to(sentinel)
    if where == "lock":
        with pytest.raises(OSError):
            sync_sessions(src, dst)
    else:
        assert sync_sessions(src, dst)["skipped"] >= 1
    assert sentinel.read_text() == "private"
    assert list(outside.iterdir()) == [sentinel]


@pytest.mark.parametrize("contents", ['{"unfinished":', '{broken}\n', '[]\n', '{"params":{"sessionId":"wrong"}}\n'])
def test_incomplete_or_invalid_log_is_retryable_without_partial_publish(tmp_path, contents):
    src, dst = tmp_path / "src", tmp_path / "dst"
    session = native(src)
    good = (session / "updates.jsonl").read_bytes()
    (session / "updates.jsonl").write_text(contents)
    assert sync_sessions(src, dst)["skipped"] == 1
    assert not (dst / session.relative_to(src)).exists()
    (session / "updates.jsonl").write_bytes(good)
    assert sync_sessions(src, dst)["copied"] == 1


def test_filter_missing_and_concurrent_exports(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    native(src)
    native(src, "session-2")
    assert sync_sessions(src, dst, session_id="missing")["skipped"] == 1
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: sync_sessions(src, dst, session_id="session-1"), range(2)))
    assert sum(r["copied"] for r in results) == 1
    assert sum(r["unchanged"] for r in results) == 1
    assert not (dst / "sessions/%2Fworkspace/session-2").exists()


def test_reject_overlapping_roots_and_invalid_filter(tmp_path):
    native(tmp_path)
    for target in [tmp_path, tmp_path / "child", tmp_path.parent]:
        with pytest.raises(AsterunError):
            sync_sessions(tmp_path, target)
    with pytest.raises(AsterunError):
        sync_sessions(tmp_path, tmp_path.parent / "target", session_id="../escape")


def test_cli_backfill_and_failure_exit(tmp_path, capsys):
    src, dst = tmp_path / "src", tmp_path / "dst"
    native(src)
    args = ["sync-sessions", "--home", str(src), "--destination-home", str(dst)]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["copied"] == 1
    assert main(args + ["--session-id", "missing"]) == 1
    assert json.loads(capsys.readouterr().out)["skipped"] == 1


@pytest.mark.parametrize("stream", [False, True])
def test_coding_dispatch_automatically_exports_after_process_exit(isolated_env, workspace_root, monkeypatch, stream):
    from tests.test_grok_code_dispatch import setup_backend
    from asterun.backends.grok import GrokBackend
    from asterun.config import BackendConfig
    from asterun.ids import TaskId, RunId
    options = setup_backend(isolated_env, monkeypatch)
    binary = Path(options["bin"])
    # The fixture produces actual native-shaped files in its isolated HOME.
    binary.write_text(binary.read_text().replace("for event in [", '''import os
home = pathlib.Path(os.environ['GROK_HOME'])
session = home / 'sessions' / '%2Fworkspace' / sid
session.mkdir(parents=True)
(session / 'summary.json').write_text('{}')
(session / 'updates.jsonl').write_text(json.dumps({'params': {'sessionId': sid,
    'update': {'sessionUpdate': 'turn_completed', 'usage': {'inputTokens': 10}}}}) + '\\n')
for event in ['''))
    target = isolated_env / "usage-home"
    backend = GrokBackend(BackendConfig(name="grok", **options, session_sync_home=str(target)))
    dispatch = backend.dispatch_stream if stream else backend.dispatch
    result = dispatch(TaskId("task"), RunId("run"), "", "Implement the TR", cwd=workspace_root)
    assert result["status"] == "succeeded"
    assert (target / "sessions/%2Fworkspace" / result["native"]["session_id"] / "updates.jsonl").is_file()


def test_sync_failure_does_not_change_coding_result(isolated_env, workspace_root, monkeypatch, caplog):
    from tests.test_grok_code_dispatch import setup_backend
    from asterun.backends.grok import GrokBackend
    from asterun.config import BackendConfig
    from asterun.ids import TaskId, RunId
    options = setup_backend(isolated_env, monkeypatch)
    backend = GrokBackend(BackendConfig(name="grok", **options, session_sync_home=str(isolated_env / "usage")))
    def fail(*args, **kwargs):
        raise OSError("secret diagnostic")
    monkeypatch.setattr("asterun.grok_sync.sync_sessions", fail)
    result = backend.dispatch(TaskId("task"), RunId("run"), "", "Implement the TR", cwd=workspace_root)
    assert result["status"] == "succeeded"
    assert "sync failed: OSError" in caplog.text and "secret diagnostic" not in caplog.text


def test_source_rewind_does_not_remove_exported_history(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    session = native(src)
    sync_sessions(src, dst)
    original = (session / "updates.jsonl").read_bytes()
    (session / "updates.jsonl").write_text("")
    assert sync_sessions(src, dst)["conflicts"] == 1
    assert (dst / session.relative_to(src) / "updates.jsonl").read_bytes() == original


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("stop_reason", ["end_turn", "cancelled"])
def test_acp_sync_is_opt_in_and_waits_for_transport_close(tmp_path, monkeypatch, enabled, stop_reason):
    from asterun.backends.grok import GrokBackend
    from asterun.config import BackendConfig
    from asterun.ids import TaskId, RunId
    src, dst = tmp_path / "native", tmp_path / "usage"
    class Transport:
        exit_code = 0
        def __init__(self, **kwargs): pass
        def on_notification(self, callback): pass
        def spawn(self, cwd): pass
        def initialize(self): return {}
        def new_session(self, cwd): return {"sessionId": "session-1"}
        def prompt(self, session_id, text, **kwargs): return {"stopReason": stop_reason}
        def close(self): native(src)
    monkeypatch.setattr("asterun.backends.grok.GrokAcpTransport", Transport)
    monkeypatch.setattr("asterun.backends.grok.validate_workspace", lambda *a, **kw: None)
    monkeypatch.setattr("asterun.backends.grok.build_environment", lambda *a, **kw: {})
    backend = GrokBackend(BackendConfig(name="grok", kind="grok", home=str(src),
        session_sync_home=str(dst) if enabled else None))
    monkeypatch.setattr(backend, "can_dispatch", lambda: True)
    monkeypatch.setattr(backend, "discover_bin", lambda: Path("/unused"))
    monkeypatch.setattr(backend, "_prepared_home", lambda: src)
    result = backend.dispatch(TaskId("t"), RunId("r"), "", "fixture", cwd=tmp_path)
    assert result["status"] == ("succeeded" if stop_reason == "end_turn" else "cancelled")
    assert (dst / "sessions/%2Fworkspace/session-1/updates.jsonl").exists() == enabled
