"""后台检查的派发、取消、缓存和重启边界，不消耗模型额度。"""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from threading import Event
import os
import sys
import time

import pytest

from asterun.application import Application
from asterun import local_checks
from asterun.executor import Executor
from asterun.ids import RunId
from asterun.sqlite_store import SqliteStore
from tests.test_local_checks import setup, check


def background(app):
    app.executor = Executor()
    app.local_checks.executor = Executor()
    return app


def submit(app, bound, key='one'):
    response = app.handle('workflow.check', {**bound, 'check_name': 'compile', 'async': True, 'idempotency_key': key})
    assert response.ok, response.error
    return response.data


def get(app, bound, job, cancel=False):
    response = app.handle('workflow.check-cancel' if cancel else 'workflow.check-status',
                          {'task_id': bound['task_id'], 'expected_run_id': bound['expected_run_id'], 'job_id': job['job_id']})
    assert response.ok, response.error
    return response.data


def finish(app, bound, job):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        value = get(app, bound, job)
        if value['status'] not in {'running', 'cancelling'}:
            return value
        time.sleep(.01)
    raise AssertionError('check still running')


def test_async_fast_idempotent_and_core_remains_available(isolated_env, workspace_root, monkeypatch):
    app, bound = setup(isolated_env, workspace_root, 'import time\ntime.sleep(.4)\nprint("done")')
    background(app)
    calls = []
    execute = local_checks.execute_check
    def counted(*args, **kwargs):
        calls.append(1)
        return execute(*args, **kwargs)
    monkeypatch.setattr(local_checks, 'execute_check', counted)
    try:
        started = time.monotonic()
        job = submit(app, bound)
        assert time.monotonic() - started < .3
        assert submit(app, bound)['job_id'] == job['job_id']
        assert app.handle('scheduler.status', {}).ok
        done = finish(app, bound, job)
        assert done['status'] == 'completed' and done['result']['passed']
        assert submit(app, bound)['job_id'] == job['job_id']
        assert len(calls) == 1
        assert len(app.store.list_runs(app._require_task(bound['task_id']).id)) == 1
        assert app._require_task(bound['task_id']).acceptance.value == 'pending'
    finally:
        app.close()


def test_cancel_precedes_queued_success_and_no_report(isolated_env, workspace_root):
    app, bound = setup(isolated_env, workspace_root, 'print("done")')
    background(app)
    try:
        job = submit(app, bound)
        deadline = time.monotonic() + 5
        while app.local_checks.executor.updates.empty() and time.monotonic() < deadline:
            time.sleep(.01)
        done = get(app, bound, job, cancel=True)
        assert done['status'] == 'cancelled' and 'result' not in done
    finally:
        app.close()


def test_slots_and_process_cancellation(isolated_env, workspace_root):
    app, bound = setup(isolated_env, workspace_root, 'import time\ntime.sleep(30)', timeout=30)
    background(app)
    try:
        first = submit(app, bound)
        second = submit(app, bound, 'two')
        denied = app.handle('workflow.check', {**bound, 'check_name': 'compile', 'async': True, 'idempotency_key': 'three'})
        assert not denied.ok and denied.error['code'] == 'RATE_LIMITED'
        sync = app.handle('workflow.check', {**bound, 'check_name':'compile'})
        assert not sync.ok and sync.error['code'] == 'RATE_LIMITED'
        get(app, bound, first, cancel=True)
        assert finish(app, bound, first)['status'] == 'cancelled'
        get(app, bound, second, cancel=True)
        assert finish(app, bound, second)['status'] == 'cancelled'
        assert not app.local_checks.active
    finally:
        app.close()


def test_source_drift_and_other_run_invalidate_result(isolated_env, workspace_root):
    app, bound = setup(isolated_env, workspace_root, 'print("done")')
    background(app)
    try:
        job = submit(app, bound)
        assert finish(app, bound, job)['status'] == 'completed'
        (workspace_root / 'note.txt').write_text('drift')
        value = get(app, bound, job)
        assert value['status'] == 'stale' and 'result' not in value
    finally:
        app.close()


def test_restart_preserves_unconfirmed_intent_without_dispatch(isolated_env, workspace_root, monkeypatch):
    app, bound = setup(isolated_env, workspace_root, 'print("done")')
    background(app)
    database = isolated_env / 'checks.sqlite'
    store = SqliteStore(database)
    task = app._require_task(bound['task_id'])
    run = app.store.get_run(task.current_run_id)
    store.bootstrap_applied_revision(app.config.revision)
    store.save_task(task); store.save_run(run)
    store.save_session(app.store.get_session(task.conversation_id))
    store.save_operation(app.store.find_operation_for_run(run.id))
    app.store = store
    monkeypatch.setattr(app.local_checks.executor, 'start', lambda *args: None)
    job = submit(app, bound)
    # 模拟已持久意图后崩溃：不调用正常 close，不接收终态。
    store.close()
    restored = Application(app.config, store=SqliteStore(database), background=True)
    try:
        value = get(restored, bound, job)
        assert value['status'] == 'unknown'
        assert submit(restored, bound)['job_id'] == job['job_id']
        assert not restored.local_checks.executor.workers
    finally:
        restored.close()


def test_failed_intent_never_executes(isolated_env, workspace_root, monkeypatch):
    app, bound = setup(isolated_env, workspace_root, 'print("done")')
    background(app)
    monkeypatch.setattr(app.store, 'save_run', lambda *args: (_ for _ in ()).throw(OSError('disk full')))
    try:
        response = app.handle('workflow.check', {**bound, 'check_name': 'compile', 'async': True, 'idempotency_key': 'one'})
        assert not response.ok
        assert not app.local_checks.executor.workers and not app.local_checks.active
    finally:
        app.close()


def test_cache_reuses_failure_and_runtime_change_invalidates(isolated_env, workspace_root, monkeypatch):
    app, bound = setup(isolated_env, workspace_root, 'print("type error")\nraise SystemExit(2)')
    spec = app.config.workflow.local_checks['compile']
    spec['cache'] = True
    # 小型运行时夹具，完整枚举；实际 OS 沙箱保持原实现。
    runtime = isolated_env / 'runtime'; runtime.mkdir(); dependency = runtime / 'dependency'; dependency.write_text('v1')
    monkeypatch.setattr(local_checks, 'runtime_paths', lambda *args: [runtime, Path(sys.executable).resolve()])
    # execute 仍使用原有 OS 沙箱；此测试用不依赖 Python 搜索路径的合成执行结果。
    calls = []
    def execute(*args, **kwargs):
        calls.append(1)
        return {'passed': False, 'diagnostic': 'type error', 'acceptance_unchanged': True, 'independent_review': False}
    monkeypatch.setattr(local_checks, 'execute_check', execute)
    try:
        one = check(app, bound)
        two = check(app, bound)
        assert not one['cache_hit'] and two['cache_hit'] and len(calls) == 1
        st = dependency.stat(); dependency.write_text('v2'); os.utime(dependency, ns=(st.st_atime_ns, st.st_mtime_ns))
        three = check(app, bound)
        assert not three['cache_hit'] and len(calls) == 2
        spec['cache'] = False
        assert not check(app, bound)['cache_hit'] and len(calls) == 3
    finally:
        app.close()


def test_runtime_unreadable_disables_cache_and_async_requires_resident(isolated_env, workspace_root, monkeypatch):
    app, bound = setup(isolated_env, workspace_root, 'print("done")')
    spec = app.config.workflow.local_checks['compile']; spec['cache'] = True
    monkeypatch.setattr(local_checks, 'runtime_paths', lambda *args: [isolated_env / 'missing'])
    assert local_checks.runtime_fingerprint(spec) is None
    result = app.handle('workflow.check', {**bound, 'check_name': 'compile', 'async': True, 'idempotency_key': 'one'})
    assert not result.ok and result.error['code'] == 'INVALID_REQUEST'
    app.close()


def test_async_status_schema_cli_mcp_and_permission(isolated_env, workspace_root):
    from asterun.cli import build_parser, _load_request
    from asterun.mcp_server import handle_rpc
    from asterun.policy import Decision
    app, bound = setup(isolated_env, workspace_root, 'print("done")')
    background(app)
    try:
        job = submit(app, bound)
        args = build_parser().parse_args(['workflow-check-status', bound['task_id'], '--run-id', bound['expected_run_id'], '--job-id', job['job_id']])
        payload = _load_request(args)
        rpc = handle_rpc(app, {'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'workflow_check_status','arguments':payload}})
        assert not rpc.get('error')
        assert not app.handle('workflow.check-status', {'task_id':bound['task_id']}).ok
        class Deny:
            def authorize(self, principal, action, resource):
                return Decision(False, 'SCOPE_DENIED')
        app.policy = Deny()
        assert app.handle('workflow.check-status', payload).error['code'] == 'SCOPE_DENIED'
    finally:
        app.close()


def test_resident_cli_client_exit_idle_persistence_mcp_and_restart(isolated_env, workspace_root):
    import json
    import sqlite3
    import subprocess
    import tempfile
    from asterun.service import LocalClient
    app, _ = setup(isolated_env, workspace_root, 'import time\ntime.sleep(.4)\nprint("completed after client exit")')
    app.close()
    cfg = isolated_env / 'cfg.json'
    config = json.loads(cfg.read_text())
    config['workflow']['checkpoint_paths'] = {'demo':['check.py','note.txt']}
    cfg.write_text(json.dumps(config))
    with tempfile.TemporaryDirectory(prefix='asterun-check-ipc-') as directory:
        state = Path(directory)
        command = [sys.executable,'-m','asterun','--config',str(cfg),'--state-dir',str(state)]
        client = LocalClient(state / 'asterun.sock')
        def start():
            process = subprocess.Popen([*command,'serve'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            deadline = time.monotonic() + 5
            while not client.path.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            assert client.path.exists()
            return process
        def cli(*args):
            result = subprocess.run([*command,'--connect',*args],capture_output=True,text=True,timeout=5)
            assert result.returncode == 0, result.stdout + result.stderr
            return json.loads(result.stdout)
        process = start()
        try:
            task = cli('task-submit','--workspace','demo','--text','check lifecycle')
            tid, rid = task['ids']['task_id'], task['ids']['run_id']
            snap = cli('workflow-snapshot',tid,'--target','check.py','--target','note.txt')['data']
            bound = {'task_id':tid,'expected_run_id':rid,'expected_revision':snap['revision'],
                     'expected_input_hash':snap['input_hash'],'expected_target_hash':snap['target_hash'],
                     'target_paths':snap['target_paths'],'check_name':'compile','async':True,'idempotency_key':'resident-check'}
            request = isolated_env / 'request.json';request.write_text(json.dumps(bound))
            job = cli('workflow-check',tid,'--request',str(request))['data']
            # 不发任何观察请求，核心必须自己完成并保存。
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                with sqlite3.connect(f'file:{state / "asterun.sqlite"}?mode=ro',uri=True) as db:
                    row = json.loads(db.execute('SELECT payload FROM runs WHERE id=?',(rid,)).fetchone()[0])
                db.close()
                if row['native']['local_checks'][job['job_id']]['status'] == 'completed':
                    break
                time.sleep(.02)
            else:
                raise AssertionError('idle check completion was not persisted')
            rpc = {'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'workflow_check_status',
                   'arguments':{'task_id':tid,'expected_run_id':rid,'job_id':job['job_id']}}}
            response = subprocess.run([*command,'--connect','mcp'],input=json.dumps(rpc)+'\n',text=True,capture_output=True,timeout=5)
            envelope = json.loads(json.loads(response.stdout)['result']['content'][0]['text'])
            assert envelope['ok'] and envelope['data']['result']['passed']
            checkpoint = cli('workflow-checkpoint',tid,'--run-id',rid)
            assert checkpoint['data']['source_matches']
        finally:
            process.terminate();out,err=process.communicate(timeout=8)
            assert process.returncode == 0,(out,err)
        process = start()
        try:
            replay = cli('workflow-check',tid,'--request',str(request))['data']
            assert replay['job_id'] == job['job_id'] and replay['status'] == 'completed'
        finally:
            process.terminate();out,err=process.communicate(timeout=8)
            assert process.returncode == 0,(out,err)


def test_terminal_job_write_failure_becomes_unknown_without_retry(isolated_env, workspace_root, monkeypatch):
    app, bound = setup(isolated_env, workspace_root, 'print("done")')
    background(app)
    save = app.store.save_run
    failed = []
    def fail_once(run):
        jobs = run.native.get('local_checks', {})
        if any(j['status'] == 'completed' for j in jobs.values()) and not failed:
            failed.append(True)
            raise OSError('disk full')
        return save(run)
    monkeypatch.setattr(app.store, 'save_run', fail_once)
    try:
        job = submit(app, bound)
        payload = {'task_id':bound['task_id'],'expected_run_id':bound['expected_run_id'],'job_id':job['job_id']}
        deadline = time.monotonic() + 6
        observed = None
        while time.monotonic() < deadline:
            observed = app.handle('workflow.check-status', payload)
            if observed.ok and observed.data['status'] == 'unknown':
                break
            time.sleep(.01)
        assert failed and observed.ok and observed.data['status'] == 'unknown'
        assert submit(app, bound)['job_id'] == job['job_id']
        assert not app.local_checks.active
    finally:
        app.close()


def test_check_summaries_survive_double_compaction_without_diagnostics():
    from asterun.observe import compact_task_snapshot
    value = {'task':{'id':'task'},'run':{'id':'run','native':{'local_checks':{
        str(i):{'job_id':str(i),'status':'completed','stage':'consumer','passed':False,
                'result':{'diagnostic':'PRIVATE'}} for i in range(8)}}}}
    compact = compact_task_snapshot(value)
    assert compact == compact_task_snapshot(compact)
    assert len(compact['run']['native']['local_checks']) == 4
    assert compact['run']['native']['local_checks_count'] == 8
    assert 'PRIVATE' not in str(compact)


def test_implicit_runtime_read_roots_cannot_include_source(isolated_env, workspace_root, monkeypatch):
    app, bound = setup(isolated_env, workspace_root, 'print("done")')
    monkeypatch.setattr(local_checks, 'runtime_paths', lambda *args: [workspace_root.parent])
    try:
        result = app.handle('workflow.check', {**bound,'check_name':'compile'})
        assert not result.ok and result.error['code'] == 'INVALID_REQUEST'
    finally:
        app.close()


def test_configured_single_check_slot_is_enforced(isolated_env, workspace_root):
    app, bound = setup(isolated_env, workspace_root, 'import time\ntime.sleep(30)', timeout=30)
    app.config.scheduler.local_check_concurrency = 1
    background(app)
    try:
        first = submit(app, bound)
        denied = app.handle('workflow.check', {**bound, 'check_name': 'compile', 'async': True, 'idempotency_key': 'second'})
        assert not denied.ok and denied.error['code'] == 'RATE_LIMITED'
        get(app, bound, first, cancel=True)
        assert finish(app, bound, first)['status'] == 'cancelled'
    finally:
        app.close()
