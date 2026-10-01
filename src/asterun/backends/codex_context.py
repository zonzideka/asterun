"""Codex 原生压缩协议门禁；导出 schema 不登录、不启动模型。"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from asterun.errors import AsterunError


def binary_sha(binary):
    with Path(binary).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def preflight(binary):
    try:
        before = binary_sha(binary)
        with tempfile.TemporaryDirectory(prefix='asterun-context-schema-') as directory:
            root = Path(directory); (root / 'home').mkdir()
            env = {'PATH': os.defpath, 'HOME': str(root / 'home'), 'CODEX_HOME': str(root / 'home/codex')}
            version = subprocess.run([binary, '--version'], cwd=root, env=env, check=True,
                capture_output=True, text=True, timeout=5).stdout.strip()
            subprocess.run([binary, 'app-server', 'generate-json-schema', '--out', str(root / 'schema')],
                cwd=root, env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
            documents = {}; hashes = {}
            for name in ('ClientRequest.json', 'ServerNotification.json', 'v2/ThreadCompactStartParams.json',
                         'v2/ThreadCompactStartResponse.json'):
                path = root / 'schema' / name
                if path.stat().st_size > 16 * 1024 * 1024: raise ValueError('schema too large')
                raw = path.read_bytes(); documents[name] = json.loads(raw); hashes[name] = hashlib.sha256(raw).hexdigest()
            params = documents['v2/ThreadCompactStartParams.json']
            if params.get('required') != ['threadId'] or params['properties']['threadId']['type'] != 'string':
                raise ValueError('compact params changed')
            if documents['v2/ThreadCompactStartResponse.json'].get('type') != 'object' or documents['v2/ThreadCompactStartResponse.json'].get('required'):
                raise ValueError('compact acknowledgement changed')
            methods = {v for row in documents['ClientRequest.json'].get('oneOf', [])
                       for v in row.get('properties', {}).get('method', {}).get('enum', [])}
            if not {'thread/compact/start', 'thread/read', 'thread/start', 'thread/resume', 'turn/start'} <= methods:
                raise ValueError('native methods missing')
            definitions = documents['ServerNotification.json']['definitions']
            items = definitions['ThreadItem']['oneOf']
            if not any(r.get('properties', {}).get('type', {}).get('enum') == ['contextCompaction'] for r in items):
                raise ValueError('completion item missing')
            if not {'threadId', 'turn'} <= set(definitions['TurnCompletedNotification']['required']):
                raise ValueError('completion binding missing')
        if binary_sha(binary) != before or not version or len(version) > 256:
            raise ValueError('binary changed')
        return {'verification': 'installed_schema_only', 'native_client_version': version,
                'binary_sha256': before, 'schema_sha256': hashes, 'live_compaction_verified': False}
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        raise AsterunError('CAPABILITY_UNSUPPORTED', '原生上下文 schema 无法核验，未发起模型请求') from exc


def compact(runtime, transport, session, record, publish, stopping, cancelled, *, timeout=60):
    """ACK 不等于终态；错误、取消、超时及持久失败一律不继续发送工作 prompt。"""
    record = deepcopy(record)
    old_turn = record['source_native']['turn_id']
    observed = {'turn': None, 'compaction_item': False, 'terminal': None, 'invalid': False}

    def observe(message):
        params = message.get('params') or {}
        if params.get('threadId') != session.session_id: return
        turn = params.get('turn') or {}
        tid = params.get('turnId') or turn.get('id')
        if not tid or tid == old_turn: return
        if observed['turn'] is not None and tid != observed['turn']:
            observed['invalid'] = True; return
        if message.get('method') == 'turn/started': observed['turn'] = tid
        if message.get('method') == 'item/completed' and params.get('item', {}).get('type') == 'contextCompaction':
            observed['turn'] = tid; observed['compaction_item'] = True
        if message.get('method') == 'turn/completed':
            observed['turn'] = tid; observed['terminal'] = turn.get('status')

    transport.on_message(observe)
    def save(phase):
        record.update(phase=phase, compaction_turn_id=observed['turn'])
        native = {'thread_id': session.session_id, 'backend_session_id': session.session_id,
                  'context_transition': deepcopy(record)}
        snap = session.to_status_dict()
        if snap.get('token_usage'):
            native.update(token_usage=snap['token_usage'], usage_observation=snap.get('usage_observation', {}),
                          usage_scope='thread_cumulative', usage_source='codex_app_server')
        publish({'status': 'running', 'terminated': False, 'native': native})
    runtime._read(transport, record['source_native'], Path(session.cwd), check_latest=True)
    save('compaction_requested')  # 核心确认持久化后，才允许发 RPC。
    if stopping.is_set() or cancelled(): raise AsterunError('REMOTE_STATE_UNKNOWN', '压缩发出前已停止')
    transport.request('thread/compact/start', {'threadId': session.session_id}, timeout=timeout)
    save('compaction_acknowledged')
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stopping.is_set() or cancelled() or not transport.alive or observed['invalid'] or session.pending_requests:
            raise AsterunError('REMOTE_STATE_UNKNOWN', '压缩状态未确定，不派发后继 prompt')
        if observed['terminal']:
            if observed['terminal'] != 'completed' or not observed['compaction_item']:
                raise AsterunError('REMOTE_STATE_UNKNOWN', '缺少成功压缩终态及产物事件')
            native = {'backend_session_id': session.session_id, 'turn_id': observed['turn']}
            thread = runtime._read(transport, native, Path(session.cwd), check_latest=True)
            latest = thread['turns'][-1]
            if latest.get('status') != 'completed' or not any(i.get('type') == 'contextCompaction' for i in latest.get('items', [])):
                raise AsterunError('REMOTE_STATE_UNKNOWN', '压缩终态与原生持久历史不一致')
            save('compaction_completed')
            return record
        stopping.wait(0.03)
    raise AsterunError('REMOTE_STATE_UNKNOWN', '压缩观察超时；保留原操作，不自动重发')
