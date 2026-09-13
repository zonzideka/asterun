"""有界 REST 适配与本地回执日志；不拥有任务队列或自主审批循环。"""
from __future__ import annotations
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import urllib.error
import urllib.parse
import urllib.request

API = 'https://jules.googleapis.com/v1alpha/'
LIMIT = 128 * 1024

class Rejected(Exception): pass
class Deferred(Exception): pass

def require(value, reason):
    if not value: raise Rejected(reason)

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()

def session_name(value):
    require(isinstance(value,str) and re.fullmatch(r'sessions/[A-Za-z0-9_-]{1,180}',value), 'invalid_session')
    return value

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): raise Rejected('redirect_forbidden')

class Journal:
    def __init__(self,root):
        self.path=root/'receipts.sqlite3'
        require(not self.path.is_symlink(), 'invalid_journal')
        if self.path.exists():
            require(self.path.stat().st_uid==os.getuid() and stat.S_IMODE(self.path.stat().st_mode)==0o600,'invalid_journal')
        self.db=sqlite3.connect(self.path);self.path.chmod(0o600)
        self.db.execute('CREATE TABLE IF NOT EXISTS records (key TEXT PRIMARY KEY, value TEXT NOT NULL)');self.db.commit()
    def get(self,key):
        row=self.db.execute('SELECT value FROM records WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else None
    def put(self,key,value):
        self.db.execute('INSERT OR REPLACE INTO records VALUES (?,?)',(key,json.dumps(value)));self.db.commit()
    def close(self):self.db.close()

class Client:
    def __init__(self,options,key,journal,owner):
        self.options,self.key,self.journal,self.owner=options,key,journal,owner
        self.base=options.get('endpoint',API)
        if self.base!=API:
            parsed=urllib.parse.urlsplit(self.base)
            require(options.get('offline_fixture') is True and key=='jules-offline-fixture-key' and
                    parsed.scheme=='http' and parsed.hostname=='127.0.0.1' and parsed.port and
                    not parsed.username and not parsed.password and parsed.path=='/v1alpha/' and
                    not parsed.query and not parsed.fragment,'endpoint_forbidden')
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    def call(self,method,path,body=None):
        rate=self.journal.get('rate:'+self.owner) or {}
        if rate.get('next',0)>time.time(): raise Deferred('provider_backoff')
        data=None if body is None else json.dumps(body,allow_nan=False).encode()
        request=urllib.request.Request(self.base+path,data,{'x-goog-api-key':self.key,'Content-Type':'application/json'},method=method)
        try:
            with self.opener.open(request,timeout=2) as response:
                raw=response.read(LIMIT+1)
                require(len(raw)<=LIMIT,'response_too_large')
                result=json.loads(raw or b'{}')
                require(isinstance(result,dict),'invalid_response')
                return result
        except urllib.error.HTTPError as error:
            if error.code in {429,503}:
                try:delay=int(error.headers.get('Retry-After','30'))
                except ValueError:delay=30
                self.journal.put('rate:'+self.owner,{'next':time.time()+max(5,min(delay,3600))})
            # POST 任意错误均不能证明没有副作用，也不能由客户端重试。
            raise Deferred('http_result_unknown') from None

    def source(self):
        return {'source':self.options['source'],'githubRepoContext':{'startingBranch':self.options['starting_branch']}}
    def verify(self,session):
        require(session.get('sourceContext')==self.source(),'source_context_changed')
        require(session.get('requirePlanApproval') is True,'plan_approval_policy_changed')
    def read(self,name,record,*,fresh=False):
        if record.get('next_poll',0)>time.time():raise Deferred('poll_backoff')
        session=self.call('GET',name)
        require(session.get('name')==name,'session_identity_changed');self.verify(session)
        cursor='' if fresh else record.get('cursor','')
        latest=None if fresh or not cursor else record.get('scan_plan')
        seen=set();complete=False
        # 普通观察最多 2 页后持久保存；审批核验从第一页完整重读，过大历史交给人工。
        for _ in range(8 if fresh else 2):
            page=self.call('GET',name+'/activities?'+urllib.parse.urlencode({'pageSize':50,**({'pageToken':cursor} if cursor else {})}))
            rows=page.get('activities',[]);require(isinstance(rows,list) and len(rows)<=100,'invalid_activities')
            for row in rows:
                require(isinstance(row,dict) and isinstance(row.get('name'),str) and row['name'].startswith(name+'/activities/'),'activity_identity_changed')
                plan=row.get('planGenerated',{}).get('plan')
                if plan is not None:
                    require(isinstance(plan,dict) and isinstance(plan.get('id'),str) and isinstance(plan.get('steps'),list),'invalid_plan')
                    require(len(json.dumps(plan).encode())<=24000,'plan_too_large')
                    stamp=row.get('createTime');require(isinstance(stamp,str) and stamp.endswith('Z'),'plan_time_missing')
                    candidate={'time':stamp,'name':row['name'],'plan':plan}
                    if latest and stamp==latest['time'] and digest(plan)!=digest(latest['plan']):raise Rejected('ambiguous_latest_plan')
                    if latest is None or (stamp,row['name'])>(latest['time'],latest['name']):latest=candidate
            token=page.get('nextPageToken','');require(isinstance(token,str) and len(token)<=2048,'invalid_cursor')
            if not token:complete=True;cursor='';break
            require(token not in seen,'cursor_loop');seen.add(token);cursor=token
        record.update(cursor=cursor,scan_plan=latest,next_poll=time.time()+5)
        if complete:record['plan']=latest
        record['session']=session
        self.journal.put('job:'+name,record)
        if not complete:raise Deferred('activities_incomplete')
        return session,latest


def output(client,session,latest=None):
    name=session_name(session.get('name'));state=session.get('state','STATE_UNSPECIFIED')
    url=session.get('url','')
    if url:
        parsed=urllib.parse.urlsplit(url)
        require(parsed.scheme=='https' and parsed.hostname in {'jules.google.com','jules.google'} and not parsed.username,'invalid_native_url')
    result={'text':'Jules '+state,'session':name,'url':url,'state':state,
            'source_evidence':{'source':client.options['source'],'starting_branch':client.options['starting_branch'],
                               'revision_kind':'mutable_branch','immutable_sha_verified':False,'workspace_kind':'provider_managed'}}
    if latest:
        result['plan']=latest['plan'];result['plan_sha256']=digest({'session':name,'source':client.source(),'activity':latest})
    return result


def remote_status(state):
    return {'COMPLETED':'succeeded','FAILED':'failed',
            'AWAITING_PLAN_APPROVAL':'waiting_input','AWAITING_USER_FEEDBACK':'waiting_input'}.get(state,'pending_reconcile')


def execute(method,input,context):
    journal=None;lock=None;action=None;client=None;record=None
    refs={k:context[k] for k in ('provider_job_ref','provider_session_id') if context.get(k)}
    attempted=False
    try:
        options=context.get('options');require(isinstance(options,dict) and not set(options)-{'execution_enabled','state_root','source','starting_branch','endpoint','offline_fixture'},'invalid_options')
        require(options.get('execution_enabled') is True,'execution_not_enabled')
        require(context.get('auth_mode')=='user_api_key' and context.get('upstream_version')=='v1alpha','connection_mismatch')
        for key in ('run_id','task_id','plan_id','binding_id','provider_account_ref','workspace_root'):
            require(isinstance(context.get(key),str) and bool(context[key]),'execution_binding_required')
        require(isinstance(options.get('source'),str) and re.fullmatch(r'sources/[A-Za-z0-9_.-]{1,200}',options['source']),'source_required')
        branch=options.get('starting_branch');require(isinstance(branch,str) and 0<len(branch)<=200 and not any(ord(c)<32 for c in branch),'branch_required')
        root=Path(options.get('state_root',''));require(root.is_absolute() and root.resolve(strict=True)==root and not root.is_relative_to(Path(context['workspace_root']).resolve()),'state_root_invalid')
        info=root.stat();require(stat.S_ISDIR(info.st_mode) and info.st_uid==os.getuid() and stat.S_IMODE(info.st_mode)==0o700,'state_root_not_private')
        key=os.environ.pop('JULES_API_KEY',None);require(isinstance(key,str) and key.strip(),'credential_required')
        owner=digest({'account':context['provider_account_ref'],'key_fingerprint':hashlib.sha256(key.encode()).hexdigest(),
                      'workspace':context['workspace_root'],'source':options['source'],'branch':branch,'endpoint':options.get('endpoint',API)})
        fd=os.open(root/'worker.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600);lock=os.fdopen(fd,'a')
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        journal=Journal(root);client=Client(options,key,journal,owner)
        action_key='action:'+owner+':'+context['run_id'];action=journal.get(action_key)
        cap=context.get('capability');require(cap in {'agent.execute','remote.plan.inspect','remote.plan.approve','remote.message.send'},'capability_unsupported')
        if method=='execution.start':
            if action:
                require(action['input_sha256']==digest({'capability':cap,'input':input}),'idempotency_conflict')
                # worker 重送请求只回读本地已记录结果，绝不重新 POST。
                return action['result']
            if cap=='agent.execute':
                require(set(input)=={'text'} and isinstance(input['text'],str) and 0<len(input['text'])<=65536,'invalid_input')
                name=None
            else:
                name=session_name(input.get('session'));record=journal.get('job:'+name)
                require(record and record['owner']==owner,'session_not_owned')
                refs={'provider_job_ref':name,'provider_session_id':name}
            if cap=='remote.plan.inspect':
                session,latest=client.read(name,record,fresh=True)
                return {'status':'succeeded','output':output(client,session,latest),**refs}
            if cap=='remote.plan.approve':
                require(set(input)=={'session','plan_sha256'},'invalid_input')
                session,latest=client.read(name,record,fresh=True)
                require(session.get('state')=='AWAITING_PLAN_APPROVAL' and latest,'plan_not_waiting')
                require(output(client,session,latest).get('plan_sha256')==input['plan_sha256'],'plan_changed_reapproval_required')
            if cap=='remote.message.send':
                require(set(input)=={'session','text'} and isinstance(input['text'],str) and 0<len(input['text'])<=32000,'invalid_input')
                session=client.call('GET',name);require(session.get('name')==name,'session_identity_changed');client.verify(session)
                require(session.get('state') in {'AWAITING_USER_FEEDBACK','AWAITING_PLAN_APPROVAL','IN_PROGRESS','PLANNING'},'session_not_active')
            action={'input_sha256':digest({'capability':cap,'input':input}),'session':name,
                    'result':{'status':'pending_reconcile','output':{'text':'remote_result_unknown'},**refs}}
            journal.put(action_key,action)  # POST 前 fsync-backed SQLite commit；未知结果保持原键。
            attempted=True
            if cap=='agent.execute':
                session=client.call('POST','sessions',{'prompt':input['text'],'requirePlanApproval':True,'sourceContext':client.source()})
                name=session_name(session.get('name'));refs={'provider_job_ref':name,'provider_session_id':name}
                action['session']=name;action['result'].update(refs);journal.put(action_key,action)
                client.verify(session)
                record={'owner':owner,'session':session,'next_poll':0,'cursor':''};journal.put('job:'+name,record)
                result={'status':remote_status(session.get('state')),'output':output(client,session),**refs}
            else:
                suffix=':approvePlan' if cap=='remote.plan.approve' else ':sendMessage'
                client.call('POST',name+suffix,{} if cap=='remote.plan.approve' else {'prompt':input['text']})
                result={'status':'succeeded','output':{'text':'Jules request accepted; execution and quality remain unverified','session':name},**refs}
            action['result']=result;action['post_confirmed']=True;journal.put(action_key,action);return result
        if not action:
            raise Rejected('execution_intent_missing')
        if not action.get('post_confirmed'):return action['result']
        name=action.get('session');require(name,'native_reference_unknown')
        record=journal.get('job:'+name);require(record and record['owner']==owner,'session_not_owned')
        refs={'provider_job_ref':name,'provider_session_id':name}
        if cap!='agent.execute':return action['result']
        if method=='execution.cancel':
            return {'status':'pending_reconcile','supported':False,'output':{'text':'cancel_unsupported; delete is not cancel'},**refs}
        session,latest=client.read(name,record)
        state=session.get('state')
        status=remote_status(state)
        return {'status':status,'output':output(client,session,latest),**refs}
    except Exception as error:
        reason=str(error) if isinstance(error,(Rejected,Deferred)) else 'remote_result_unknown'
        status='pending_reconcile' if attempted or method!='execution.start' else 'failed'
        result={'status':status,'output':{'text':reason},'usage':[],**refs}
        if action and attempted and journal:
            action['result']=result;journal.put(action_key,action)
        return result
    finally:
        if journal:journal.close()
        if lock:lock.close()
