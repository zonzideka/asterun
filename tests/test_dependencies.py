import json
from pathlib import Path

from asterun.application import Application
from asterun.config import load_config
from asterun.sqlite_store import SqliteStore
from tests.conftest import write_config


def make(isolated_env,workspace_root, *, persistent=False):
    cfg=write_config(isolated_env/'deps.json',workspace_root)
    data=json.loads(cfg.read_text())
    for alias in ('second','combined'):
        root=isolated_env/alias;root.mkdir();(root/'note.txt').write_text(alias)
        data['workspaces'][alias]={'root':str(root),'allow_non_git':True}
    cfg.write_text(json.dumps(data))
    return Application(load_config(cfg),store=SqliteStore(isolated_env/'deps.sqlite') if persistent else None)


def accept(app,id):
    snap=app.handle('workflow.snapshot',{'task_id':id,'target_paths':['note.txt']}).data
    bound={'task_id':id,'target_paths':['note.txt'],**{'expected_'+k:snap[k] for k in ('run_id','revision','input_hash','target_hash')}}
    result=app.handle('workflow.evaluate',{**bound,'checks':[{'kind':'file_exists','path':'note.txt'}]})
    assert result.ok,result.error
    return result.data


def test_dependencies_wait_for_acceptance_not_execution_and_do_not_starve_queue(isolated_env,workspace_root):
    app=make(isolated_env,workspace_root)
    try:
        one=app.handle('task.submit',{'workspace':'demo','text':'worker'})
        child=app.handle('task.submit',{'workspace':'combined','text':'combine','depends_on':[one.ids['task_id']]})
        assert child.ok,child.error
        assert child.data['run']['status']=='queued'
        independent=app.handle('task.submit',{'workspace':'second','text':'independent'})
        assert independent.data['run']['status']=='succeeded'
        app.poll()
        assert app.store.get_task(app._require_task(child.ids['task_id']).id).acceptance.value=='pending'
        assert accept(app,one.ids['task_id'])['acceptance']=='passed'
        app.poll()
        result=app.handle('task.get',{'task_id':child.ids['task_id']})
        assert result.data['run']['status']=='succeeded'
        assert accept(app,child.ids['task_id'])['acceptance']=='passed'
        (workspace_root/'note.txt').write_text('upstream changed')
        result=app.handle('task.get',{'task_id':child.ids['task_id'],'compact':True})
        assert result.data['task']['acceptance']=='pending'
        assert not result.data['task']['dependency_state']['ready']
    finally:app.close()


def test_combiner_required_integration_stage_blocks_green_workers(isolated_env,workspace_root):
    app=make(isolated_env,workspace_root)
    try:
        one=app.handle('task.submit',{'workspace':'demo','text':'worker'})
        two=app.handle('task.submit',{'workspace':'second','text':'worker'})
        accept(app,one.ids['task_id']);accept(app,two.ids['task_id'])
        child=app.handle('task.submit',{'workspace':'combined','text':'combine','depends_on':[one.ids['task_id'],two.ids['task_id']],
                                       'required_stages':['integration'],'idempotency_key':'combine'})
        assert child.ok,child.error
        assert child.data['run']['status']=='succeeded'
        assert accept(app,child.ids['task_id'])['acceptance']=='pending'
        repeated=app.handle('task.submit',{'workspace':'combined','text':'combine','depends_on':[one.ids['task_id'],two.ids['task_id']],
                                       'required_stages':['integration'],'idempotency_key':'combine'})
        assert repeated.ids['task_id']==child.ids['task_id']
        changed=app.handle('task.submit',{'workspace':'combined','text':'combine','depends_on':[one.ids['task_id']],
                                       'required_stages':['integration'],'idempotency_key':'combine'})
        assert not changed.ok and changed.error['code']=='IDEMPOTENCY_CONFLICT'
    finally:app.close()


def test_dependency_queue_survives_restart_and_shared_workspace_rejected(isolated_env,workspace_root):
    app=make(isolated_env,workspace_root,persistent=True)
    one=app.handle('task.submit',{'workspace':'demo','text':'worker'})
    forbidden=app.handle('task.submit',{'workspace':'demo','text':'same tree','depends_on':[one.ids['task_id']]})
    assert not forbidden.ok
    child=app.handle('task.submit',{'workspace':'combined','text':'combine','depends_on':[one.ids['task_id']]})
    assert child.ok and child.data['run']['status']=='queued'
    app.close()
    restored=Application(load_config(isolated_env/'deps.json'),store=SqliteStore(isolated_env/'deps.sqlite'))
    try:
        restored.poll()
        assert restored.handle('task.get',{'task_id':child.ids['task_id']}).data['run']['status']=='queued'
        accept(restored,one.ids['task_id']);restored.poll()
        assert restored.handle('task.get',{'task_id':child.ids['task_id']}).data['run']['status']=='succeeded'
        assert len(restored.backends['fake'].calls)==1
    finally:restored.close()


def test_ready_dependency_still_obeys_persistent_quota_stop(isolated_env, workspace_root):
    from tests.test_grok_code_policy import config_file, open_app, ActionPolicy
    from asterun.contracts import RunStatus
    from asterun.ids import RunId
    from asterun.grok_quota import scopes
    path = config_file(isolated_env, workspace_root, 1, 'workspace-code-v1')
    data = json.loads(path.read_text())
    for alias in ('child', 'holder'):
        root = isolated_env / alias; root.mkdir(); (root / 'note.txt').write_text(alias)
        data['workspaces'][alias] = {'root': str(root), 'allow_non_git': True}
    data['backends']['worker']['home'] = str(isolated_env / 'grok-home')
    path.write_text(json.dumps(data))
    app, backend = open_app(path, ActionPolicy())
    try:
        parent = app.handle('task.submit', {'workspace': 'demo', 'backend': 'fake'})
        holder = app.handle('task.submit', {'workspace': 'holder', 'backend': 'worker', 'script': 'cancel_accept_only'})
        child = app.handle('task.submit', {'workspace': 'child', 'backend': 'worker', 'depends_on': [parent.ids['task_id']]})
        assert child.ok and child.data['run']['status'] == 'queued'
        accept(app, parent.ids['task_id'])
        run = app.store.get_run(RunId(holder.ids['run_id']))
        run.native.update(provider_failure={'kind': 'quota_exhausted'}, grok_quota_scopes=scopes(app, 'worker'))
        run.status = RunStatus.PENDING_RECONCILE
        app.store.save_run(run); app.scheduler.finish('worker', run.id)
        app._drain_queue()
        current = app.store.get_run(RunId(child.ids['run_id']))
        assert current.status == RunStatus.FAILED and current.error_code == 'BUDGET_EXHAUSTED'
        assert backend.dispatch_count == 1
    finally:
        app.close()


def test_failed_combination_is_not_accepted_after_both_workers_pass(isolated_env, workspace_root):
    app = make(isolated_env, workspace_root)
    app.config.workflow.local_checks['combined'] = {'argv': ['/usr/bin/false'], 'inputs': ['note.txt'],
        'timeout_seconds': 5, 'runtime_roots': [], 'stage': 'integration'}
    try:
        parents = [app.handle('task.submit', {'workspace': alias}).ids['task_id'] for alias in ('demo', 'second')]
        for parent in parents:
            assert accept(app, parent)['acceptance'] == 'passed'
        child = app.handle('task.submit', {'workspace': 'combined', 'depends_on': parents, 'required_stages': ['integration']})
        id = child.ids['task_id']
        snap = app.handle('workflow.snapshot', {'task_id': id, 'target_paths': ['note.txt']}).data
        bound = {'task_id': id, 'target_paths': ['note.txt'], **{'expected_' + k: snap[k] for k in ('run_id', 'revision', 'input_hash', 'target_hash')}}
        checked = app.handle('workflow.check', {**bound, 'check_name': 'combined'})
        assert checked.ok and not checked.data['passed'], checked.error
        assert accept(app, id)['acceptance'] == 'failed'
    finally:
        app.close()
