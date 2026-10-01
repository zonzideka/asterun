import hashlib
import json
from pathlib import Path
import sys
import shutil

import pytest

from asterun.application import Application
from asterun.cli import build_parser, _load_request
from asterun.config import load_config
from asterun.errors import AsterunError
from asterun.local_checks import validate_checks
from asterun.mcp_server import handle_rpc
from asterun.policy import Decision
from tests.conftest import write_config


def setup(isolated_env, workspace_root, source, *, result_file=False, timeout=5):
    if sys.platform != "darwin" and not shutil.which("bwrap"):
        pytest.skip("当前主机缺少 OS 禁网检查沙箱；不回退为无隔离执行")
    (workspace_root / "check.py").write_text(source)
    spec = {"argv": [str(Path(sys.executable).resolve()), "check.py"], "inputs": ["check.py", "note.txt"],
            "timeout_seconds": timeout, "runtime_roots": [str(Path(sys.base_prefix).resolve())]}
    if result_file:spec["result_file"] = "counts.json"
    config = write_config(isolated_env / "cfg.json", workspace_root, extra={"workflow": {"local_checks": {"compile": spec}}})
    app = Application(load_config(config))
    first = app.handle("task.submit", {"workspace": "demo", "text": "fix"})
    snap = app.handle("workflow.snapshot", {"task_id": first.ids["task_id"], "target_paths": spec["inputs"]}).data
    bound = {"task_id":snap["task_id"],"expected_run_id":snap["run_id"],"expected_revision":snap["revision"],
             "expected_input_hash":snap["input_hash"],"expected_target_hash":snap["target_hash"],"target_paths":snap["target_paths"]}
    return app,bound


def check(app,bound):
    result=app.handle("workflow.check",{**bound,"check_name":"compile"})
    assert result.ok,result.error
    return result.data


def test_os_sandbox_blocks_network_credentials_and_original_source(isolated_env,workspace_root,monkeypatch):
    monkeypatch.setenv("TOP_SECRET_TEST", "secret")
    outside=workspace_root/'unselected.txt';outside.write_text('private')
    code=f'''import os,pathlib,socket
assert 'TOP_SECRET_TEST' not in os.environ
assert pathlib.Path('note.txt').is_file()
for mode in ['r','w']:
 try:open({str(outside)!r},mode)
 except PermissionError:pass
 else:raise AssertionError('original workspace accessible')
s=socket.socket()
try:s.connect(('127.0.0.1',9))
except PermissionError:pass
else:raise AssertionError('network accessible')
print('isolation verified')
'''
    app,bound=setup(isolated_env,workspace_root,code)
    try:
        data=check(app,bound)
        assert data['passed'],data
        assert data['before_hash']==data['after_hash']
        assert outside.read_text()=='private'
        assert data['acceptance_unchanged'] and not data['independent_review']
    finally:app.close()


@pytest.mark.parametrize('counts,passed',[
    ({'passed':4,'failed':0,'skipped':1,'load_errors':0},True),
    ({'passed':4,'failed':0,'skipped':0,'load_errors':12},False),
    ({'passed':0,'failed':0,'skipped':5,'load_errors':0},False),
    ({'passed':4,'failed':1,'skipped':0,'load_errors':0},False),
    ({'failed':0},False),
])
def test_load_errors_and_empty_suites_cannot_pass(isolated_env,workspace_root,counts,passed):
    app,bound=setup(isolated_env,workspace_root,f"import pathlib\npathlib.Path('counts.json').write_text({json.dumps(counts)!r})",result_file=True)
    try:assert check(app,bound)['passed'] is passed
    finally:app.close()


def test_timeout_has_no_valid_success(isolated_env,workspace_root):
    app,bound=setup(isolated_env,workspace_root,'import time\ntime.sleep(10)',timeout=1)
    try:
        data=check(app,bound)
        assert not data['passed'] and data['timed_out']
    finally:app.close()


def test_cli_mcp_diagnostics_reach_idempotent_same_task_repair(isolated_env,workspace_root):
    app,bound=setup(isolated_env,workspace_root,"print('src/main.ts:4 TS2322 type mismatch; fixtures: missing expected case')\nraise SystemExit(2)")
    try:
        request=isolated_env/'request.json';request.write_text(json.dumps({**bound,'check_name':'compile'}))
        args=build_parser().parse_args(['workflow-check',bound['task_id'],'--request',str(request)])
        data=app.handle('workflow.check',_load_request(args))
        assert data.ok and not data.data['passed'],data.error
        rpc=handle_rpc(app,{'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'workflow_check','arguments':{**bound,'check_name':'compile'}}})
        assert not rpc.get('error'),rpc
        report=data.data['report'];raw=json.dumps(report).encode();(workspace_root/'report.json').write_bytes(raw)
        evaluation=app.handle('workflow.evaluate',{**bound,'checks':[{'kind':'external_report','path':'report.json','sha256':hashlib.sha256(raw).hexdigest()}]})
        assert evaluation.ok and evaluation.data['acceptance']=='failed'
        repair={**bound,'idempotency_key':'one-repair'}
        repaired=app.handle('workflow.repair',repair)
        assert repaired.ok,repaired.error
        assert 'TS2322' in repaired.data['run']['prompt']
        assert repaired.ids['task_id']==bound['task_id']
        repeated=app.handle('workflow.repair',repair)
        assert repeated.ok and repeated.ids['run_id']==repaired.ids['run_id']
    finally:app.close()


def test_permissions_target_drift_and_unconfigured_commands_fail_closed(isolated_env,workspace_root):
    app,bound=setup(isolated_env,workspace_root,"print('ok')")
    try:
        assert not app.handle('workflow.check',{**bound,'check_name':'not-configured'}).ok
        (workspace_root/'note.txt').write_text('changed')
        result=app.handle('workflow.check',{**bound,'check_name':'compile'})
        assert not result.ok and result.error['code']=='REVISION_CONFLICT'
        class Policy:
            def authorize(self,principal,action,resource):return Decision(action!='process.execute','SCOPE_DENIED')
        app.policy=Policy()
        assert app.handle('workflow.check',{**bound,'check_name':'compile'}).error['code']=='SCOPE_DENIED'
    finally:app.close()


@pytest.mark.parametrize('spec',[{'argv':'sh check.py','inputs':['a']},{'argv':['python3'],'inputs':['a']},
    {'argv':['/usr/bin/python3'],'inputs':['../escape']},{'argv':['/usr/bin/python3'],'inputs':['a'],'timeout_seconds':True},
    {'argv':['/usr/bin/python3'],'inputs':['a'],'runtime_roots':['/']}])
def test_invalid_configuration(spec):
    with pytest.raises(AsterunError):validate_checks({'test':spec})


def test_no_sandbox_never_falls_back_to_plain_subprocess(tmp_path, monkeypatch):
    from asterun.local_checks import sandbox_command
    monkeypatch.setattr(sys,'platform','unsupported')
    with pytest.raises(AsterunError,match='不会无隔离执行'):
        sandbox_command(['/usr/bin/true'],tmp_path,[])
