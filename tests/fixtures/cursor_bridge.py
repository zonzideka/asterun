"""本地 HTTP/Connect 协议替身，绝不转发供应商或接收真实账户。"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import struct
import sys

parser = argparse.ArgumentParser()
parser.add_argument('--workspace'); parser.add_argument('--state-root')
args, _ = parser.parse_known_args()
root = Path(args.state_root); workspace = args.workspace
mode = (root / 'mode').read_text() if (root / 'mode').exists() else 'happy'
state_path = root / 'native.json'
state = json.loads(state_path.read_text()) if state_path.exists() else {'agents': {}, 'runs': {}, 'sends': 0}

def save():
    state_path.write_text(json.dumps(state))

def record(method, body):
    def clean(value):
        if isinstance(value, dict): return {k: clean(v) for k, v in value.items() if k not in {'apiKey', 'api_key'}}
        if isinstance(value, list): return [clean(v) for v in value]
        return value
    with (root / 'calls.jsonl').open('a') as out: out.write(json.dumps({'method': method, 'body': clean(body)}) + '\n')

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def send_json(self, value, status=200):
        data=json.dumps(value).encode();self.send_response(status);self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def do_POST(self):
        raw = self.rfile.read(int(self.headers['Content-Length']))
        if 'connect+json' in self.headers.get('Content-Type',''): raw=raw[5:]
        body=json.loads(raw); method=self.path.rsplit('/',1)[-1];record(method,body)
        if method in {'Shutdown','CloseAgent','CancelRun'}:
            if method=='CancelRun' and mode!='cancel_unconfirmed':
                state['runs'][body['runId']]['status']='cancelled';save()
            self.send_json({});return
        if method=='ListModels':self.send_json({'items': [] if mode=='model_missing' else [{'id':'fixture-model'}]});return
        if method in {'CreateAgent','ResumeAgent'}:
            opts=body['options'];agent=body.get('agentId','agent-fixture')
            if 'tools' not in opts or opts.get('local',{}).get('settingSources') not in (None,[]):
                self.send_json({'code':'invalid_argument','message':'restriction snapshot missing'},400);return
            state['agents'][agent]={'agentId':agent,'local':{'cwd':workspace},'model':{'id':'fixture-model'},'options':opts}
            # 永远不把测试 key 落进原生 fixture 数据。
            state['agents'][agent]['options'].pop('apiKey',None);save()
            self.send_json({'agentId':agent,'model':{'id':'wrong' if mode=='model_mismatch' else 'fixture-model'}});return
        if method=='GetAgent':
            item=state['agents'].get(body['agentId'],{})
            if mode=='workspace_mismatch':item={**item,'local':{'cwd':'/different/workspace'}}
            self.send_json({'agent':item});return
        if method=='ListRuns':
            runs=list(reversed(list(state['runs'].values())))
            if mode=='newer_turn':runs=[{**runs[0],'runId':'run-external'}]+runs
            self.send_json({'items':runs[:1]});return
        if method=='GetRun':
            item=state['runs'].get(body['runId'],{})
            if mode=='get_identity_mismatch':item={**item,'agentId':'agent-wrong'}
            self.send_json({'run':item});return
        if method=='Send':
            state['sends']+=1;turn=f"run-fixture-{state['sends']}";agent=body['agentId']
            result={'runId':turn,'agentId':agent,'status': 'running' if mode in {'eof','cancel_unconfirmed'} else 'error' if mode=='error' else 'finished',
                    'result':body['message'].get('text',''), 'model':{'id':'fixture-model'},
                    'createdAt':'2026-09-11T00:00:00Z','usage':{'inputTokens':6,'outputTokens':4,'totalTokens':10}}
            state['runs'][turn]=result;save()
            if mode=='create_timeout': self.close_connection=True;return
            events=[{'offset':'1','sdkMessage':{'type':'status','agentId':agent,'runId':turn,'status':'running'}}]
            if mode not in {'eof','cancel_unconfirmed'}:
                for n in (2,3):events.append({'offset':str(n),'sdkMessage':{'type':'usage','agentId':agent,'runId':turn,'usage':{'inputTokens':3,'outputTokens':2,'totalTokens':5}}})
                if mode in {'tool_error','unexpected_tool'}: events.append({'offset':'4','sdkMessage':{'type':'tool_call','agentId':agent,'runId':turn,'name':'Read' if mode=='tool_error' else 'Shell','status':'error','args':{'secret':'private diagnostic'},'result':{'private':'diagnostic'}}})
                events.append({'offset':'5','result':{'result':result}})
            data=b''.join(struct.pack('>BI',0,len(chunk))+chunk for chunk in (json.dumps(e).encode() for e in events))
            data+=struct.pack('>BI',2,2)+b'{}'
            self.send_response(200);self.send_header('Content-Type','application/connect+json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
        if method=='WaitLiveRun': self.send_json({'result':state['runs'][body['runId']]});return
        self.send_json({'code':'unimplemented','message':'fixture method unsupported'},501)

require_no_keys = not any(k in os.environ for k in ('CURSOR_API_KEY','OPENAI_API_KEY','ANTHROPIC_API_KEY'))
(root/'bridge-environment.json').write_text(json.dumps({'no_account_keys':require_no_keys,'pid':os.getpid()}))
server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
print('cursor-sdk-bridge ready '+json.dumps({'schemaVersion':1,'transport':'tcp','protocol':'connect','url':f'http://127.0.0.1:{server.server_port}','authToken':'fixture-bridge-token','serverVersion':'1.0.31','workspaceRef':workspace,'stateRoot':str(root)}),file=sys.stderr,flush=True)
server.serve_forever()
