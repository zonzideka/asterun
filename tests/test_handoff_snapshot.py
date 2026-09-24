from copy import deepcopy
import json

import pytest

from asterun.application import Application
from asterun.backup import backup_state, restore_state
from asterun.config import load_config
from asterun.contracts import RunStatus, AcceptanceStatus
from asterun.errors import AsterunError
from asterun.handoff import state
from asterun.ids import TaskId, RunId
from asterun.policy import Decision
from asterun.quality import digest
from asterun.sqlite_store import SqliteStore
from tests.conftest import write_config


def setup(env, root, script='success'):
    cfg = write_config(env / 'config.json', root)
    directory = env / 'state'; directory.mkdir()
    app = Application(load_config(cfg), store=SqliteStore(directory / 'asterun.sqlite'), state_dir=directory)
    result = app.handle('task.submit', {'workspace': 'demo', 'script': script,
        'text': '修复解析器；不能改公共接口，保留独立审查，不得发布。第二阶段先征得同意。'})
    assert result.ok, result.error
    b = {'task_id': result.ids['task_id'], 'expected_run_id': result.ids['run_id']}
    return app, b, directory


def create(app, b, revision=0):
    return app.handle('workflow.handoff-create', {**b, 'expected_handoff_revision': revision, 'target_paths': ['note.txt']})


def read(app, b, revision=1):
    return app.handle('workflow.handoff-read', {**b, 'expected_handoff_revision': revision, 'reconstruct': True})


def test_roundtrip_restart_backup_original_constraints_not_short_summary(isolated_env, workspace_root):
    app, b, directory = setup(isolated_env, workspace_root)
    task = app.store.get_task(TaskId(b['task_id']))
    task.external_require_review = True; task.review_status = 'required'; app.store.save_task(task)
    result = create(app, b); assert result.ok, result.error
    raw = app.store.get_checkpoint_blob(result.data['sha256'])
    assert task.text.encode() not in raw and '非 Git 输入'.encode() not in raw
    package = read(app, b); assert package.ok, package.error
    assert package.data['materials']['original_objective_and_constraints'] == task.text
    assert package.data['materials']['external_require_review']
    assert not package.data['native_session_created']
    assert app.backends['fake'].dispatch_count == 1
    archive = isolated_env / 'state.tar.gz'; backup_state(directory, archive); app.close()
    restored = isolated_env / 'restored'; restore_state(archive, restored)
    app = Application(load_config(isolated_env / 'config.json'), store=SqliteStore(restored / 'asterun.sqlite'), state_dir=restored)
    try:
        again = read(app, b); assert again.ok, again.error
        assert again.data == package.data
        assert app.backends['fake'].dispatch_count == 0
    finally: app.close()


@pytest.mark.parametrize('drift', ['source', 'task', 'run', 'missing', 'schema', 'policy', 'stale_acceptance'])
def test_handoff_invalidates_instead_of_falling_back(isolated_env, workspace_root, drift):
    app, b, _ = setup(isolated_env, workspace_root)
    try:
        result = create(app, b); assert result.ok, result.error
        task = app.store.get_task(TaskId(b['task_id']))
        run = app.store.get_run(RunId(b['expected_run_id']))
        if drift == 'source': (workspace_root / 'note.txt').write_text('changed')
        elif drift == 'task': task.text += '新增限制'; app.store.save_task(task)
        elif drift == 'run': run.summary += 'changed'; app.store.save_run(run)
        elif drift == 'stale_acceptance':
            task.acceptance = AcceptanceStatus.PASSED; task.evidence_stale = True; app.store.save_task(task)
        elif drift == 'missing':
            app.store._conn.execute('DELETE FROM control_blobs WHERE sha256=?', (result.data['sha256'],))
        elif drift == 'schema':
            document = json.loads(app.store.get_checkpoint_blob(result.data['sha256']))
            document['schema_version'] = 100
            document['checksum'] = digest({k:v for k,v in document.items() if k != 'checksum'})
            sha = app.store.put_checkpoint_blob(json.dumps(document).encode())
            task.handoff_snapshot['sha256'] = sha; app.store.save_task(task)
        else: app.config.workflow.input_optimization.update(mode='observe', scopes=['caller_result'])
        assert not read(app, b).ok
        assert app.backends['fake'].dispatch_count == 1
    finally: app.close()


def test_two_connections_cas_no_last_writer_wins(isolated_env, workspace_root):
    app, b, directory = setup(isolated_env, workspace_root)
    other = SqliteStore(directory / 'asterun.sqlite')
    try:
        task = other.get_task(TaskId(b['task_id'])); run = other.get_run(RunId(b['expected_run_id']))
        expected = state(other, task, run)
        winner = create(app, b); assert winner.ok
        with pytest.raises(AsterunError, match='交接状态'):
            other.commit_handoff(task, run, expected, 0, b'loser')
        assert other.get_task(task.id).handoff_snapshot['sha256'] == winner.data['sha256']
        next_one = create(app, b, 1); assert next_one.ok
        assert not read(app, b, 1).ok and read(app, b, 2).ok
    finally: other.close(); app.close()


@pytest.mark.parametrize('point', ['before_blob', 'after_blob'])
def test_interrupted_atomic_write_keeps_old_pointer_and_no_fake_reference(isolated_env, workspace_root, monkeypatch, point):
    app, b, _ = setup(isolated_env, workspace_root)
    try:
        first = create(app, b); assert first.ok
        task = app.store.get_task(TaskId(b['task_id']))
        blob_count = app.store._conn.execute('SELECT COUNT(*) FROM control_blobs').fetchone()[0]
        def fail(*args): raise OSError('disk full during handoff commit')
        with monkeypatch.context() as m:
            m.setattr(app.store, 'put_checkpoint_blob' if point == 'before_blob' else 'save_task', fail)
            failed = create(app, b, 1)
            assert not failed.ok
        assert app.store.get_task(task.id).handoff_snapshot == task.handoff_snapshot
        assert app.store._conn.execute('SELECT COUNT(*) FROM control_blobs').fetchone()[0] == blob_count
        assert read(app, b).ok
    finally: app.close()


def test_approval_and_unknown_remain_unresolved_no_dispatch(isolated_env, workspace_root):
    app, b, _ = setup(isolated_env, workspace_root, 'wait_input')
    try:
        first = create(app, b); assert first.ok, first.error
        assert not first.data['offline_reconstruction_ready']
        package = read(app, b); assert package.ok, package.error
        doc = package.data['document']
        assert doc['pending_approvals'] and doc['unresolved_actions']
        assert package.data['materials']['pending_approval']
        approval = app.store.find_pending_approval(RunId(b['expected_run_id']))
        approval.summary += 'changed'; app.store.save_approval(approval)
        assert not read(app, b).ok
        assert app.backends['fake'].dispatch_count == 1
    finally: app.close()


def test_read_rechecks_permission_and_state_after_reconstruction(isolated_env, workspace_root, monkeypatch):
    app, b, _ = setup(isolated_env, workspace_root)
    try:
        assert create(app, b).ok
        with monkeypatch.context() as m:
            m.setattr(app.policy, 'authorize', lambda *args: Decision(False, 'denied'))
            assert not read(app, b).ok
        import asterun.handoff as handoff
        snapshot = handoff.snapshot
        def concurrent_change(*args, **kw):
            result = snapshot(*args, **kw)
            task = app.store.get_task(TaskId(b['task_id'])); task.text += ' concurrent'; app.store.save_task(task)
            return result
        monkeypatch.setattr(handoff, 'snapshot', concurrent_change)
        assert not read(app, b).ok
    finally: app.close()


def test_external_report_keeps_source_review_and_invalidates_when_report_changes(app, workspace_root):
    from tests.test_external_handoff import evaluate_report
    task_id = evaluate_report(app, workspace_root, require_review=True)
    task = app.store.get_task(TaskId(task_id))
    b = {'task_id': task_id, 'expected_run_id': task.current_run_id.value}
    assert create(app, b).ok
    response = read(app, b); assert response.ok, response.error
    acceptance = response.data['document']['verified_facts'][1]
    assert acceptance['value'] == 'pending' and acceptance['evidence_source'] == 'external_reported'
    assert response.data['materials']['external_require_review']
    (workspace_root / 'report.json').write_text('changed')
    assert not read(app, b).ok


def test_read_does_not_erase_existing_payload_manifest(isolated_env, workspace_root):
    app, b, _ = setup(isolated_env, workspace_root)
    try:
        run_id = RunId(b['expected_run_id'])
        app.store.save_payload_manifest(run_id, 'dispatch_prompt', {'sha256': 'metadata'})
        assert create(app, b).ok and read(app, b).ok
        assert app.store.get_run(run_id).native['payload_manifests']['dispatch_prompt'] == {'sha256': 'metadata'}
    finally: app.close()


def test_full_review_constraints_and_findings_reload_from_authority(isolated_env, workspace_root):
    from dataclasses import replace
    app, b, _ = setup(isolated_env, workspace_root)
    try:
        app.config = replace(app.config, workflow=replace(app.config.workflow, require_review=True))
        task = app.store.get_task(TaskId(b['task_id']))
        task.findings = [{'id': f'f-{n}', 'status': 'open', 'detail': f'限制 {n}'} for n in range(70)]
        app.store.save_task(task)
        assert create(app, b).ok
        result = read(app, b); assert result.ok, result.error
        assert len(result.data['document']['open_issues']) == 64
        assert result.data['document']['open_issue_count'] == 70
        assert len(result.data['materials']['findings']) == 70
        assert result.data['materials']['acceptance_contract']['require_review']
        assert app.backends['fake'].dispatch_count == 1
    finally: app.close()


def test_unknown_action_stays_unresolved_and_never_replayed(isolated_env, workspace_root):
    app, b, _ = setup(isolated_env, workspace_root)
    try:
        run = app.store.get_run(RunId(b['expected_run_id']))
        run.status = RunStatus.PENDING_RECONCILE; app.store.save_run(run)
        result = create(app, b); assert result.ok and not result.data['offline_reconstruction_ready']
        response = read(app, b); assert response.ok
        assert response.data['document']['unresolved_actions'][0]['status'] == 'pending_reconcile'
        assert app.backends['fake'].dispatch_count == 1
    finally: app.close()
