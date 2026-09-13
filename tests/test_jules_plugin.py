"""P07 独立 wheel/worker 与仅回环 REST fixture；不接触真实供应商。"""
from contextlib import closing
from copy import deepcopy
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading
import urllib.parse
import pytest
from asterun.application import Application
from tests.test_cursor_plugin import host
from tests.test_plugin_execution import external_config
ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture(scope='module')
def jules_installation(tmp_path_factory):
    root=tmp_path_factory.mktemp('jules-package').resolve();(root/'home').mkdir()
    env={'HOME':str(root/'home'),'PATH':str(Path(sys.executable).parent)+':/usr/bin:/bin','PIP_NO_INDEX':'1','PIP_DISABLE_PIP_VERSION_CHECK':'1','PYTHONDONTWRITEBYTECODE':'1'}
    def call(args):
        result=subprocess.run(args,env=env,capture_output=True,text=True,timeout=60);assert result.returncode==0,result.stdout+result.stderr
        return result.stdout.strip()
    source=root/'source';shutil.copytree(ROOT/'packages/asterun-plugin-jules',source)
    call([sys.executable,'-m','pip','wheel','--no-build-isolation','--no-deps','-w',str(root/'wheels'),str(source)])
    wheel=next((root/'wheels').glob('*.whl'));call([sys.executable,'-m','venv',str(root/'venv')]);python=root/'venv/bin/python'
    call([str(python),'-m','pip','install','--no-deps',str(wheel)]);call([str(python),'-m','pip','check'])
    library=Path(call([str(python),'-c','import sysconfig; print(sysconfig.get_path("purelib"))']))
    assert call([str(python),'-c','import importlib.util; print(importlib.util.find_spec("asterun") is None)'])=='True'
    return {'wheel':wheel,'sha256':hashlib.sha256(wheel.read_bytes()).hexdigest(),'manifest':library/'asterun_plugin_jules/manifest.json','runtime_path':library,'runner':[str(python),'-m','asterun_plugin_jules.worker']}

@pytest.fixture
def remote(isolated_env,workspace_root):
    state=isolated_env/'remote-state';state.mkdir(mode=0o700)
    fixture={'calls':[],'mode':'happy','plan':'p1','session':{'name':'sessions/fixture','state':'AWAITING_PLAN_APPROVAL','url':'https://jules.google.com/session/fixture','requirePlanApproval':True,'sourceContext':{'source':'sources/repo','githubRepoContext':{'startingBranch':'main'}}}}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def call(self):
            assert self.headers['x-goog-api-key']=='jules-offline-fixture-key'
            body=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))) or b'{}');fixture['calls'].append((self.command,self.path,body))
            if fixture['mode']=='rate':self.send_response(429);self.send_header('Retry-After','10');self.end_headers();return
            if self.command=='POST' and fixture['mode']=='timeout':self.close_connection=True;return
            if self.command=='POST' and self.path.endswith(':approvePlan'):fixture['session']['state']='IN_PROGRESS';result={}
            elif self.command=='POST' and self.path.endswith(':sendMessage'):result={}
            elif self.path.split('?')[0].endswith('/activities'):
                query=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                page=query.get('pageToken',['0'])[0]
                if page=='0':result={'activities':[],'nextPageToken':'1'}
                else:result={'activities':[{'name':'sessions/fixture/activities/plan','createTime':'2026-09-11T00:00:00Z','planGenerated':{'plan':{'id':fixture['plan'],'steps':[{'id':'s1','title':'read files'}]}}}]}
                if fixture['mode']=='cursor_loop':result['nextPageToken']='1'
                if fixture['mode']=='many_pages' and int(page)<4:result={'activities':[],'nextPageToken':str(int(page)+1)}
            else:result=fixture['session']
            raw=json.dumps(result).encode();self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        do_GET=call;do_POST=call;do_DELETE=call
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    fixture['context']={'run_id':'run-create','task_id':'task-fixture','binding_id':'bind-fixture','plan_id':'plan-fixture','provider_account_ref':'fixture','workspace_root':str(workspace_root.resolve()),'capability':'agent.execute','auth_mode':'user_api_key','upstream_version':'v1alpha',
        'options':{'execution_enabled':True,'state_root':str(state.resolve()),'source':'sources/repo','starting_branch':'main','endpoint':f'http://127.0.0.1:{server.server_port}/v1alpha/','offline_fixture':True}}
    fixture['state']=state
    yield fixture
    server.shutdown();thread.join();server.server_close()

def rpc(installation,remote,*,method='execution.start',cap=None,input=None,run=None):
    context=deepcopy(remote['context']);context.update(capability=cap or 'agent.execute',run_id=run or 'run-create')
    return host(installation).call(method,input or {'text':'fixture request'},context,cwd=context['workspace_root'],environment={'JULES_API_KEY':'jules-offline-fixture-key'},secrets=('jules-offline-fixture-key',)).result

def due(remote):
    with closing(sqlite3.connect(remote['state']/'receipts.sqlite3')) as db:
        for key,raw in db.execute('SELECT key,value FROM records').fetchall():
            obj=json.loads(raw)
            if key.startswith('job:'):obj['next_poll']=0;db.execute('UPDATE records SET value=? WHERE key=?',(json.dumps(obj),key))
        db.commit()

def test_static_is_inert(jules_installation,tmp_path):
    assert host(jules_installation).call('plugin.describe',{}, {},cwd=tmp_path).result['manifest']['plugin_id']=='google.jules'
    assert host(jules_installation).call('usage.observe',{}, {},cwd=tmp_path).result['remaining'] is None
    assert not list(tmp_path.iterdir())

def test_plan_approval_feedback_and_no_automatic_pr(jules_installation,remote):
    first=rpc(jules_installation,remote);assert first['status']=='waiting_input',first
    post=remote['calls'][0];assert post==('POST','/v1alpha/sessions',{'prompt':'fixture request','requirePlanApproval':True,'sourceContext':remote['session']['sourceContext']})
    plan=rpc(jules_installation,remote,cap='remote.plan.inspect',input={'session':'sessions/fixture'},run='inspect');assert plan['status']=='succeeded',plan
    assert plan['output']['plan']['id']=='p1' and not plan['output']['source_evidence']['immutable_sha_verified']
    assert len([c for c in remote['calls'] if c[0]=='POST'])==1
    due(remote)
    approval=rpc(jules_installation,remote,cap='remote.plan.approve',input={'session':'sessions/fixture','plan_sha256':plan['output']['plan_sha256']},run='approve');assert approval['status']=='succeeded',approval
    assert rpc(jules_installation,remote,cap='remote.plan.approve',input={'session':'sessions/fixture','plan_sha256':plan['output']['plan_sha256']},run='approve')==approval
    remote['session']['state']='AWAITING_USER_FEEDBACK';due(remote)
    observed=rpc(jules_installation,remote,method='execution.observe');assert observed['status']=='waiting_input',observed
    sent=rpc(jules_installation,remote,cap='remote.message.send',input={'session':'sessions/fixture','text':'approved feedback'},run='feedback');assert sent['status']=='succeeded',sent
    assert len([c for c in remote['calls'] if c[0]=='POST'])==3
    remote['session']['state']='COMPLETED';due(remote)
    final=rpc(jules_installation,remote,method='execution.reconcile');assert final['status']=='succeeded',final


def test_changed_plan_refuses_approval(jules_installation,remote):
    rpc(jules_installation,remote)
    plan=rpc(jules_installation,remote,cap='remote.plan.inspect',input={'session':'sessions/fixture'},run='inspect')
    remote['plan']='p2';due(remote)
    result=rpc(jules_installation,remote,cap='remote.plan.approve',input={'session':'sessions/fixture','plan_sha256':plan['output']['plan_sha256']},run='approve')
    assert result['status']=='failed' and result['output']['text']=='plan_changed_reapproval_required',result
    assert len([c for c in remote['calls'] if c[0]=='POST'])==1

@pytest.mark.parametrize('mode',['timeout','rate'])
def test_unknown_create_and_backoff_never_resend(jules_installation,remote,mode):
    remote['mode']=mode
    first=rpc(jules_installation,remote);assert first['status']=='pending_reconcile',first
    assert rpc(jules_installation,remote)==first
    assert rpc(jules_installation,remote,method='execution.reconcile')==first
    assert len(remote['calls'])==1
    if mode=='rate':
        second=rpc(jules_installation,remote,run='new-intent');assert second['status']=='pending_reconcile',second
        assert len(remote['calls'])==1

@pytest.mark.parametrize('cap',[ 'remote.plan.approve','remote.message.send'])
def test_unknown_post_is_not_reissued(jules_installation,remote,cap):
    rpc(jules_installation,remote)
    plan=rpc(jules_installation,remote,cap='remote.plan.inspect',input={'session':'sessions/fixture'},run='inspect');due(remote)
    remote['mode']='timeout';input={'session':'sessions/fixture',**({'plan_sha256':plan['output']['plan_sha256']} if cap.endswith('approve') else {'text':'feedback'})}
    first=rpc(jules_installation,remote,cap=cap,input=input,run='change');assert first['status']=='pending_reconcile',first
    assert rpc(jules_installation,remote,cap=cap,input=input,run='change')==first
    assert rpc(jules_installation,remote,method='execution.reconcile',cap=cap,run='change')==first
    assert len([c for c in remote['calls'] if c[0]=='POST'])==2


def test_persistent_pagination_throttle_and_cancel_is_not_delete(jules_installation,remote):
    rpc(jules_installation,remote);remote['mode']='many_pages'
    first=rpc(jules_installation,remote,method='execution.observe');assert first['status']=='pending_reconcile',first
    count=len(remote['calls']);rpc(jules_installation,remote,method='execution.observe');assert len(remote['calls'])==count
    due(remote);rpc(jules_installation,remote,method='execution.observe');due(remote)
    result=rpc(jules_installation,remote,method='execution.observe');assert result['output']['plan']['id']=='p1',result
    tokens=[urllib.parse.parse_qs(urllib.parse.urlsplit(c[1]).query).get('pageToken',['0'])[0] for c in remote['calls'] if '/activities?' in c[1]]
    assert tokens==['0','1','2','3','4']
    result=rpc(jules_installation,remote,method='execution.cancel');assert result['status']=='pending_reconcile' and not result['supported']
    assert all(c[0]!='DELETE' for c in remote['calls'])

@pytest.mark.parametrize('change,reason',[('source','source_context_changed'),('policy','plan_approval_policy_changed'),('identity','session_identity_changed'),('cursor','cursor_loop')])
def test_remote_drift_is_not_accepted(jules_installation,remote,change,reason):
    rpc(jules_installation,remote)
    if change=='source':remote['session']['sourceContext']['githubRepoContext']['startingBranch']='other'
    if change=='policy':remote['session']['requirePlanApproval']=False
    if change=='identity':remote['session']['name']='sessions/other'
    if change=='cursor':remote['mode']='cursor_loop'
    result=rpc(jules_installation,remote,method='execution.observe')
    assert result['status']=='pending_reconcile' and result['output']['text']==reason,result

@pytest.mark.parametrize('field,value',[('execution_enabled',False),('endpoint','https://evil.invalid/v1alpha/'),('starting_branch',''),('source','../../secrets')])
def test_invalid_options_never_contact_http(jules_installation,remote,field,value):
    remote['context']['options'][field]=value
    result=rpc(jules_installation,remote);assert result['status']=='failed',result
    assert remote['calls']==[]


def test_session_cannot_be_claimed_by_other_account(jules_installation,remote):
    rpc(jules_installation,remote);remote['context']['provider_account_ref']='other-account'
    result=rpc(jules_installation,remote,cap='remote.message.send',input={'session':'sessions/fixture','text':'feedback'},run='feedback')
    assert result['status']=='failed' and result['output']['text']=='session_not_owned',result
    assert len(remote['calls'])==1


def test_application_restart_disable_reconciles_without_create(jules_installation,remote,isolated_env,workspace_root):
    path=external_config(isolated_env,workspace_root,jules_installation);raw=json.loads(path.read_text())
    raw['plugins']['google.jules']=raw['plugins'].pop('example.external-fake')
    conn=raw['connections']['external'];conn.update(plugin_id='google.jules',auth_mode='user_api_key',upstream_version='v1alpha',options=remote['context']['options'])
    secret=isolated_env/'fixture-key';secret.write_text('jules-offline-fixture-key');secret.chmod(0o600)
    raw['provider_accounts'][conn['provider_account_ref']].update(provider='jules',credential_ref='file:'+str(secret.resolve()))
    raw['billing_pools'][conn['billing_pool_refs'][0]].update(billing_mode='subscription',remaining=None,overage_verified=True,allow_unknown_subscription=True,meter='requests',local_limit=10,estimate_per_action=1)
    path.write_text(json.dumps(raw))
    with closing(Application.from_paths(path,isolated_env/'state')) as app:
        first=app.handle('task.submit',{'workspace':'demo','text':'create'});assert first.ok,first.to_dict()
        assert first.data['run']['native']['provider_job_ref']=='sessions/fixture'
        assert app.handle('plugin.disable',{'plugin_id':'google.jules'}).ok
    remote['session']['state']='COMPLETED';due(remote)
    with closing(Application.from_paths(path,isolated_env/'state')) as app:
        result=app.handle('task.reconcile',{'task_id':first.ids['task_id']});assert result.ok,result.to_dict()
    assert len([c for c in remote['calls'] if c[0]=='POST'])==1

@pytest.mark.parametrize('state',['QUEUED','PLANNING','IN_PROGRESS'])
def test_confirmed_create_running_state_is_reconcilable_without_user_wait(jules_installation,remote,state):
    remote['session']['state']=state
    first=rpc(jules_installation,remote);assert first['status']=='pending_reconcile',first
    assert first['output']['state']==state and first['provider_job_ref']=='sessions/fixture'
    remote['session']['state']='COMPLETED'
    observed=rpc(jules_installation,remote,method='execution.reconcile');assert observed['status']=='succeeded',observed
    assert len([c for c in remote['calls'] if c[0]=='POST'])==1
