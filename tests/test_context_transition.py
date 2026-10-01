from copy import deepcopy
from dataclasses import replace
import json

import pytest

from asterun.application import Application
from asterun.config import load_config
from asterun.contracts import RunStatus
from asterun.errors import AsterunError
from asterun.ids import TaskId, RunId
from asterun.sqlite_store import SqliteStore
from tests.test_native_lifecycle import native_config, wait_for, submit
from tests.conftest import write_config


def package(app, submitted):
    b = {'task_id': submitted.ids['task_id'], 'expected_run_id': submitted.ids['run_id']}
    created = app.handle('workflow.handoff-create', {**b, 'expected_handoff_revision': 0, 'target_paths': ['note.txt']})
    assert created.ok, created.error
    return {**b, 'expected_handoff_revision': 1, 'idempotency_key': 'successor-once'}


def enable(path):
    value = json.loads(path.read_text()); value['workflow'] = {'input_optimization': {
        'mode': 'enforce', 'context_strategies': ['native_compact', 'handoff_fresh']}}
    path.write_text(json.dumps(value))


@pytest.mark.parametrize('strategy', ['native_compact', 'handoff_fresh'])
def test_native_protocol_success_and_idempotent_replay(native_config, isolated_env, workspace_root, strategy):
    enable(native_config)
    app = Application.from_paths(native_config, isolated_env / 'state', background=True)
    try:
        first = submit(app, 'finish 保留公共接口；不得部署。')
        done = wait_for(app, first.ids['task_id'], lambda d: d['run']['status'] == 'succeeded')
        old_native = deepcopy(done['run']['native'])
        request = {**package(app, first), 'context_strategy': strategy}
        result = app.handle('workflow.repair', request); assert result.ok, result.error
        current = wait_for(app, first.ids['task_id'], lambda d: d['run']['status'] in ['succeeded', 'pending_reconcile'])
        assert current['run']['status'] == 'succeeded', current
        assert current['task']['run_count'] == 2 and current['task']['repair_count'] == 1
        assert current['task']['acceptance'] != 'passed'
        assert current['run']['native']['context_transition']['phase'] == 'prompt_started'
        calls_before = (workspace_root / 'native-calls.jsonl').read_text()
        replay = app.handle('workflow.repair', request)
        assert replay.ok and replay.data['reused'] and replay.ids['run_id'] == result.ids['run_id']
        assert (workspace_root / 'native-calls.jsonl').read_text() == calls_before
        calls = [json.loads(line) for line in calls_before.splitlines()]
        assert sum(c.get('method') == 'turn/start' for c in calls) == 2
        if strategy == 'native_compact':
            assert sum(c.get('method') == 'thread/compact/start' for c in calls) == 1
            assert current['run']['native']['thread_id'] == old_native['thread_id']
            assert current['run']['native']['context_transition']['compaction_turn_id'] != old_native['turn_id']
        else:
            assert current['run']['native']['thread_id'] != old_native['thread_id']
            prompt = [c['params']['input'][0]['text'] for c in calls if c.get('method') == 'turn/start'][-1]
            assert '不得部署' in prompt and '非 Git 输入' in prompt and 'require_review' in prompt
        assert app.store.get_run(RunId(first.ids['run_id'])).native == old_native
    finally: app.close()


@pytest.mark.parametrize('scenario', ['disconnect_after_ack', 'failed'])
def test_compaction_ack_or_failure_never_dispatches_prompt_or_replays_on_restart(native_config, isolated_env, workspace_root, scenario):
    enable(native_config); directory = isolated_env / 'state'
    app = Application.from_paths(native_config, directory, background=True)
    first = submit(app, 'finish')
    wait_for(app, first.ids['task_id'], lambda d: d['run']['status'] == 'succeeded')
    request = {**package(app, first), 'context_strategy': 'native_compact'}
    (workspace_root / 'context-scenario.json').write_text(json.dumps(scenario))
    result = app.handle('workflow.repair', request); assert result.ok, result.error
    done = wait_for(app, first.ids['task_id'], lambda d: d['run']['status'] == 'pending_reconcile')
    assert done['run']['native']['context_transition']['phase'] == 'compaction_acknowledged'
    app.close()
    reopened = Application.from_paths(native_config, directory, background=True)
    try:
        replay = reopened.handle('workflow.repair', request); assert replay.ok and replay.data['reused']
        reconciled = reopened.handle('task.reconcile', {'task_id': first.ids['task_id']}); assert reconciled.ok
        run = reopened.store.get_run(RunId(result.ids['run_id']))
        assert run.status == 'pending_reconcile'
        calls = [json.loads(l) for l in (workspace_root / 'native-calls.jsonl').read_text().splitlines()]
        assert sum(c.get('method') == 'turn/start' for c in calls) == 1
        assert sum(c.get('method') == 'thread/compact/start' for c in calls) == 1
    finally: reopened.close()


def fake_setup(env, root):
    path = write_config(env / 'config.json', root); enable(path)
    store = SqliteStore(env / 'state.sqlite')
    app = Application(load_config(path), store=store)
    first = app.handle('task.submit', {'workspace': 'demo', 'text': '保留约束', 'idempotency_key': 'source'})
    assert first.ok
    return app, first, {**package(app, first), 'context_strategy': 'handoff_fresh'}


@pytest.mark.parametrize('failure', ['disabled','budget','changed_file','changed_contract','unknown','cancel','pending_check','write_failure'])
def test_gates_prevent_successor_dispatch(isolated_env, workspace_root, failure):
    app, first, request = fake_setup(isolated_env, workspace_root)
    try:
        task = app.store.get_task(TaskId(first.ids['task_id']))
        run = app.store.get_run(RunId(first.ids['run_id']))
        if failure == 'disabled': app.config.workflow.input_optimization['mode'] = 'observe'
        elif failure == 'budget': app.config = replace(app.config, workflow=replace(app.config.workflow, max_repairs=0))
        elif failure == 'changed_file': (workspace_root / 'note.txt').write_text('changed')
        elif failure == 'changed_contract': task.text += ' changed'; app.store.save_task(task)
        elif failure == 'unknown': run.status = RunStatus.PENDING_RECONCILE; app.store.save_run(run)
        elif failure == 'cancel': run.cancel_requested = True; app.store.save_run(run)
        elif failure == 'pending_check': run.native['local_checks']={'job':{'status':'unknown'}}; app.store.save_run(run)
        else: app.store.fail_next_intent = True
        result = app.handle('workflow.repair', request)
        assert not result.ok
        assert app.backends['fake'].dispatch_count == 1
        assert app.store.get_task(task.id).run_count == 1
    finally: app.close()


def test_atomic_successor_cas_rejects_stale_second_worker(isolated_env, workspace_root, monkeypatch):
    app, first, request = fake_setup(isolated_env, workspace_root)
    original = app.store.commit_intent; attempts = []
    def capture(op, task, run, **kw):
        attempts.append(deepcopy((op, task, run)))
        return original(op,task,run,**kw)
    monkeypatch.setattr(app.store, 'commit_intent', capture)
    try:
        result = app.handle('workflow.repair', request); assert result.ok, result.error
        op, task, run = attempts[0]
        from asterun.ids import new_operation_id, new_run_id
        op.id = new_operation_id(); op.idempotency_key = 'other-worker'; run.id = new_run_id()
        other = SqliteStore(isolated_env / 'state.sqlite')
        try:
            with pytest.raises(AsterunError, match='不能并行'): other.commit_intent(op,task,run)
            assert other.get_task(task.id).run_count == 2
        finally: other.close()
    finally: app.close()


def test_committed_but_undispatched_successor_restarts_once(isolated_env, workspace_root, monkeypatch):
    app, first, request = fake_setup(isolated_env, workspace_root)
    original = app.store.commit_intent
    def crash(*a,**kw):
        result=original(*a,**kw)
        raise OSError('crash after atomic intent commit')
    monkeypatch.setattr(app.store, 'commit_intent', crash)
    failed=app.handle('workflow.repair',request)
    assert not failed.ok and app.backends['fake'].dispatch_count==1
    new_run=app.store.get_task(TaskId(first.ids['task_id'])).current_run_id
    app.close()
    cfg=load_config(isolated_env/'config.json')
    restarted=Application(cfg,store=SqliteStore(isolated_env/'state.sqlite'))
    try:
        replay=restarted.handle('workflow.repair',request)
        assert replay.ok and replay.data['reused']
        assert restarted.store.get_run(new_run).status=='succeeded'
        assert restarted.backends['fake'].dispatch_count==1
        assert restarted.store.get_task(TaskId(first.ids['task_id'])).run_count==2
    finally:restarted.close()


def test_queued_successor_source_change_blocks_without_budget_reset(isolated_env,workspace_root,monkeypatch):
    app,first,request=fake_setup(isolated_env,workspace_root)
    original=app._dispatch_committed
    def crash(*args): raise OSError('crash before dispatch')
    monkeypatch.setattr(app,'_dispatch_committed',crash)
    assert not app.handle('workflow.repair',request).ok
    (workspace_root/'note.txt').write_text('source changed')
    app.close()
    restarted=Application(load_config(isolated_env/'config.json'),store=SqliteStore(isolated_env/'state.sqlite'))
    try:
        restarted.poll()
        task=restarted.store.get_task(TaskId(first.ids['task_id']))
        assert restarted.store.get_run(task.current_run_id).status=='failed'
        assert task.run_count==2 and task.repair_count==1
        assert restarted.backends['fake'].dispatch_count==0
    finally:restarted.close()


def test_source_approval_survives_rejected_transition(isolated_env,workspace_root):
    path=write_config(isolated_env/'config.json',workspace_root);enable(path)
    app=Application(load_config(path))
    try:
        first=app.handle('task.submit',{'workspace':'demo','text':'test','script':'wait_input'})
        approval=app.store.find_pending_approval(RunId(first.ids['run_id']))
        request={**package(app,first),'context_strategy':'handoff_fresh'}
        assert not app.handle('workflow.repair',request).ok
        assert app.store.find_pending_approval(RunId(first.ids['run_id'])).to_dict()==approval.to_dict()
        assert app.backends['fake'].dispatch_count==1
    finally:app.close()


@pytest.mark.parametrize('drift', ['event', 'policy', 'report'])
def test_queued_handoff_revalidates_authority_and_evidence(isolated_env, workspace_root, monkeypatch, drift):
    from asterun.contracts import EventType
    from asterun.context_transition import guard
    from hashlib import sha256
    app, first, request = fake_setup(isolated_env, workspace_root)
    try:
        task = app.store.get_task(TaskId(first.ids['task_id']))
        source = app.store.get_run(RunId(first.ids['run_id']))
        if drift == 'report':
            from asterun.external_evaluation import prepare_snapshot
            bound = prepare_snapshot(task, source, workspace_root, ['note.txt'])
            raw = json.dumps({'task_id': task.id.value, 'run_id': source.id.value, 'input_hash': task.input_hash,
                              'target_hash': bound['target_hash'], 'checks': [{'name': 'check', 'passed': False, 'detail': 'fix'}]}).encode()
            (workspace_root / 'report.json').write_bytes(raw)
            task.external_evaluation = {'expected_run_id': source.id.value, 'expected_revision': task.config_revision,
                'expected_input_hash': task.input_hash, 'target_paths': ['note.txt'], 'target_hash': bound['target_hash'],
                'reports': [{'path': 'report.json', 'sha256': sha256(raw).hexdigest()}]}
            app.store.save_task(task)
            created = app.handle('workflow.handoff-create', {'task_id': task.id.value, 'expected_run_id': source.id.value,
                                   'expected_handoff_revision': 1, 'target_paths': ['note.txt']})
            assert created.ok, created.error
            request.update(expected_handoff_revision=2, expected_revision=task.config_revision,
                           expected_input_hash=task.input_hash, target_paths=['note.txt'], expected_target_hash=bound['target_hash'])
        def stop(*a): raise OSError('pause before dispatch')
        monkeypatch.setattr(app, '_dispatch_committed', stop)
        failed = app.handle('workflow.repair', request)
        assert not failed.ok
        current = app.store.get_task(task.id)
        assert current.run_count == 2, failed.error
        if drift == 'event': app._event(source.id, EventType.MESSAGE, 'core', {'message': 'late evidence'})
        elif drift == 'policy': app.config.workflow.input_optimization['context_strategies'] = ['handoff_fresh']
        else: (workspace_root / 'report.json').write_text('{}')
        with pytest.raises(AsterunError): guard(app, current, app.store.get_run(current.current_run_id))
        assert app.backends['fake'].dispatch_count == 1
    finally: app.close()


@pytest.mark.parametrize('phase,thread_count,work_count', [
    ('session_create_requested', 1, 1), ('session_bound', 2, 1),
    ('prompt_requested', 2, 1), ('prompt_started', 2, 2)])
def test_native_successor_crash_phases_do_not_replay(native_config, isolated_env, workspace_root, monkeypatch,
                                                      phase, thread_count, work_count):
    from asterun.backends.codex_runtime import CodexRuntime
    enable(native_config)
    directory = isolated_env / 'state'
    app = Application.from_paths(native_config, directory, background=True)
    first = submit(app, 'finish')
    wait_for(app, first.ids['task_id'], lambda d: d['run']['status'] == 'succeeded')
    request = {**package(app, first), 'context_strategy': 'handoff_fresh'}
    original = CodexRuntime.run
    def crash(self, *args, publish, **kwargs):
        def broken(value):
            publish(value)
            if value.get('native', {}).get('context_transition', {}).get('phase') == phase:
                raise OSError('crash after durable phase')
        return original(self, *args, publish=broken, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(CodexRuntime, 'run', crash)
        result = app.handle('workflow.repair', request); assert result.ok, result.error
        wait_for(app, first.ids['task_id'], lambda d: d['run']['status'] == 'pending_reconcile')
        app.close()
    restarted = Application.from_paths(native_config, directory, background=True)
    try:
        assert restarted.handle('workflow.repair', request).data['reused']
        assert restarted.handle('task.reconcile', {'task_id': first.ids['task_id']}).ok
        calls = [json.loads(line) for line in (workspace_root / 'native-calls.jsonl').read_text().splitlines()]
        assert sum(c.get('method') == 'thread/start' for c in calls) == thread_count
        assert sum(c.get('method') == 'turn/start' for c in calls) == work_count
        run = restarted.store.get_run(RunId(result.ids['run_id']))
        # 协议替身随连接关闭而停止，已受理工作轮次也没有完成证据。
        assert run.status == 'pending_reconcile'
        assert restarted.store.get_task(TaskId(first.ids['task_id'])).run_count == 2
    finally: restarted.close()


def test_other_task_unresolved_local_check_blocks_context_change(isolated_env, workspace_root):
    app, first, request = fake_setup(isolated_env, workspace_root)
    try:
        other = app.handle('task.submit', {'workspace': 'demo', 'text': 'another task'})
        assert other.ok
        run = app.store.get_run(RunId(other.ids['run_id']))
        run.native['local_checks'] = {'job': {'status': 'unknown'}}
        app.store.save_run(run)
        failed = app.handle('workflow.repair', request)
        assert not failed.ok and failed.error['code'] == 'REMOTE_STATE_UNKNOWN'
        assert app.backends['fake'].dispatch_count == 2
    finally: app.close()


def test_queued_handoff_rechecks_read_permission(isolated_env, workspace_root, monkeypatch):
    from asterun.context_transition import guard
    from asterun.policy import Decision
    app, first, request = fake_setup(isolated_env, workspace_root)
    try:
        def stop(*a): raise OSError('pause before dispatch')
        monkeypatch.setattr(app, '_dispatch_committed', stop)
        assert not app.handle('workflow.repair', request).ok
        task = app.store.get_task(TaskId(first.ids['task_id']))
        original = app.policy.authorize
        monkeypatch.setattr(app.policy, 'authorize', lambda principal, action, workspace:
                            Decision(False, 'revoked') if action == 'workspace.read' else original(principal, action, workspace))
        with pytest.raises(AsterunError): guard(app, task, app.store.get_run(task.current_run_id))
        assert app.backends['fake'].dispatch_count == 1
    finally: app.close()


@pytest.mark.parametrize('queued', [False, True])
def test_sync_compatibility_never_bypasses_context_runtime(native_config, isolated_env, workspace_root, monkeypatch, queued):
    enable(native_config)
    directory = isolated_env / 'state'
    app = Application.from_paths(native_config, directory, background=True)
    first = submit(app, 'finish')
    wait_for(app, first.ids['task_id'], lambda d: d['run']['status'] == 'succeeded')
    request = {**package(app, first), 'context_strategy': 'native_compact'}
    if queued:
        def stop(*a): raise OSError('before dispatch')
        monkeypatch.setattr(app, '_dispatch_committed', stop)
        assert not app.handle('workflow.repair', request).ok
    app.close()
    sync = Application.from_paths(native_config, directory, background=False)
    try:
        if queued:
            sync.poll()
            task = sync.store.get_task(TaskId(first.ids['task_id']))
            assert sync.store.get_run(task.current_run_id).status == 'failed'
        else:
            result = sync.handle('workflow.repair', request)
            assert not result.ok and result.error['code'] == 'CAPABILITY_UNSUPPORTED'
        calls = [json.loads(line) for line in (workspace_root / 'native-calls.jsonl').read_text().splitlines()]
        assert sum(c.get('method') == 'turn/start' for c in calls) == 1
        assert not any(c.get('method') == 'thread/compact/start' for c in calls)
    finally: sync.close()


@pytest.mark.parametrize('binding', ['missing', 'conflicting'])
def test_source_native_reference_must_be_complete_and_consistent(native_config, isolated_env, monkeypatch, binding):
    enable(native_config)
    app = Application.from_paths(native_config, isolated_env / 'state', background=True)
    try:
        first = submit(app, 'finish')
        wait_for(app, first.ids['task_id'], lambda d: d['run']['status'] == 'succeeded')
        run = app.store.get_run(RunId(first.ids['run_id']))
        if binding == 'missing': run.native.pop('backend_session_id')
        else: run.native['backend_session_id'] = 'different-thread'
        app.store.save_run(run)
        request = {**package(app, first), 'context_strategy': 'native_compact'}
        failed = app.handle('workflow.repair', request)
        assert not failed.ok and failed.error['code'] == 'BINDING_MISMATCH'
        assert app.store.get_task(TaskId(first.ids['task_id'])).run_count == 1
    finally: app.close()
