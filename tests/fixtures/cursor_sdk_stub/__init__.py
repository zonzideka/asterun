"""仅测试用 SDK 接缝；真正 SDK 安装另用同一 HTTP fixture 验证。"""
from types import SimpleNamespace
import json
import subprocess
import struct
import urllib.request

class Options:
    def __init__(self, **kwargs):self.kwargs=kwargs
    def wire(self):
        names={'api_key':'apiKey','disallowed_tools':'disallowedTools','mcp_servers':'mcpServers','setting_sources':'settingSources'}
        raw={names.get(k,k): v.wire() if hasattr(v,'wire') else v for k,v in self.kwargs.items()}
        if 'model' in raw:raw['model']={'id':raw['model']}
        if 'tools' in raw:raw['tools']={'names':raw['tools']}
        return raw
AgentOptions=LocalAgentOptions=SendOptions=Options

def obj(raw):
    return SimpleNamespace(id=raw['runId'],agent_id=raw['agentId'],model=SimpleNamespace(id=raw.get('model',{}).get('id')),
        status=raw['status'],result=raw.get('result',''),usage=SimpleNamespace(total_tokens=raw['usage']['totalTokens']) if raw.get('usage') else None,created_at=raw.get('createdAt'))

class Run:
    def __init__(self,client,raw,events=()):
        self.__dict__.update(vars(obj(raw)));self.client=client;self.rows=events
    def events(self):
        for row in self.rows:
            raw=row.get('sdkMessage'); message=SimpleNamespace(**{k: v for k,v in (raw or {}).items() if k not in {'agentId','runId'}}) if raw else None
            if message:message.agent_id=raw.get('agentId','');message.run_id=raw.get('runId','')
            yield SimpleNamespace(offset=row.get('offset'),sdk_message=message)
    def wait(self):return self
    def supports(self,op):return op=='cancel'
    def cancel(self):self.client.rpc('CancelRun',{'runId':self.id,'agentId':self.agent_id})

class Agent:
    def __init__(self,client,raw):self.client=client;self.agent_id=raw['agentId'];self.model=SimpleNamespace(id=raw['model']['id'])
    def __enter__(self):return self
    def __exit__(self,*args):self.client.rpc('CloseAgent',{'agentId':self.agent_id})
    def send(self,text,options,idempotency_key):
        rows=self.client.rpc('Send',{'agentId':self.agent_id,'message':{'text':text},'options':options.wire(),'idempotencyKey':idempotency_key},stream=True)
        result=next((r['result']['result'] for r in reversed(rows) if 'result' in r),None)
        if result is None:
            status=rows[0]['sdkMessage'];result=self.client.rpc('GetRun',{'runId':status['runId']})['run']
        return Run(self.client,result,rows)

class Agents:
    def __init__(self,client):self.client=client
    def create(self,options):return Agent(self.client,self.client.rpc('CreateAgent',{'options':options.wire()}))
    def resume(self,session,options):return Agent(self.client,self.client.rpc('ResumeAgent',{'agentId':session,'options':options.wire()}))
    def get(self,session,*,cwd,api_key):
        raw=self.client.rpc('GetAgent',{'agentId':session,'options':{'cwd':cwd,'apiKey':api_key}})['agent']
        return SimpleNamespace(agent_id=raw['agentId'],runtime='local',cwd=raw['local']['cwd'])
    def list_runs(self,session,**options):
        rows=self.client.rpc('ListRuns',{'agentId':session,'options':options})['items']
        return SimpleNamespace(items=[Run(self.client,row) for row in rows])
    def get_run(self,turn,options):return Run(self.client,self.client.rpc('GetRun',{'runId':turn,'options':options})['run'])

class CursorClient:
    @classmethod
    def launch_bridge(cls,*,command,workspace,state_root,host,timeout,client_timeout,max_retries,allow_api_key_env_fallback):
        assert max_retries==0 and allow_api_key_env_fallback is True
        self=cls();self.proc=subprocess.Popen([command,'--workspace',workspace,'--state-root',state_root],stderr=subprocess.PIPE,stdout=subprocess.DEVNULL,text=True)
        line=self.proc.stderr.readline();self.url=json.loads(line.split(' ready ',1)[1])['url'];self.agents=Agents(self)
        self.models=SimpleNamespace(list=lambda **kw:[SimpleNamespace(**r) for r in self.rpc('ListModels',{'options':{'apiKey':kw['api_key']}})['items']])
        return self
    def rpc(self,method,body,stream=False):
        request=urllib.request.Request(self.url+'/fixture/'+method,json.dumps(body).encode(),{'Content-Type':'application/json'})
        with urllib.request.urlopen(request,timeout=5) as response:data=response.read()
        if not stream:return json.loads(data)
        rows=[]
        while data:
            flag,size=struct.unpack('>BI',data[:5]);raw=data[5:5+size];data=data[5+size:]
            if flag==0:rows.append(json.loads(raw))
        return rows
    def __enter__(self):return self
    def __exit__(self,*args):
        self.proc.terminate();self.proc.wait(timeout=2);self.proc.stderr.close()
