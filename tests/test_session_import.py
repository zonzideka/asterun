import hashlib
import json
from pathlib import Path
from urllib.parse import quote

from asterun.application import Application
from asterun.config import load_config
from asterun.sqlite_store import SqliteStore
from tests.conftest import write_config


def setup(root, workspace):
    home = root / 'grok-home'; home.mkdir()
    sid = '12345678-1234-1234-1234-123456789abc'
    logs = home / 'sessions' / quote(str(workspace.resolve()), safe='') / sid
    logs.mkdir(parents=True)
    (logs / 'summary.json').write_text(json.dumps({'info': {'id': sid, 'cwd': str(workspace.resolve())}, 'private': 'do not export'}))
    (logs / 'updates.jsonl').write_text(json.dumps({'method': 'session/update', 'params': {
        'sessionId': sid, 'update': {'sessionUpdate': 'turn_completed', 'thought': 'private',
            'usage': {'inputTokens': 10, 'outputTokens': 2, 'unexpected': 'private'}}}}) + '\n')
    cfg = write_config(root / 'import.json', workspace, extra_backends={'native': {'kind': 'grok', 'home': str(home)}})
    app = Application(load_config(cfg), store=SqliteStore(root / 'import.sqlite'))
    task = app.handle('task.submit', {'workspace': 'demo', 'text': 'local tracking task'})
    assert task.ok, task.error
    payload = {'task_id': task.ids['task_id'], 'backend': 'native', 'session_id': sid,
               **{key+'_sha256': hashlib.sha256((logs/name).read_bytes()).hexdigest()
                  for key, name in [('summary', 'summary.json'), ('updates', 'updates.jsonl')]}}
    return app, logs, payload, cfg


def test_readonly_import_dedup_reload_cli_mcp_and_no_usage_double_count(isolated_env, workspace_root):
    from asterun.cli import build_parser, _load_request
    from asterun.mcp_server import handle_rpc
    app, logs, payload, cfg = setup(isolated_env, workspace_root)
    try:
        calls = len(app.backends['fake'].calls)
        before = app.handle('usage.report', {'task_id': payload['task_id']}).data
        result = app.handle('session.import', payload)
        assert result.ok, result.error
        assert result.data['source'] == 'external_observed'
        assert result.data['last_reported_usage'] == {'inputTokens': 10, 'outputTokens': 2}
        assert not result.data['resume_supported'] and not result.data['usage_included_in_totals']
        assert 'private' not in json.dumps(result.data)
        assert not (logs.parents[2] / '.asterun-session-bindings').exists()
        assert app.handle('session.import', payload).data['reused']
        after = app.handle('usage.report', {'task_id': payload['task_id']}).data
        assert before == after
        assert len(app.backends['fake'].calls) == calls
        assert app._require_task(payload['task_id']).acceptance.value == 'pending'
        other = app.handle('task.submit', {'workspace': 'demo', 'text': 'another tracking task'})
        assert app.handle('session.import', {**payload, 'task_id': other.ids['task_id']}).error['code'] == 'IDEMPOTENCY_CONFLICT'
        request = isolated_env / 'request.json'; request.write_text(json.dumps(payload))
        args = build_parser().parse_args(['session-import', '--request', str(request)])
        rpc = handle_rpc(app, {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'session_import', 'arguments': _load_request(args)}})
        assert not rpc['result']['isError']
    finally:
        app.close()
    restored = Application(load_config(cfg), store=SqliteStore(isolated_env / 'import.sqlite'))
    try:
        assert restored.handle('session.import', payload).data['reused']
        assert not restored.backends['fake'].calls
        (logs / 'updates.jsonl').write_text((logs / 'updates.jsonl').read_text() * 2)
        assert not restored.handle('session.import', payload).ok
        changed = {**payload, 'updates_sha256': hashlib.sha256((logs / 'updates.jsonl').read_bytes()).hexdigest()}
        assert restored.handle('session.import', changed).error['code'] == 'REVISION_CONFLICT'
    finally:
        restored.close()


def test_import_rejects_escaping_links_unbound_logs_and_permissions(isolated_env, workspace_root):
    from asterun.policy import Decision
    app, logs, payload, _ = setup(isolated_env, workspace_root)
    try:
        assert not app.handle('session.import', {**payload, 'session_id': '../escape'}).ok
        summary = logs / 'summary.json'; raw = summary.read_bytes()
        summary.unlink(); summary.symlink_to(workspace_root / 'note.txt')
        assert not app.handle('session.import', payload).ok
        summary.unlink(); summary.write_text('{}')
        bad = {**payload, 'summary_sha256': hashlib.sha256(summary.read_bytes()).hexdigest()}
        assert not app.handle('session.import', bad).ok
        summary.write_bytes(raw)
        class Deny:
            def authorize(self, *args):
                return Decision(False, 'SCOPE_DENIED')
        app.policy = Deny()
        assert app.handle('session.import', payload).error['code'] == 'SCOPE_DENIED'
    finally:
        app.close()
