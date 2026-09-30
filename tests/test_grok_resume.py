from dataclasses import replace
import json
from pathlib import Path
import sys
from threading import Event

import pytest

from asterun.application import Application
from asterun.backends.grok import GrokBackend
from asterun.config import BackendConfig
from asterun.grok_profile import prepare_home, CODE_PROFILE
from asterun.ids import TaskId, RunId
from tests.conftest import write_config


def options(tmp_path, monkeypatch):
    home = tmp_path / "grok-home"
    prepare_home(home, execution_profile=CODE_PROFILE)
    binary = tmp_path / "grok-fake"
    binary.write_text(f"#!{sys.executable}\n" + '''
import json, os, pathlib, sys, urllib.parse
args=sys.argv
if '--help' in args:
 print('--session-id --resume');sys.exit(0)
sid=args[args.index('--resume' if '--resume' in args else '--session-id')+1]
cwd=pathlib.Path.cwd()
log=cwd/'calls.jsonl'
with log.open('a') as f:f.write(json.dumps({'session':sid,'resumed':'--resume' in args})+'\\n')
session=pathlib.Path(os.environ['GROK_HOME'])/'sessions'/urllib.parse.quote(str(cwd),safe='')/sid
session.mkdir(parents=True,exist_ok=True)
(session/'summary.json').write_text(json.dumps({'info':{'id':sid,'cwd':str(cwd)}}))
with (session/'updates.jsonl').open('a') as f:f.write(json.dumps({'params':{'sessionId':sid}})+'\\n')
prompt=pathlib.Path(args[args.index('--prompt-file')+1]).read_text()
reason='cancelled' if prompt=='cancel' else 'max_turn_requests' if prompt=='limit' else 'end_turn'
print(json.dumps({'type':'end','sessionId':sid,'stopReason':reason}))
''')
    binary.chmod(0o755)
    monkeypatch.setenv("ASTERUN_RUN_GROK", "1")
    return {"kind": "grok", "home": str(home), "bin": str(binary),
            "execution_profile": CODE_PROFILE, "session_policy": "resume"}


@pytest.mark.parametrize("first_prompt", ["start", "cancel", "limit"])
def test_same_native_session_survives_restart_and_terminal_outcomes(isolated_env, workspace_root, monkeypatch, first_prompt):
    conf = write_config(isolated_env / "cfg.json", workspace_root, {"grok": options(isolated_env, monkeypatch)})
    state = isolated_env / "state"
    app = Application.from_paths(conf, state)
    first = app.handle("task.submit", {"workspace": "demo", "backend": "grok", "text": first_prompt})
    assert first.ok, first.error
    cid = first.ids["conversation_id"]
    sid = first.data["run"]["native"]["session_id"]
    app.close()
    app = Application.from_paths(conf, state)
    try:
        check = app.handle("session.resume", {"conversation_id": cid, "native": True})
        assert check.ok and check.data["resume_ready"] and not check.data["native_resumed"], check.error
        second = app.handle("task.submit", {"workspace": "demo", "backend": "grok", "text": "continue", "conversation_id": cid})
        assert second.ok and second.data["run"]["status"] == "succeeded", second.error
        assert second.data["run"]["native"]["session_id"] == sid
        assert second.data["run"]["native"]["native_resumed"] is True
        calls = [json.loads(line) for line in (workspace_root / "calls.jsonl").read_text().splitlines()]
        assert len(calls) == 2 and calls[-1] == {"session": sid, "resumed": True}
    finally:
        app.close()


@pytest.mark.parametrize("change", ["home", "model", "workspace", "identity", "credentials", "binary", "missing", "symlink", "truncated", "external_turn", "unsupported"])
def test_mismatch_does_not_fall_back_to_new_or_spawn(isolated_env, workspace_root, monkeypatch, change):
    cfg = BackendConfig(name="grok", **options(isolated_env, monkeypatch))
    backend = GrokBackend(cfg)
    identity = {"account": "a", "runtime": "r"}
    first = backend.dispatch(TaskId("t1"), RunId("r1"), "", "start", cwd=workspace_root, native={"asterun_identity": identity})
    sid = first["native"]["session_id"]
    native = {"backend_session_id": sid, "asterun_identity": identity}
    cwd = workspace_root
    if change == "home":
        new_home = isolated_env / "other-home"
        prepare_home(new_home, execution_profile=CODE_PROFILE)
        cfg = replace(cfg, home=str(new_home))
    elif change == "model":cfg = replace(cfg, model="other")
    elif change == "identity":native["asterun_identity"] = {"account": "other", "runtime": "r"}
    elif change == "credentials":
        auth = Path(cfg.home) / 'auth.json';auth.write_text('{}');auth.chmod(0o600)
    elif change == "workspace":
        cwd = isolated_env / "other-workspace";cwd.mkdir()
    elif change in {"binary", "unsupported"}:
        p = Path(cfg.bin)
        p.write_text(p.read_text().replace("--session-id --resume", "--session-id") if change == "unsupported" else p.read_text()+"\n# changed\n")
    else:
        p = next(Path(cfg.home).glob('sessions/*/*/updates.jsonl'))
        if change == "missing":p.unlink()
        elif change == "truncated":p.write_text('{}')
        elif change == "external_turn":
            with p.open('a') as stream:stream.write('{}\n')
        else:
            p.unlink();p.symlink_to(workspace_root / "calls.jsonl")
    result = GrokBackend(cfg).dispatch_stream(TaskId("t2"), RunId("r2"), "", "next", cwd=cwd,
                                             native=native, publish=None, stopping=Event())
    assert result["status"] == "failed"
    assert result["native"]["asterun.grok_dispatch_v1"]["delivery"] == "not_sent"
    assert len((workspace_root / "calls.jsonl").read_text().splitlines()) == 1


def test_same_session_has_cross_backend_process_lock(isolated_env, workspace_root, monkeypatch):
    from asterun.backends.grok_session import binding, session_lease
    cfg = BackendConfig(name="grok", **options(isolated_env, monkeypatch))
    backend = GrokBackend(cfg)
    first = backend.dispatch(TaskId("t"), RunId("r"), "", "start", cwd=workspace_root)
    sid = first["native"]["session_id"]
    home = Path(cfg.home).resolve()
    bound = binding(cfg, home, workspace_root, Path(cfg.bin), {})
    with session_lease(home, workspace_root, sid, bound, resume=True):
        result = GrokBackend(cfg).dispatch(TaskId("t"), RunId("r2"), "", "next", cwd=workspace_root,
                                           native={"backend_session_id": sid})
    assert result["status"] == "failed" and result["error_code"] == "REMOTE_STATE_UNKNOWN"
    assert len((workspace_root / "calls.jsonl").read_text().splitlines()) == 1
