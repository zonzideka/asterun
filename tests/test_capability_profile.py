"""P08 新 profile 与冻结 RCv1 共用持久控制服务和质量引擎。"""
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from uuid import uuid4
import pytest
from asterun.application import Application
from asterun.config_migration import migrate_v1_to_v2
from asterun.control import identifier
from asterun.policy import DenyAll,Principal
from asterun.mcp_server import handle_rpc
from tests.conftest import write_config
from tests.test_plugin_execution import plugin_config
from tests.test_plugin_admission import v2_config
from tests.test_quality_runtime import ReviewingFake,RepairingFake
from tests.test_control_runtime import create_command,start_command
PROFILE='asterun-capability/v1'
BASE={'schema_version':PROFILE,'namespace_id':'ns_local'}

def request(backend='fake',cap='agent.execute',data=None,quality=None,key=None):
    return {**BASE,'workspace':'demo','backend':backend,'capability':cap,'input':data or {'text':'code task'},
            'workflow_id':'wf_code_change' if quality else 'wf_capability_call','idempotency_key':key or uuid4().hex,
            **({'quality':quality} if quality else {})}

def prepare(app,payload=None):
    result=app.handle('capability.prepare',payload or request());assert result.ok,result.to_dict()
    return result.data['plan']

def submit(app,plan,key=None):
    req={**BASE,'plan_id':plan['id'],'plan_sha256':plan['plan_sha256'],'idempotency_key':key or uuid4().hex}
    result=app.handle('capability.submit',req);assert result.ok,result.to_dict()
    return result.data,req

def finish(app,run_id):
    for _ in range(15):
        app.poll()
        run=app.control.get('Run',run_id)
        if run['status'] in {'SUCCEEDED','FAILED','CANCELED'}:return run
    return app.control.get('Run',run_id)


def test_prepare_is_static_submit_atomic_and_original_key_readback(v2_config):
    with closing(Application.from_paths(v2_config,v2_config.parent/'state')) as app:
        req=request();plan=prepare(app,req)
        assert app.handle('capability.prepare',req).data['plan']==plan
        assert app.backends['fake'].dispatch_count==0 and app.control.store.list('Task','ns_local')==[]
        assert app.store._conn.execute('SELECT COUNT(*) FROM plugin_pool_reservations').fetchone()[0]==0
        result,submit_req=submit(app,plan)
        assert app.backends['fake'].dispatch_count==0
        assert len(app.control.store.pending('ns_local'))==1
        assert app.handle('capability.submit',submit_req).data==result
        assert finish(app,result['run_id'])['status']=='SUCCEEDED'
        assert app.backends['fake'].dispatch_count==1
        app.policy=DenyAll();assert not app.handle('capability.submit',submit_req).ok


def test_generic_external_capability_preserves_typed_output(plugin_config):
    with closing(Application.from_paths(plugin_config,plugin_config.parent/'state')) as app:
        plan=prepare(app,request('external','local.echo',{'text':'typed value','mode':'success'}))
        result,_=submit(app,plan);run=finish(app,result['run_id']);assert run['status']=='SUCCEEDED',run
        task=app.control.get('Task',result['task_id']);artifact=app.control.get('Artifact',task['result_artifact_ids'][0])
        assert json.loads(app.control.store.get_blob('ns_local',artifact['sha256']))=={'text':'typed value'}
        assert len(app.control.store.list('Action','ns_local'))==1

@pytest.mark.parametrize('change',['model','auth','pool','config','account','workspace'])
def test_plan_configuration_drift_refuses_submit(v2_config,change):
    with closing(Application.from_paths(v2_config,v2_config.parent/'state')) as app:
        plan=prepare(app)
        connection=next(iter(app.config.connections.values()))
        if change=='model':connection['options']['model']='changed'
        if change=='auth':connection['auth_mode']='changed'
        if change=='pool':next(iter(app.config.billing_pools.values()))['local_limit']=999
        if change=='config':app.config.revision+=1
        if change=='account':connection['provider_account_ref']='changed'
        if change=='workspace':app.config.workspaces['demo']=replace(app.config.workspaces['demo'],root=Path('/tmp'))
        result=app.handle('capability.submit',{**BASE,'plan_id':plan['id'],'plan_sha256':plan['plan_sha256'],'idempotency_key':uuid4().hex})
        assert not result.ok,result.to_dict()
        assert app.control.store.list('Task','ns_local')==[] and app.backends['fake'].dispatch_count==0


def test_expired_plan_and_wrong_confirmation_are_refused(v2_config):
    with closing(Application.from_paths(v2_config,v2_config.parent/'state')) as app:
        plan=prepare(app)
        result=app.handle('capability.submit',{**BASE,'plan_id':plan['id'],'plan_sha256':'0'*64,'idempotency_key':uuid4().hex})
        assert not result.ok
        operation=app.control.store.get('Operation',plan['id'],'ns_local')
        p=operation['extensions']['asterun.capability_plan'];p['expires_at']='2000-01-01T00:00:00Z';p.pop('plan_sha256')
        from asterun.control_protocol import digest
        p['plan_sha256']=digest(p);app.control.save(operation,update=True)
        result=app.handle('capability.submit',{**BASE,'plan_id':p['id'],'plan_sha256':p['plan_sha256'],'idempotency_key':uuid4().hex})
        assert not result.ok and result.error['code']=='REVISION_CONFLICT',result.to_dict()


def test_submit_rollback_preserves_reusable_prepared_plan(v2_config,monkeypatch):
    with closing(Application.from_paths(v2_config,v2_config.parent/'state')) as app:
        plan=prepare(app);original=app.control.store.enqueue
        def fail(*args,**kwargs):
            from asterun.errors import AsterunError
            raise AsterunError('BUDGET_EXHAUSTED','fixture after admission')
        monkeypatch.setattr(app.control.store,'enqueue',fail)
        result=app.handle('capability.submit',{**BASE,'plan_id':plan['id'],'plan_sha256':plan['plan_sha256'],'idempotency_key':uuid4().hex})
        assert not result.ok and app.control.store.list('Task','ns_local')==[]
        assert app.store._conn.execute('SELECT COUNT(*) FROM plugin_pool_reservations').fetchone()[0]==0
        monkeypatch.setattr(app.control.store,'enqueue',original);submit(app,plan)


def test_profile_method_namespace_principal_idempotency_isolation(v2_config):
    with closing(Application.from_paths(v2_config,v2_config.parent/'state')) as app:
        key='same-key-across-profile';plan=prepare(app,request(key=key))
        created=create_command(app.control);created['idempotency_key']=key
        assert app.control.command(created)['status']=='SUCCEEDED'
        result,_=submit(app,plan,key)
        changed=request(key=key,data={'text':'different'});assert not app.handle('capability.prepare',changed).ok
        alien=request(key=key);alien['namespace_id']='ns_other';assert not app.handle('capability.prepare',alien).ok
        app.principal=Principal('different','test')
        denied=app.handle('capability.submit',{**BASE,'plan_id':plan['id'],'plan_sha256':plan['plan_sha256'],'idempotency_key':uuid4().hex})
        assert not denied.ok


def test_unknown_after_restart_is_not_resubmitted(plugin_config):
    state=plugin_config.parent/'state'
    with closing(Application.from_paths(plugin_config,state)) as app:
        plan=prepare(app,request('external','local.echo',{'text':'unknown','mode':'crash'}));result,req=submit(app,plan)
        run=finish(app,result['run_id']);assert run['status']=='RECONCILING',run
        assert app.backends['external'].dispatch_count==1
    with closing(Application.from_paths(plugin_config,state)) as app:
        assert app.handle('capability.submit',req).data==result
        app.poll();assert app.backends['external'].dispatch_count==0
        assert app.control.get('Run',result['run_id'])['status']=='RECONCILING'


def quality_app(root,workspace,mode='pass',reviewer_enabled=True,max_repairs=1):
    old=write_config(root/'old.json',workspace,extra_backends={'reviewer':{'kind':'fake','enabled':reviewer_enabled}},extra={
        'workflow':{'require_review':True,'review_backend':'reviewer','max_repairs':max_repairs},'scheduler':{'max_runs_per_task':8}})
    path=root/'v2.json';path.write_text(json.dumps(migrate_v1_to_v2(old)['config']))
    app=Application.from_paths(path,root/'state');app.backends['fake']=RepairingFake(app.config.backends['fake']);reviewer=ReviewingFake(app.config.backends['reviewer'],mode=mode);app.backends['reviewer']=reviewer
    return app,reviewer,path


def w1():return {'target_paths':['note.txt'],'checks':[{'kind':'file_exists','path':'note.txt'}],'max_repairs':1}

@pytest.mark.parametrize('mode,expected',[('pass','SUCCEEDED'),('repair','SUCCEEDED'),('finding','WAITING'),('drift','WAITING'),('unknown','WAITING')])
def test_w1_quality_uses_same_action_budget_and_bound_review(isolated_env,workspace_root,mode,expected):
    app,reviewer,_=quality_app(isolated_env,workspace_root,mode)
    with closing(app):
        plan=prepare(app,request(quality=w1()));result,_=submit(app,plan);run=finish(app,result['run_id'])
        assert run['status']==expected,run
        actions=app.control.store.list('Action','ns_local')
        assert len(actions)>=2 and len(run['assignment_ids'])==len(actions)
        assert len(app.control.store.list('Grant','ns_local'))==len(actions)
        assert len(app.control.store.list('Reservation','ns_local'))==len(actions)
        assert len(reviewer.contexts)>=1
        assignments=[app.control.get('Assignment',a) for a in run['assignment_ids']]
        assert all(a['session_ids'] for a in assignments)
        sessions=[app.control.get('Session',a['session_ids'][0]) for a in assignments]
        assert sessions[0]['extensions']['asterun.legacy_conversation_id']!=sessions[1]['extensions']['asterun.legacy_conversation_id']
        response=app.handle('capability.get',{**BASE,'run_id':run['id']});assert response.ok,response.to_dict()
        if expected=='SUCCEEDED':
            assert response.data['quality_verdict']=='PASS'
            evidence=[app.control.get('Evidence',e) for e in run['completion_evidence_ids']]
            assert any(e['validator_id']=='val_code_quality' and e['verdict']=='PASS' for e in evidence)
            assert all(a['status']=='SUCCEEDED' for a in actions)
            assert len(actions)==(4 if mode=='repair' else 2)
            (workspace_root/'note.txt').write_text('external mutation')
            assert app.handle('capability.get',{**BASE,'run_id':run['id']}).data['quality_verdict']=='STALE'
        else:assert response.data['quality_verdict']!='PASS'


def test_w1_missing_reviewer_waits_without_quality_success(isolated_env,workspace_root):
    app,reviewer,_=quality_app(isolated_env,workspace_root,reviewer_enabled=False)
    with closing(app):
        result,_=submit(app,prepare(app,request(quality=w1())))
        run=finish(app,result['run_id']);assert run['status']=='WAITING',run
        assert reviewer.contexts==[] and run['completion_evidence_ids']==[]


def test_w1_changed_baseline_rejected_before_dispatch(isolated_env,workspace_root):
    app,_,_=quality_app(isolated_env,workspace_root)
    with closing(app):
        plan=prepare(app,request(quality=w1()));(workspace_root/'note.txt').write_text('changed')
        result=app.handle('capability.submit',{**BASE,'plan_id':plan['id'],'plan_sha256':plan['plan_sha256'],'idempotency_key':uuid4().hex})
        assert not result.ok and app.backends['fake'].dispatch_count==0


def test_rcv1_frozen_workflow_does_not_accept_w1(v2_config):
    with closing(Application.from_paths(v2_config,v2_config.parent/'state')) as app:
        response=app.control.command(create_command(app.control));task=app.control.get('Task',response['result_ref'])
        command=start_command(app.control,task);command['params']['workflow_id']='wf_code_change'
        from asterun.errors import AsterunError
        with pytest.raises(AsterunError):app.control.command(command)
        root=Path(__file__).parents[1]/'src/asterun/schemas'
        assert hashlib.sha256((root/'runner-control-v1.schema.json').read_bytes()).hexdigest()=='462f0956ca7f7997cc472ffb73e619111c66f25318cb383b677213880f23fe76'
        assert hashlib.sha256((root/'state-machines.json').read_bytes()).hexdigest()=='99ceb7f0b8a30160d06085d0c2b645e618506a393941a7ebf78ff5c3bfa5ebb6'
        tools=handle_rpc(app,{'jsonrpc':'2.0','id':1,'method':'tools/list'})['result']['tools']
        assert any(t['name']=='capability_prepare' for t in tools)
        assert app.handle('capability.discovery').data['schema_version']==PROFILE


def test_same_plan_cannot_create_second_execution_and_restart_before_delivery(v2_config):
    state=v2_config.parent/'state'
    with closing(Application.from_paths(v2_config,state)) as app:
        plan=prepare(app);result,req=submit(app,plan)
        denied=app.handle('capability.submit',{**req,'idempotency_key':uuid4().hex})
        assert not denied.ok and len(app.control.store.list('Task','ns_local'))==1
    with closing(Application.from_paths(v2_config,state)) as app:
        assert app.handle('capability.submit',req).data==result
        read=app.handle('capability.get',{**BASE,'run_id':result['run_id']});assert read.ok,read.to_dict()
        assert read.data['run']['status']=='SUCCEEDED'
        assert app.backends['fake'].dispatch_count==1


def test_w1_baseline_changed_after_admission_refuses_dispatch(isolated_env,workspace_root):
    app,_,_=quality_app(isolated_env,workspace_root)
    with closing(app):
        result,_=submit(app,prepare(app,request(quality=w1())))
        (workspace_root/'note.txt').write_text('changed after submit')
        run=finish(app,result['run_id']);assert run['status']=='FAILED',run
        assert app.backends['fake'].dispatch_count==0


def test_w1_pause_resume_and_cancel_preserve_quality_intent(isolated_env,workspace_root):
    from tests.test_control_runtime import command
    app,reviewer,_=quality_app(isolated_env,workspace_root)
    with closing(app):
        result,_=submit(app,prepare(app,request(quality=w1())))
        app.poll()  # 已执行实现与审查，但核心质量完成尚未变成标准成功。
        run=app.control.get('Run',result['run_id']);assert run['status']=='WAITING',run
        app.control.command(command('run.pause',{'run_id':run['id'],'reason':'fixture pause'},run['revision']))
        app.poll();run=app.control.get('Run',run['id']);assert run['status']=='PAUSED',run
        count=len(reviewer.contexts)
        app.control.command(command('run.resume',{'run_id':run['id'],'capsule_id':None},run['revision']))
        run=finish(app,run['id']);assert run['status']=='SUCCEEDED',run
        assert len(reviewer.contexts)==count


def test_w1_cancel_unknown_review_does_not_claim_remote_terminal(isolated_env,workspace_root):
    from tests.test_control_runtime import command
    app,reviewer,_=quality_app(isolated_env,workspace_root,mode='unknown')
    with closing(app):
        result,_=submit(app,prepare(app,request(quality=w1())))
        app.poll();run=app.control.get('Run',result['run_id'])
        # 未证实取消的适配回执保持 unknown。
        reviewer.cancel=lambda *a,**k:{'status':'pending_reconcile','terminated':False}
        reviewer.reconcile=lambda *a,**k:{'status':'pending_reconcile','terminated':False}
        app.control.command(command('run.cancel',{'run_id':run['id'],'reason':'fixture cancel'},run['revision']))
        app.poll();run=app.control.get('Run',run['id']);assert run['status']=='CANCELING',run
        assert len(reviewer.contexts)==1
        assert any(a['status']=='UNKNOWN_REMOTE' for a in app.control.store.list('Action','ns_local'))


def test_w1_restart_keeps_quality_actions_and_no_duplicate_review(isolated_env,workspace_root):
    app,reviewer,path=quality_app(isolated_env,workspace_root)
    with closing(app):
        result,req=submit(app,prepare(app,request(quality=w1())))
        app.poll();assert len(reviewer.contexts)==1
    with closing(Application.from_paths(path,isolated_env/'state')) as app:
        fresh=ReviewingFake(app.config.backends['reviewer']);app.backends['reviewer']=fresh
        assert app.handle('capability.submit',req).data==result
        run=finish(app,result['run_id']);assert run['status']=='SUCCEEDED',run
        assert fresh.contexts==[]
        assert len(app.control.store.list('Action','ns_local'))==2

@pytest.mark.parametrize('completed',[False,True])
def test_prepared_plan_and_quality_graph_survive_independent_restore(isolated_env,workspace_root,completed):
    from asterun.backup import backup_state,restore_state
    app,_,path=quality_app(isolated_env,workspace_root)
    archive=isolated_env/'w1-backup.tar.gz';target=isolated_env/'restored'
    with closing(app):
        result,req=submit(app,prepare(app,request(quality=w1())))
        if completed:assert finish(app,result['run_id'])['status']=='SUCCEEDED'
        expected={kind:app.control.store.list(kind,'ns_local') for kind in ['Task','Run','Assignment','Action','Grant','Reservation','Evidence']}
        backup_state(app.state_dir,archive)
    restore_state(archive,target)
    with closing(Application.from_paths(path,target)) as app:
        for kind,rows in expected.items():assert app.control.store.list(kind,'ns_local')==rows
        app.backends['reviewer']=ReviewingFake(app.config.backends['reviewer'])
        assert app.handle('capability.submit',req).data==result
        run=finish(app,result['run_id']);assert run['status']=='SUCCEEDED',run
        assert app.backends['fake'].dispatch_count==(0 if completed else 1)
        assert len(app.control.store.list('Action','ns_local'))==2
