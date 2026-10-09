import json
import subprocess

from asterun.application import Application
from asterun.backup import backup_state, restore_state
from asterun.config import load_config
from asterun.ids import RunId
from asterun.observe import compact_task_snapshot
from asterun.sqlite_store import SqliteStore
from tests.conftest import write_config


def setup(isolated_env, workspace_root, *, sqlite=False):
    cfg = write_config(isolated_env / 'config.json', workspace_root,
                       extra={'workflow': {'checkpoint_paths': {'demo': ['note.txt', 'new.txt']}}})
    state = isolated_env / 'state'; state.mkdir()
    store = SqliteStore(state / 'asterun.sqlite') if sqlite else None
    return Application(load_config(cfg), store=store, state_dir=state), state


def inspect(app, submitted, include=True):
    return app.handle('workflow.checkpoint', {'task_id': submitted.ids['task_id'],
                      'expected_run_id': submitted.ids['run_id'], 'include_artifact': include})


def test_original_dirty_baseline_new_files_patch_and_no_index_write(isolated_env, workspace_root, monkeypatch):
    def git(*args):
        return subprocess.check_output(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', *args], cwd=workspace_root)
    git('init', '-q'); git('add', 'note.txt'); git('commit', '-qm', 'baseline')
    (workspace_root / 'note.txt').write_text('user dirty\n')
    index = (workspace_root / '.git/index').read_bytes()
    app, _ = setup(isolated_env, workspace_root)
    backend = app.backends['fake']; dispatch = backend.dispatch
    def implementation(*args, **kwargs):
        (workspace_root / 'note.txt').write_text('user dirty\nagent addition\n')
        (workspace_root / 'new.txt').write_text('new artifact\n')
        return dispatch(*args, **kwargs)
    monkeypatch.setattr(backend, 'dispatch', implementation)
    try:
        submitted = app.handle('task.submit', {'workspace': 'demo', 'text': 'implement'})
        assert submitted.ok, submitted.error
        data = inspect(app, submitted)
        assert data.ok, data.error
        data = data.data
        assert data['source_matches'] and data['recovery_ready']
        assert data['artifact']['changes'][0]['new_file']
        assert 'agent addition' in data['artifact']['patch']
        assert '-user dirty' not in data['artifact']['patch']
        baseline = json.loads(app.store.get_checkpoint_blob(data['baseline_sha256']))
        assert any('note.txt' in row for row in baseline['dirty_at_baseline'])
        assert (workspace_root / '.git/index').read_bytes() == index
        assert git('log', '--format=%s', '-1').strip() == b'baseline'
        compact = compact_task_snapshot(submitted.data)
        assert 'artifact' not in json.dumps(compact) and 'agent addition' not in json.dumps(compact)
        (workspace_root / 'note.txt').write_text('new user edit')
        drift = inspect(app, submitted).data
        assert not drift['source_matches'] and not drift['recovery_ready']
    finally:
        app.close()


def test_checkpoint_backup_restore_and_source_identity(isolated_env, workspace_root):
    app, state = setup(isolated_env, workspace_root, sqlite=True)
    submitted = app.handle('task.submit', {'workspace': 'demo', 'text': 'implement'})
    assert submitted.ok, submitted.error
    data = inspect(app, submitted).data
    assert data['source_matches']
    archive = isolated_env / 'state.tar.gz'
    backup_state(state, archive)
    app.close()
    restored_dir = isolated_env / 'restored'
    restore_state(archive, restored_dir)
    restored = Application(load_config(isolated_env / 'config.json'),
                           store=SqliteStore(restored_dir / 'asterun.sqlite'), state_dir=restored_dir)
    try:
        copy = inspect(restored, submitted)
        assert copy.ok, copy.error
        assert copy.data['latest_sha256'] == data['latest_sha256']
        assert copy.data['artifact'] == data['artifact']
    finally:
        restored.close()


def test_checkpoint_disk_failure_before_dispatch_prevents_delivery(isolated_env, workspace_root, monkeypatch):
    app, _ = setup(isolated_env, workspace_root)
    calls = []
    monkeypatch.setattr(app.backends['fake'], 'dispatch', lambda *args, **kwargs: calls.append(1))
    monkeypatch.setattr(app.store, 'put_checkpoint_blob', lambda *args: (_ for _ in ()).throw(OSError('disk full')))
    try:
        response = app.handle('task.submit', {'workspace': 'demo', 'text': 'implement'})
        assert not response.ok and not calls
    finally:
        app.close()


def test_terminal_persistence_failure_restarts_without_redelivery(isolated_env, workspace_root, monkeypatch):
    app, state = setup(isolated_env, workspace_root, sqlite=True)
    write = app.store.put_checkpoint_blob
    count = []
    def fail_terminal(content):
        count.append(1)
        if len(count) > 1:
            raise OSError('disk full at terminal checkpoint')
        return write(content)
    monkeypatch.setattr(app.store, 'put_checkpoint_blob', fail_terminal)
    response = app.handle('task.submit', {'workspace': 'demo', 'text': 'implement'})
    assert not response.ok
    task_id = app.store.list_task_ids()[0]
    run_id = app.store.get_task(task_id).current_run_id
    failed = app.store.get_run(run_id)
    # 终态检查点没写上时，不能提交成功运行；结果留着，重启后重放，不重新调用后端。
    assert failed.status.value == 'pending_reconcile'
    assert failed.native['unapplied_result']['status'] == 'succeeded'
    assert failed.native['checkpoint']['status'] == 'baseline'
    app.close()
    restored = Application(load_config(isolated_env / 'config.json'), store=SqliteStore(state / 'asterun.sqlite'), state_dir=state)
    calls = []
    monkeypatch.setattr(restored.backends['fake'], 'dispatch', lambda *args, **kwargs: calls.append(1))
    try:
        restored.poll()
        run = restored.store.get_run(run_id)
        assert run.status.value == 'succeeded' and not calls
        assert 'unapplied_result' not in run.native
        assert run.native['checkpoint']['status'] == 'captured'
        data = restored.handle('workflow.checkpoint', {'task_id':task_id.value, 'expected_run_id':run_id.value})
        assert data.ok and data.data['recovery_ready']
    finally:
        restored.close()


def test_checkpoint_api_cli_mcp_permission_and_scope(isolated_env, workspace_root):
    from asterun.cli import build_parser, _load_request
    from asterun.mcp_server import handle_rpc
    from asterun.policy import Decision
    app, _ = setup(isolated_env, workspace_root)
    try:
        submitted = app.handle('task.submit', {'workspace':'demo', 'text':'implement'})
        args = build_parser().parse_args(['workflow-checkpoint',submitted.ids['task_id'],'--run-id',submitted.ids['run_id']])
        payload = _load_request(args)
        rpc = handle_rpc(app, {'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'workflow_checkpoint','arguments':payload}})
        assert not rpc.get('error')
        class Deny:
            def authorize(self, principal, action, resource):return Decision(action != 'workspace.read', 'SCOPE_DENIED')
        app.policy = Deny()
        assert not inspect(app, submitted).ok
    finally:
        app.close()
