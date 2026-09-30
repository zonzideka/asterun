import json
import subprocess

from asterun.application import Application
from asterun.config import load_config
from tests.conftest import write_config


def git(root, *args):
    return subprocess.check_output(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', *args], cwd=root, text=True).strip()


def setup(root, workspace):
    git(workspace, 'init', '-q')
    git(workspace, 'add', 'note.txt')
    git(workspace, 'commit', '-qm', 'fixture')
    commit = git(workspace, 'rev-parse', 'HEAD')
    slot = root / 'worker'; slot.mkdir()
    cfg = write_config(root / 'materialize.json', workspace, extra={'workspaces': {
        'demo': {'root': str(workspace)}, 'worker': {'root': str(slot), 'allow_non_git': True},
        'alias': {'root': str(workspace)},
    }})
    return Application(load_config(cfg)), slot, {'source_workspace': 'demo', 'workspace': 'worker', 'commit': commit}


def test_fixed_worktree_creation_replay_preserves_dirty_source_and_target(isolated_env, workspace_root):
    app, slot, payload = setup(isolated_env, workspace_root)
    try:
        original = (workspace_root / 'note.txt').read_text()
        (workspace_root / 'note.txt').write_text('private dirty input')
        result = app.handle('workspace.materialize', payload)
        assert result.ok, result.error
        assert not result.data['reused'] and result.data['detached']
        assert (slot / 'note.txt').read_text() == original
        assert (workspace_root / 'note.txt').read_text() == 'private dirty input'
        assert app.handle('workspace.materialize', payload).data['reused']
        (slot / 'note.txt').write_text('do not overwrite')
        assert app.handle('workspace.materialize', payload).error['code'] == 'REVISION_CONFLICT'
        assert (slot / 'note.txt').read_text() == 'do not overwrite'
    finally:
        app.close()


def test_worktree_refuses_active_alias_nonempty_slot_filters_and_permissions(isolated_env, workspace_root):
    from asterun.policy import Decision
    app, slot, payload = setup(isolated_env, workspace_root)
    try:
        (slot / 'existing').write_text('keep')
        assert not app.handle('workspace.materialize', payload).ok
        (slot / 'existing').unlink()
        git(workspace_root, 'config', 'filter.fixture.process', '/missing/must-not-run')
        assert app.handle('workspace.materialize', payload).error['code'] == 'CAPABILITY_UNSUPPORTED'
        git(workspace_root, 'config', '--unset', 'filter.fixture.process')
        holder = app.handle('task.submit', {'workspace': 'alias', 'script': 'cancel_accept_only'})
        assert holder.ok
        assert not app.handle('workspace.materialize', payload).ok
        assert not (slot / '.git').exists()
        class Deny:
            def authorize(self, *args):
                return Decision(False, 'SCOPE_DENIED')
        app.policy = Deny()
        assert app.handle('workspace.materialize', payload).error['code'] == 'SCOPE_DENIED'
    finally:
        app.close()


def test_worktree_cli_mcp_and_complete_commit_required(isolated_env, workspace_root):
    from asterun.cli import build_parser, _load_request
    from asterun.mcp_server import handle_rpc
    app, slot, payload = setup(isolated_env, workspace_root)
    try:
        assert not app.handle('workspace.materialize', {**payload, 'commit': 'HEAD'}).ok
        request = isolated_env / 'request.json'; request.write_text(json.dumps(payload))
        args = build_parser().parse_args(['workspace-materialize', '--request', str(request)])
        rpc = handle_rpc(app, {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'workspace_materialize', 'arguments': _load_request(args)}})
        assert not rpc.get('error'), rpc
        assert (slot / '.git').exists()
    finally:
        app.close()
