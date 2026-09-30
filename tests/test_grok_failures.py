import json
import sys
from threading import Event

import pytest

from asterun.application import Application
from asterun.backends.grok_code import classify_failure, run_code
from asterun.config import load_config
from asterun.contracts import RunStatus
from asterun.grok_quota import scopes, guard
from asterun.errors import AsterunError
from asterun.ids import RunId
from tests.conftest import write_config
from tests.test_grok_resume import options


@pytest.mark.parametrize("code,kind", [(402,"quota_exhausted"),(429,"rate_limited"),(401,"authentication_failed"),(403,"authentication_failed")])
@pytest.mark.parametrize("channel", ["stdout", "stderr", "event"])
def test_partial_edit_then_provider_failure_keeps_unknown(tmp_path, code, kind, channel):
    script = tmp_path / "fixture.py"
    diagnostic = f"unexpected HTTP status {code}: PRIVATE_TOKEN"
    script.write_text('import sys,json,pathlib\npathlib.Path("partial.txt").write_text("edited")\n'
        + (f'print(json.dumps({{"type":"error","message":{diagnostic!r}}}))\n' if channel == "event" else
           f'print({diagnostic!r},file=sys.{channel},flush=True)\n') + 'sys.exit(1)\n')
    result = run_code(command=[sys.executable,str(script)], env={}, cwd=tmp_path, session_id="test",
                      task_id="t",run_id="r",timeout_seconds=5,publish=None,stopping=Event())
    assert result["status"] == "pending_reconcile" and result["error_code"] == "REMOTE_STATE_UNKNOWN"
    assert result["native"]["provider_failure"]["kind"] == kind
    assert (tmp_path / "partial.txt").exists()
    assert "PRIVATE_TOKEN" not in json.dumps(result)


def test_incidental_number_is_not_quota_evidence():
    assert classify_failure("read file 402 lines") is None
    assert classify_failure("HTTP 400") is None


def test_multiline_non_json_error_tail_keeps_402(tmp_path):
    script=tmp_path/'failure.py'
    script.write_text("print('Error: upstream request failed', flush=True)\nprint('HTTP 402 Payment Required: PRIVATE', flush=True)\nraise SystemExit(1)\n")
    result=run_code(command=[sys.executable,str(script)],env={},cwd=tmp_path,session_id='test',
                    task_id='t',run_id='r',timeout_seconds=5,publish=None,stopping=Event())
    assert result['status']=='pending_reconcile'
    assert result['native']['provider_failure']['kind']=='quota_exhausted'
    assert 'PRIVATE' not in json.dumps(result)


def test_queued_runs_do_not_start_after_pool_exhaustion(isolated_env,workspace_root):
    from tests.test_grok_code_policy import config_file, open_app, ActionPolicy
    path=config_file(isolated_env,workspace_root,1,'workspace-code-v1')
    app,backend=open_app(path,ActionPolicy())
    # Use the protocol fixture as a long-running source and persist exactly the
    # same scoped diagnostic that the native adapter emits.
    from asterun.backends.fake import FakeBackend
    backend.dispatch=lambda *a,**kw: FakeBackend.dispatch(backend,*a,**kw)
    try:
        app.config.backends['worker'].home=str(isolated_env/'profile')
        first=app.handle('task.submit',{'workspace':'demo','backend':'worker','script':'cancel_accept_only'})
        second=app.handle('task.submit',{'workspace':'demo','backend':'worker','text':'queued'})
        assert first.ok and second.ok and second.data['queued']
        run=app.store.get_run(RunId(first.ids['run_id']))
        run.native.update(provider_failure={'kind':'quota_exhausted'},grok_quota_scopes=scopes(app,'worker'))
        run.status=RunStatus.PENDING_RECONCILE
        app.store.save_run(run)
        app.scheduler.finish('worker',run.id)
        app._drain_queue()
        queued=app.store.get_run(RunId(second.ids['run_id']))
        assert queued.status==RunStatus.FAILED and queued.error_code=='BUDGET_EXHAUSTED'
        assert backend.dispatch_count==1
        assert app.store.get_run(run.id).status==RunStatus.PENDING_RECONCILE
    finally:app.close()


def test_v1_quota_stop_survives_reopen_and_shared_profile_aliases(isolated_env, workspace_root, monkeypatch):
    opts = options(isolated_env, monkeypatch)
    cfg = write_config(isolated_env / "config.json",workspace_root,{"grok":opts,"alias":opts})
    state = isolated_env / "state"
    app = Application.from_paths(cfg,state)
    first = app.handle("task.submit",{"workspace":"demo","backend":"grok","text":"start"})
    assert first.ok
    run = app.store.get_run(RunId(first.ids["run_id"]))
    run.native["provider_failure"] = {"kind":"quota_exhausted","status_code":402}
    run.status = RunStatus.PENDING_RECONCILE
    app.store.save_run(run)
    app.close()
    app = Application.from_paths(cfg,state)
    try:
        denied = app.handle("task.submit",{"workspace":"demo","backend":"alias","text":"new"})
        assert not denied.ok and denied.error["code"] == "BUDGET_EXHAUSTED"
        assert app.backends["alias"].dispatch_count == 0
        assert app.store.get_run(run.id).status == RunStatus.PENDING_RECONCILE
        app.config.backends["alias"].quota_epoch = "confirmed-next-window"
        guard(app,"alias")
    finally:app.close()


def test_shared_v2_pool_gate_applies_to_other_connection(isolated_env, workspace_root, monkeypatch):
    from asterun.config_migration import migrate_v1_to_v2
    opts=options(isolated_env,monkeypatch)
    cfg=write_config(isolated_env/'v1.json',workspace_root,{'grok':opts})
    raw=migrate_v1_to_v2(cfg)['config']
    cfg.write_text(json.dumps(raw))
    app=Application.from_paths(cfg,isolated_env/'state')
    try:
        result=app.handle('task.submit',{'workspace':'demo','text':'test'})
        assert result.ok
        run=app.store.get_run(RunId(result.ids['run_id']))
        # The gate consumes persisted native classification and the immutable
        # dispatch-time pool/window identities, never a current alias guess.
        run.native.update(grok_quota_scopes=scopes(app,'fake'),provider_failure={'kind':'quota_exhausted'})
        app.store.save_run(run)
        conn=app.config.connections[app.config.backends['fake'].connection_ref]
        other=app.config.connections[app.config.backends['grok'].connection_ref]
        other['billing_pool_refs']=conn['billing_pool_refs']
        with pytest.raises(AsterunError,match='402'):guard(app,'grok')
        pool=app.config.billing_pools[conn['billing_pool_refs'][0]]
        pool['window_id']='verified-new-window'
        guard(app,'grok')
    finally:app.close()
