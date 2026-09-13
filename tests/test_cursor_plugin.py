"""P06：默认独立 SDK 接缝；可指定本地 wheelhouse 重跑同一套真实 SDK + HTTP fixture。"""
from copy import deepcopy
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from asterun.application import Application
from asterun.plugins.registry import PluginRegistry, installation_digest
from asterun.plugins.worker import WorkerHost
from tests.test_plugin_execution import external_config

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'packages/asterun-plugin-cursor'

@pytest.fixture(scope='module')
def cursor_installation(tmp_path_factory):
    root = tmp_path_factory.mktemp('cursor-plugin').resolve()
    env = {'PATH':str(Path(sys.executable).parent)+':/usr/bin:/bin','HOME':str(root/'home'),
           'PIP_NO_INDEX':'1','PIP_DISABLE_PIP_VERSION_CHECK':'1','PYTHONDONTWRITEBYTECODE':'1'}
    (root/'home').mkdir()
    def call(args):
        result=subprocess.run(args,cwd=root,env=env,capture_output=True,text=True,timeout=90)
        assert result.returncode==0,result.stdout+result.stderr
        return result.stdout.strip()
    source=root/'source';shutil.copytree(PACKAGE,source)
    call([sys.executable,'-m','pip','wheel','--no-build-isolation','--no-deps','-w',str(root/'wheels'),str(source)])
    wheel=next((root/'wheels').glob('*.whl'))
    call([sys.executable,'-m','venv',str(root/'venv')]);python=root/'venv/bin/python'
    call([str(python),'-m','pip','install','--no-deps',str(wheel)])
    library=Path(call([str(python),'-c','import sysconfig; print(sysconfig.get_path("purelib"))']))
    wheelhouse=os.environ.get('ASTERUN_TEST_CURSOR_WHEELHOUSE')
    if wheelhouse:
        call([str(python),'-m','pip','install','--no-index','--find-links',wheelhouse,'cursor-sdk==1.0.31','httpx==0.28.1'])
        call([str(python),'-m','pip','check'])
    else:
        shutil.copytree(ROOT/'tests/fixtures/cursor_sdk_stub',library/'cursor_sdk')
        metadata=library/'cursor_sdk-1.0.31.dist-info';metadata.mkdir()
        (metadata/'METADATA').write_text('Metadata-Version: 2.1\nName: cursor-sdk\nVersion: 1.0.31\n')
    assert call([str(python),'-c','import importlib.util; print(importlib.util.find_spec("asterun") is None)'])=='True'
    return {'wheel':wheel,'sha256':hashlib.sha256(wheel.read_bytes()).hexdigest(),
        'manifest':library/'asterun_plugin_cursor/manifest.json','runtime_path':library,
        'runner':[str(python),'-m','asterun_plugin_cursor.worker'],'python':python,'env':env,'real_sdk':bool(wheelhouse)}


def setup(root,workspace,installation,mode='happy'):
    root=root.resolve();workspace=workspace.resolve();state=root/'sdk-state';state.mkdir(mode=0o700)
    (state/'mode').write_text(mode)
    binary=root/'bridge-fixture';binary.write_text('#!'+str(installation['python'])+'\n'+(ROOT/'tests/fixtures/cursor_bridge.py').read_text());binary.chmod(0o700)
    options={'runtime':'local','model':'fixture-model','state_root':str(state),'bridge_bin':str(binary),
             'bridge_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),'execution_enabled':True,
             'tools':[],'disallowed_tools':['Shell'],'mcp_servers':{},'setting_sources':[],'timeout_seconds':3}
    context={'run_id':'core-run-1','task_id':'task-fixture','binding_id':'binding-fixture','plan_id':'plan-fixture',
             'provider_account_ref':'cursor-fixture','workspace_root':str(workspace),'capability':'agent.execute',
             'auth_mode':'user_api_key','upstream_version':'1.0.31','options':options}
    return state,context


def host(installation):
    registry=PluginRegistry();registration=registry.register_external(installation['manifest'],
        runner=installation['runner'],installation_path=installation['wheel'],installation_sha256=installation['sha256'],
        runtime_path=installation['runtime_path'],runtime_sha256=installation_digest(installation['runtime_path']),enabled=True)
    return WorkerHost(registry,registration)


def rpc(installation,context,method='execution.start'):
    return host(installation).call(method,{'text':'fixture text'},context,cwd=context['workspace_root'],
        environment={'CURSOR_API_KEY':'cursor-offline-fixture-key'},secrets=('cursor-offline-fixture-key',)).result


def calls(state):
    return [json.loads(row) for row in (state/'calls.jsonl').read_text().splitlines()] if (state/'calls.jsonl').exists() else []


def configure(root,workspace,installation,context):
    path=external_config(root,workspace,installation);raw=json.loads(path.read_text())
    raw['plugins']['cursor.agent']=raw['plugins'].pop('example.external-fake')
    connection=raw['connections']['external'];connection.update(plugin_id='cursor.agent',auth_mode='user_api_key',upstream_version='1.0.31',options=context['options'])
    secret=root/'fake-key';secret.write_text('cursor-offline-fixture-key');secret.chmod(0o600)
    raw['provider_accounts'][connection['provider_account_ref']].update(provider='cursor',credential_ref='file:'+str(secret.resolve()))
    pool=raw['billing_pools'][connection['billing_pool_refs'][0]]
    pool.update(billing_mode='subscription',remaining=None,overage_verified=True,allow_unknown_subscription=True,
                meter='tokens',local_limit=100,estimate_per_action=30)
    path.write_text(json.dumps(raw));return path


def test_static_worker_does_not_launch_sdk_or_resolve_accounts(cursor_installation,tmp_path):
    result=host(cursor_installation).call('plugin.describe',{}, {},cwd=tmp_path).result
    assert result['manifest']['plugin_id']=='cursor.agent'
    inspected=host(cursor_installation).call('connection.inspect',{}, {},cwd=tmp_path).result
    assert inspected['authenticated']=='not_verified'
    assert host(cursor_installation).call('usage.observe',{}, {},cwd=tmp_path).result['remaining'] is None
    assert not list(tmp_path.iterdir())


def test_create_resume_replays_permissions_and_exact_ids(cursor_installation,isolated_env,workspace_root):
    state,context=setup(isolated_env,workspace_root,cursor_installation)
    first=rpc(cursor_installation,context)
    assert first['status']=='succeeded',first
    assert first['provider_session_id']=='agent-fixture' and first['provider_turn_id']=='run-fixture-1'
    assert first['usage'][0]['scope']=='run' and first['usage'][0]['used']==10
    resume=deepcopy(context);resume.update(run_id='core-run-2',provider_session_id=first['provider_session_id'],previous_provider_turn_id=first['provider_turn_id'])
    second=rpc(cursor_installation,resume)
    assert second['status']=='succeeded' and second['provider_turn_id']=='run-fixture-2',second
    assert second['provider_session_id']==first['provider_session_id']
    rows=calls(state);created=next(r['body']['options'] for r in rows if r['method']=='CreateAgent')
    restored=next(r['body']['options'] for r in rows if r['method']=='ResumeAgent')
    assert created==restored
    assert restored['tools']=={'names':[]} and restored['disallowedTools']==['Shell']
    assert restored.get('mcpServers',{})=={} and restored['local'].get('settingSources',[])==[]
    assert json.loads((state/'bridge-environment.json').read_text())['no_account_keys']
    assert 'cursor-offline-fixture-key' not in (state/'calls.jsonl').read_text()+(state/'native.json').read_text()


@pytest.mark.parametrize('field,value,reason',[
    ('execution_enabled',False,'execution_not_enabled'),('runtime','cloud','local_runtime_required'),
    ('tools',None,'tool_snapshot_required'),('disallowed_tools',None,'tool_snapshot_required'),
    ('mcp_servers',None,'inline_mcp_not_supported'),('mcp_servers',{'server':{}},'inline_mcp_not_supported'),
    ('setting_sources',['all'],'ambient_settings_forbidden'),('model','auto','fixed_model_required'),
    ('bridge_sha256','0'*64,'bridge_digest_mismatch'),('timeout_seconds',0,'invalid_timeout'),
])
def test_snapshot_failure_never_spawns_bridge(cursor_installation,isolated_env,workspace_root,field,value,reason):
    state,context=setup(isolated_env,workspace_root,cursor_installation);context['options'][field]=value
    result=rpc(cursor_installation,context)
    assert result['status']=='failed' and result['output']['text']==reason,result
    assert not calls(state)


@pytest.mark.parametrize('field,value,reason',[
    ('auth_mode','api_key','auth_mode_mismatch'),('upstream_version','1.0.30','sdk_version_mismatch'),
    ('capability','cloud.execute','capability_unsupported'),('binding_id','','execution_binding_required'),
    ('provider_session_id','bc-cloud','native_reference_invalid'),
])
def test_context_binding_rejected_before_bridge(cursor_installation,isolated_env,workspace_root,field,value,reason):
    state,context=setup(isolated_env,workspace_root,cursor_installation);context[field]=value
    result=rpc(cursor_installation,context)
    assert result['status']=='failed' and result['output']['text']==reason,result
    assert not calls(state)


@pytest.mark.parametrize('mode,status', [('error','failed'),('eof','pending_reconcile'),('create_timeout','pending_reconcile'),
    ('model_mismatch','pending_reconcile'),('unexpected_tool','pending_reconcile'),('model_missing','failed')])
def test_errors_do_not_retry_or_fabricate_success(cursor_installation,isolated_env,workspace_root,mode,status):
    state,context=setup(isolated_env,workspace_root,cursor_installation,mode)
    result=rpc(cursor_installation,context)
    assert result['status']==status,result
    assert sum(r['method']=='Send' for r in calls(state))<=1
    assert 'private diagnostic' not in json.dumps(result)


def test_tool_failure_is_not_success(cursor_installation,isolated_env,workspace_root):
    state,context=setup(isolated_env,workspace_root,cursor_installation,'tool_error');context['options']['tools']=['Read']
    result=rpc(cursor_installation,context)
    assert result['status']=='failed',result
    assert 'private diagnostic' not in json.dumps(result)


@pytest.mark.parametrize('mode,status', [('eof','cancelled'),('cancel_unconfirmed','pending_reconcile')])
def test_cancel_requires_observed_terminal(cursor_installation,isolated_env,workspace_root,mode,status):
    state,context=setup(isolated_env,workspace_root,cursor_installation,mode)
    first=rpc(cursor_installation,context)
    context.update({k:first[k] for k in ('provider_session_id','provider_turn_id')})
    result=rpc(cursor_installation,context,'execution.cancel')
    assert result['status']==status,result
    assert sum(r['method']=='Send' for r in calls(state))==1
    assert sum(r['method']=='CancelRun' for r in calls(state))==1


def test_application_cross_restart_resume_and_shared_usage(cursor_installation,isolated_env,workspace_root):
    state,context=setup(isolated_env,workspace_root,cursor_installation)
    path=configure(isolated_env,workspace_root,cursor_installation,context)
    with closing(Application.from_paths(path,isolated_env/'state')) as app:
        first=app.handle('task.submit',{'workspace':'demo','text':'first'})
        assert first.ok and first.data['run']['status']=='succeeded',first.to_dict()
        conversation=first.ids['conversation_id']
    with closing(Application.from_paths(path,isolated_env/'state')) as app:
        checked=app.handle('session.resume',{'conversation_id':conversation,'native':True})
        assert checked.ok and checked.data['resume_ready'] and not checked.data['native_resumed'],checked.to_dict()
        second=app.handle('task.submit',{'workspace':'demo','text':'second','conversation_id':conversation,'idempotency_key':'second'})
        assert second.ok and second.data['run']['status']=='succeeded',second.to_dict()
        assert app.handle('task.submit',{'workspace':'demo','text':'second','conversation_id':conversation,'idempotency_key':'second'}).ids==second.ids
        pool=next(iter(app.config.billing_pools))
        observations=app.admission.store.observations(pool)
        assert len(observations)==2 and all(r['observation']['used']==10 for r in observations)
    assert sum(r['method']=='CreateAgent' for r in calls(state))==1
    assert sum(r['method']=='ResumeAgent' for r in calls(state))==1
    assert sum(r['method']=='Send' for r in calls(state))==2


def test_unknown_restart_disable_reconcile_no_resend(cursor_installation,isolated_env,workspace_root):
    state,context=setup(isolated_env,workspace_root,cursor_installation,'eof')
    path=configure(isolated_env,workspace_root,cursor_installation,context)
    with closing(Application.from_paths(path,isolated_env/'state')) as app:
        first=app.handle('task.submit',{'workspace':'demo','text':'first'})
        assert first.ok and first.data['run']['status']=='pending_reconcile',first.to_dict()
        assert app.handle('plugin.disable',{'plugin_id':'cursor.agent'}).ok
    with closing(Application.from_paths(path,isolated_env/'state')) as app:
        reconciled=app.handle('task.reconcile',{'task_id':first.ids['task_id']});assert reconciled.ok,reconciled.to_dict()
        assert app.admission.store.binding_for_run(first.ids['run_id'])['status']=='uncertain'
        cancelled=app.handle('task.cancel',{'task_id':first.ids['task_id']});assert cancelled.ok,cancelled.to_dict()
    assert sum(r['method']=='Send' for r in calls(state))==1


def test_resume_changed_configuration_is_rejected(cursor_installation,isolated_env,workspace_root):
    state,context=setup(isolated_env,workspace_root,cursor_installation)
    path=configure(isolated_env,workspace_root,cursor_installation,context)
    with closing(Application.from_paths(path,isolated_env/'state')) as app:
        first=app.handle('task.submit',{'workspace':'demo','text':'first'});assert first.ok
    raw=json.loads(path.read_text());raw['connections']['external']['options']['tools']=['Read'];path.write_text(json.dumps(raw))
    with closing(Application.from_paths(path,isolated_env/'state')) as app:
        result=app.handle('task.submit',{'workspace':'demo','text':'second','conversation_id':first.ids['conversation_id']})
        assert not result.ok and result.error['code']=='BINDING_MISMATCH',result.to_dict()
    assert sum(r['method']=='Send' for r in calls(state))==1


@pytest.mark.parametrize('mode,reason', [('newer_turn','native_latest_turn_changed'),
    ('workspace_mismatch','native_workspace_mismatch'),('get_identity_mismatch','native_identity_mismatch')])
def test_native_drift_refuses_resume_without_send(cursor_installation,isolated_env,workspace_root,mode,reason):
    state,context=setup(isolated_env,workspace_root,cursor_installation)
    first=rpc(cursor_installation,context);assert first['status']=='succeeded',first
    (state/'mode').write_text(mode)
    context.update(run_id='core-run-2',provider_session_id=first['provider_session_id'],previous_provider_turn_id=first['provider_turn_id'])
    result=rpc(cursor_installation,context)
    assert result['status']=='failed' and result['output']['text']==reason,result
    assert sum(row['method']=='Send' for row in calls(state))==1
    assert not any(row['method']=='ResumeAgent' for row in calls(state))
