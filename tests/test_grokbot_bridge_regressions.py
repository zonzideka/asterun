from __future__ import annotations

import argparse
from contextlib import closing
import importlib.util
from pathlib import Path
import sqlite3

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'integrations/grokbot/scripts/bridge.py'
spec = importlib.util.spec_from_file_location('grokbot_bridge_regressions', SCRIPT)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


@pytest.fixture
def ledger(tmp_path):
    state = bridge.Ledger(tmp_path.resolve() / 'private' / 'ledger.sqlite')
    add_binding(state)
    yield state
    state.close()


def add_binding(ledger, name='primary'):
    ledger.conn.execute(
        'INSERT INTO bindings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (name, '/fixture/asterun', '/fixture/config', '/fixture/state', 'local',
         '["demo"]', '["fake"]', 'chat-' + name, 'fingerprint-' + name, bridge.utc_now()),
    )


def args(**kwargs):
    return argparse.Namespace(binding='primary', **kwargs)


def submit_args(tmp_path, *, binding='primary', request='request', text='input'):
    path = tmp_path / ('input-' + binding + '-' + request)
    path.write_text(text)
    return argparse.Namespace(binding=binding, request_id=request, workspace='demo',
                              backend='fake', conversation_id=None, input=str(path))


def envelope(data, **extra):
    return {'ok': True, 'data': data, 'error': None, **extra}


def submit_response(task='task-1', run='run-1'):
    return envelope({'task': {'id': task}, 'run': {'id': run}},
                    ids={'task_id': task, 'run_id': run, 'conversation_id': 'conversation'})


def seed_task(ledger, task='task-1', run='run-1', request=None):
    request = request or task
    now = bridge.utc_now()
    ledger.conn.execute(
        'INSERT INTO submissions(binding, request_id, input_digest, workspace, backend, '
        'task_id, run_id, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        ('primary', request, 'digest', 'demo', 'fake', task, run, 'submitted', now, now),
    )
    ledger.conn.execute(
        'INSERT INTO observations(binding, task_id, request_id, run_id, updated_at) VALUES (?, ?, ?, ?, ?)',
        ('primary', task, request, run, now),
    )


class PollCore:
    def __init__(self, *, status='running', run='run-1'):
        self.status = status
        self.run = run
        self.calls = []
        self.fail = set()
        self.event_run = None
        self.task_overrides = {}

    def __call__(self, executable, argv, *, timeout, **kwargs):
        self.calls.append((argv, timeout))
        if 'task-get' in argv:
            task_id = argv[argv.index('task-get') + 1]
            if task_id in self.fail:
                raise bridge.BridgeError('CORE_UNAVAILABLE', 'fixture failure', retryable=True)
            return envelope({'task': {'id': task_id, 'workspace': 'demo', 'backend': 'fake',
                                      'acceptance': 'pending', **self.task_overrides},
                             'run': {'id': self.run, 'task_id': task_id, 'status': self.status,
                                     'summary': 'fixture result'}})
        assert 'task-events' in argv
        task_id = argv[argv.index('task-events') + 1]
        cursor = int(argv[argv.index('--cursor') + 1])
        return envelope({'task_id': task_id, 'run_id': self.event_run or self.run,
                         'events': [], 'next_cursor': cursor})


def test_submission_keys_are_scoped_and_lost_response_reuses_remote_task(ledger, tmp_path, monkeypatch):
    add_binding(ledger, 'secondary')
    accepted = {}
    keys = []
    lose_response = True

    def core(executable, argv, **kwargs):
        nonlocal lose_response
        key = argv[argv.index('--idempotency-key') + 1]
        keys.append(key)
        accepted.setdefault(key, 'task-' + str(len(accepted)))
        if lose_response:
            lose_response = False
            raise bridge.BridgeError('CORE_RESPONSE_INVALID', 'response lost after acceptance')
        return submit_response(accepted[key])

    monkeypatch.setattr(bridge, 'invoke_asterun', core)
    request = submit_args(tmp_path)
    with pytest.raises(bridge.BridgeError, match='') as error:
        bridge.cmd_submit(ledger, request, 10)
    assert error.value.code == 'CORE_RESULT_UNKNOWN'
    first = bridge.cmd_submit(ledger, request, 10)
    second = bridge.cmd_submit(ledger, submit_args(tmp_path, binding='secondary'), 10)
    assert keys[0] == keys[1]
    assert keys[0] != keys[2]
    assert len(accepted) == 2
    assert first['task_id'] != second['task_id']
    assert bridge.cmd_submit(ledger, request, 10)['reused'] is True
    assert len(keys) == 3


@pytest.mark.parametrize('submitted', [False, True])
def test_v1_intent_preserves_original_core_key_on_migration(tmp_path, monkeypatch, submitted):
    directory = tmp_path.resolve() / 'legacy'
    directory.mkdir(mode=0o700)
    path = directory / 'ledger.sqlite'
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE submissions (binding TEXT, request_id TEXT, input_digest TEXT, '
                 'workspace TEXT, backend TEXT, conversation_id TEXT, task_id TEXT, run_id TEXT, '
                 'result_conversation_id TEXT, state TEXT, created_at TEXT, updated_at TEXT, '
                 'PRIMARY KEY(binding, request_id))')
    digest = bridge.input_digest('demo', 'fake', 'input', '')
    conn.execute('INSERT INTO submissions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                 ('primary', 'request', digest, 'demo', 'fake', '',
                  'task-legacy' if submitted else None, 'run-legacy' if submitted else None,
                  None, 'submitted' if submitted else 'intended', 'now', 'now'))
    conn.commit()
    conn.close()
    path.chmod(0o600)
    migrated = bridge.Ledger(path)
    try:
        add_binding(migrated)
        keys = []
        def core(executable, argv, **kwargs):
            keys.append(argv[argv.index('--idempotency-key') + 1])
            return submit_response()
        monkeypatch.setattr(bridge, 'invoke_asterun', core)
        bridge.cmd_submit(migrated, submit_args(tmp_path), 10)
        assert keys == ([] if submitted else ['request'])
        assert migrated.conn.execute('SELECT core_idempotency_key FROM submissions').fetchone()[0] == 'request'
        assert migrated.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == '2'
    finally:
        migrated.close()


@pytest.mark.parametrize('conflicting', [False, True])
def test_overlapping_submit_cannot_overwrite_known_ids_or_rewind_observation(
        ledger, tmp_path, monkeypatch, conflicting):
    def core(executable, argv, **kwargs):
        with closing(sqlite3.connect(ledger.path)) as other, other:
            other.execute("UPDATE submissions SET task_id='task-1', run_id='run-1', "
                          "result_conversation_id='conversation', state='submitted'")
            other.execute('INSERT INTO observations(binding, task_id, request_id, run_id, '
                          'events_cursor, observing, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
                          ('primary', 'task-1', 'request', 'run-new', 9, 0, 'later'))
        return submit_response('task-conflict' if conflicting else 'task-1')
    monkeypatch.setattr(bridge, 'invoke_asterun', core)
    if conflicting:
        with pytest.raises(bridge.BridgeError) as error:
            bridge.cmd_submit(ledger, submit_args(tmp_path), 10)
        assert error.value.code == 'CORE_RESPONSE_INVALID'
    else:
        assert bridge.cmd_submit(ledger, submit_args(tmp_path), 10)['reused'] is True
    saved = ledger.conn.execute('SELECT * FROM submissions').fetchone()
    assert saved['task_id'] == 'task-1'
    observed = ledger.conn.execute('SELECT * FROM observations').fetchone()
    assert (observed['run_id'], observed['events_cursor'], observed['observing']) == ('run-new', 9, 0)


@pytest.mark.parametrize('first_fails', [False, True])
def test_poll_rotates_with_identical_timestamps_and_on_failure(ledger, monkeypatch, first_fails):
    seed_task(ledger, 'task-a')
    seed_task(ledger, 'task-b')
    core = PollCore()
    if first_fails:
        core.fail.add('task-a')
    monkeypatch.setattr(bridge, 'invoke_asterun', core)
    monkeypatch.setattr(bridge, 'utc_now', lambda: 'same-time')
    first = bridge.cmd_poll(ledger, args(limit=1), 10)
    second = bridge.cmd_poll(ledger, args(limit=1), 10)
    assert ('errors' if first_fails else 'observed') in first
    assert second['observed'][0]['task_id'] == 'task-b'
    assert [argv[-1] for argv, _ in core.calls if 'task-get' in argv] == ['task-a', 'task-b']


def test_poll_deadline_does_not_rotate_unattempted_tasks(ledger, monkeypatch):
    seed_task(ledger, 'task-a')
    seed_task(ledger, 'task-b')
    core = PollCore()
    clock = [0.0]
    def slow_core(executable, argv, **kwargs):
        response = core(executable, argv, **kwargs)
        clock[0] += 0.6
        return response
    monkeypatch.setattr(bridge.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(bridge, 'invoke_asterun', slow_core)
    first = bridge.cmd_poll(ledger, args(limit=2), 1)
    assert len(first['observed']) == 1
    assert len(core.calls) == 2
    assert core.calls[1][1] == pytest.approx(0.4)
    second = bridge.cmd_poll(ledger, args(limit=1), 1)
    assert second['observed'][0]['task_id'] == 'task-b'


@pytest.mark.parametrize('status', ['waiting_auth', 'pending_reconcile'])
def test_poll_preserves_core_blocking_status(ledger, monkeypatch, status):
    seed_task(ledger)
    monkeypatch.setattr(bridge, 'invoke_asterun', PollCore(status=status))
    result = bridge.cmd_poll(ledger, args(limit=1), 10)
    assert result['observed'][0]['status'] == status
    assert ledger.conn.execute('SELECT status FROM deliveries').fetchone()[0] == status


def test_run_change_between_snapshot_and_events_does_not_publish_old_result(ledger, monkeypatch):
    seed_task(ledger)
    core = PollCore(status='succeeded')
    core.event_run = 'run-new'
    monkeypatch.setattr(bridge, 'invoke_asterun', core)
    result = bridge.cmd_poll(ledger, args(limit=1), 10)
    assert result['errors'][0]['code'] == 'CORE_SNAPSHOT_CHANGED'
    assert ledger.conn.execute('SELECT count(*) FROM deliveries').fetchone()[0] == 0
    assert ledger.conn.execute('SELECT run_id FROM observations').fetchone()[0] == 'run-1'
    core.run = 'run-new'
    assert bridge.cmd_poll(ledger, args(limit=1), 10)['observed'][0]['run_id'] == 'run-new'


@pytest.mark.parametrize('override', [{'id': 'other-task'}, {'workspace': 'other'}, {'backend': 'other'}])
def test_poll_rejects_snapshot_outside_recorded_scope(ledger, monkeypatch, override):
    seed_task(ledger)
    core = PollCore(status='succeeded')
    core.task_overrides = override
    monkeypatch.setattr(bridge, 'invoke_asterun', core)
    result = bridge.cmd_poll(ledger, args(limit=1), 10)
    assert result['errors']
    assert ledger.conn.execute('SELECT count(*) FROM deliveries').fetchone()[0] == 0


def acknowledge(ledger, delivery_key):
    item = bridge.cmd_claim(ledger, args(delivery_key=delivery_key), 10)
    return bridge.cmd_ack(ledger, args(delivery_key=delivery_key,
                                      message_id='message-' + item['run_id'],
                                      content_sha256=item['content_sha256']), 10)


def test_old_terminal_receipt_does_not_stop_new_run_observation(ledger, monkeypatch):
    seed_task(ledger)
    core = PollCore(status='succeeded')
    monkeypatch.setattr(bridge, 'invoke_asterun', core)
    first = bridge.cmd_poll(ledger, args(limit=1), 10)['observed'][0]
    core.run = 'run-new'
    acknowledge(ledger, first['delivery_key'])
    assert ledger.conn.execute('SELECT observing FROM observations').fetchone()[0] == 1
    second = bridge.cmd_poll(ledger, args(limit=1), 10)
    assert second['active_count'] == 1
    assert second['observed'][0]['run_id'] == 'run-new'
    acknowledge(ledger, second['observed'][0]['delivery_key'])
    assert bridge.cmd_poll(ledger, args(limit=1), 10)['needs_followup'] is False


def test_overlapping_poll_discards_stale_snapshot(ledger, monkeypatch):
    seed_task(ledger)
    core = PollCore(status='succeeded')
    def overlapping(executable, argv, **kwargs):
        response = core(executable, argv, **kwargs)
        if 'task-events' in argv:
            ledger.conn.execute("UPDATE observations SET poll_sequence=poll_sequence+1, run_id='run-new'")
        return response
    monkeypatch.setattr(bridge, 'invoke_asterun', overlapping)
    result = bridge.cmd_poll(ledger, args(limit=1), 10)
    assert result['observed'] == []
    assert ledger.conn.execute('SELECT count(*) FROM deliveries').fetchone()[0] == 0
    assert ledger.conn.execute('SELECT run_id FROM observations').fetchone()[0] == 'run-new'


def test_unconfirmed_delivery_is_not_automatically_requeued(ledger, monkeypatch):
    seed_task(ledger)
    monkeypatch.setattr(bridge, 'invoke_asterun', PollCore(status='succeeded'))
    key = bridge.cmd_poll(ledger, args(limit=1), 10)['observed'][0]['delivery_key']
    assert bridge.cmd_claim(ledger, args(delivery_key=key), 10)['already_claimed'] is False
    result = bridge.cmd_poll(ledger, args(limit=1), 10)
    assert result['unconfirmed'] == 1 and result['pending_delivery'] == 0
    assert bridge.cmd_claim(ledger, args(delivery_key=key), 10)['already_claimed'] is True


def test_new_private_leaf_under_normal_home_permissions(tmp_path):
    ancestor = tmp_path.resolve() / 'home'
    ancestor.mkdir(mode=0o755)
    target = ancestor / '.local' / 'share' / 'bridge' / 'ledger.sqlite'
    ledger = bridge.Ledger(target)
    ledger.close()
    assert ancestor.stat().st_mode & 0o777 == 0o755
    assert target.parent.stat().st_mode & 0o777 == 0o700
    assert target.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('timeout', ['nan', 'inf', '-inf'])
def test_nonfinite_timeout_rejected_before_ledger_creation(tmp_path, timeout, capsys):
    path = tmp_path / 'unused' / 'ledger.sqlite'
    assert bridge.main(['--ledger', str(path), '--timeout=' + timeout, 'status', '--binding', 'primary']) == 2
    assert 'INVALID_REQUEST' in capsys.readouterr().out
    assert not path.exists()


@pytest.mark.parametrize('old_status,new_status,new_run', [
    ('waiting_input', 'succeeded', 'run-1'),
    ('succeeded', 'running', 'run-new'),
])
def test_current_snapshot_supersedes_pending_old_state(ledger, monkeypatch, old_status, new_status, new_run):
    seed_task(ledger)
    core = PollCore(status=old_status)
    monkeypatch.setattr(bridge, 'invoke_asterun', core)
    old = bridge.cmd_poll(ledger, args(limit=1), 10)['observed'][0]['delivery_key']
    core.status, core.run = new_status, new_run
    bridge.cmd_poll(ledger, args(limit=1), 10)
    state = ledger.conn.execute('SELECT delivery_state FROM deliveries WHERE delivery_key=?', (old,)).fetchone()[0]
    assert state == 'superseded'
    assert old not in [item['delivery_key'] for item in bridge.cmd_inbox(ledger, args(limit=10), 10)['items']]
    with pytest.raises(bridge.BridgeError) as error:
        bridge.cmd_claim(ledger, args(delivery_key=old), 10)
    assert error.value.code == 'DELIVERY_STATE'


def test_new_snapshot_keeps_unconfirmed_old_send_for_reconciliation(ledger, monkeypatch):
    seed_task(ledger)
    core = PollCore(status='waiting_input')
    monkeypatch.setattr(bridge, 'invoke_asterun', core)
    old = bridge.cmd_poll(ledger, args(limit=1), 10)['observed'][0]['delivery_key']
    bridge.cmd_claim(ledger, args(delivery_key=old), 10)
    core.status, core.run = 'succeeded', 'run-new'
    result = bridge.cmd_poll(ledger, args(limit=1), 10)
    assert result['unconfirmed'] == 1 and result['pending_delivery'] == 1
    assert ledger.conn.execute('SELECT delivery_state FROM deliveries WHERE delivery_key=?', (old,)).fetchone()[0] == 'sending'
    assert bridge.cmd_claim(ledger, args(delivery_key=old), 10)['already_claimed'] is True


def test_superseded_unsent_state_can_become_current_again(ledger, monkeypatch):
    seed_task(ledger)
    core = PollCore(status='waiting_auth')
    monkeypatch.setattr(bridge, 'invoke_asterun', core)
    key = bridge.cmd_poll(ledger, args(limit=1), 10)['observed'][0]['delivery_key']
    core.status = 'running'
    bridge.cmd_poll(ledger, args(limit=1), 10)
    assert ledger.conn.execute('SELECT delivery_state FROM deliveries').fetchone()[0] == 'superseded'
    core.status = 'waiting_auth'
    result = bridge.cmd_poll(ledger, args(limit=1), 10)
    assert result['observed'][0]['delivery_key'] == key
    assert result['pending_delivery'] == 1
    assert bridge.cmd_claim(ledger, args(delivery_key=key), 10)['already_claimed'] is False


def test_independent_ledgers_do_not_share_core_tasks(ledger, tmp_path, monkeypatch):
    other = bridge.Ledger(tmp_path.resolve() / 'independent' / 'ledger.sqlite')
    add_binding(other)
    accepted = {}
    keys = []
    def core(executable, argv, **kwargs):
        key = argv[argv.index('--idempotency-key') + 1]
        keys.append(key)
        accepted.setdefault(key, 'task-' + str(len(accepted)))
        return submit_response(accepted[key])
    monkeypatch.setattr(bridge, 'invoke_asterun', core)
    try:
        request = submit_args(tmp_path)
        first = bridge.cmd_submit(ledger, request, 10)
        second = bridge.cmd_submit(other, request, 10)
        assert first['task_id'] != second['task_id']
        assert keys[0] != keys[1]
        assert len(accepted) == 2
    finally:
        other.close()


def test_ledger_reopen_preserves_namespace_and_pending_key(tmp_path, monkeypatch):
    path = tmp_path.resolve() / 'reopen' / 'ledger.sqlite'
    current = bridge.Ledger(path)
    add_binding(current)
    original_namespace = current.namespace
    keys = []
    lose_response = True
    def core(executable, argv, **kwargs):
        nonlocal lose_response
        keys.append(argv[argv.index('--idempotency-key') + 1])
        if lose_response:
            lose_response = False
            raise bridge.BridgeError('CORE_RESPONSE_INVALID', 'accepted response lost')
        return submit_response('task-original')
    monkeypatch.setattr(bridge, 'invoke_asterun', core)
    request = submit_args(tmp_path)
    try:
        with pytest.raises(bridge.BridgeError):
            bridge.cmd_submit(current, request, 10)
    finally:
        current.close()
    reopened = bridge.Ledger(path)
    try:
        assert reopened.namespace == original_namespace
        assert bridge.cmd_submit(reopened, request, 10)['task_id'] == 'task-original'
        assert keys[0] == keys[1]
        assert bridge.cmd_submit(reopened, request, 10)['reused'] is True
        assert len(keys) == 2
    finally:
        reopened.close()


def test_copied_ledger_keeps_namespace_for_new_requests(ledger, tmp_path, monkeypatch):
    directory = tmp_path.resolve() / 'copied'
    directory.mkdir(mode=0o700)
    path = directory / 'ledger.sqlite'
    with closing(sqlite3.connect(path)) as target, target:
        ledger.conn.backup(target)
    path.chmod(0o600)
    copied = bridge.Ledger(path)
    keys = []
    def core(executable, argv, **kwargs):
        keys.append(argv[argv.index('--idempotency-key') + 1])
        return submit_response('task-shared-ledger')
    monkeypatch.setattr(bridge, 'invoke_asterun', core)
    try:
        assert copied.namespace == ledger.namespace
        request = submit_args(tmp_path, request='after-copy')
        bridge.cmd_submit(ledger, request, 10)
        bridge.cmd_submit(copied, request, 10)
        assert keys[0] == keys[1]
    finally:
        copied.close()


@pytest.mark.parametrize('submitted', [False, True])
def test_pre_namespace_v2_ledger_preserves_saved_keys(tmp_path, monkeypatch, submitted):
    path = tmp_path.resolve() / 'previous-v2' / 'ledger.sqlite'
    original = bridge.Ledger(path)
    add_binding(original)
    request = submit_args(tmp_path)
    old_key = 'grokbot_' + 'e' * 64
    original.conn.execute("DELETE FROM meta WHERE key='ledger_namespace'")
    original.conn.execute(
        'INSERT INTO submissions(binding, request_id, input_digest, workspace, backend, '
        'core_idempotency_key, task_id, run_id, state, created_at, updated_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        ('primary', 'request', bridge.input_digest('demo', 'fake', 'input', ''), 'demo', 'fake',
         old_key, 'task-legacy' if submitted else None, 'run-legacy' if submitted else None,
         'submitted' if submitted else 'intended', 'now', 'now'),
    )
    original.close()
    keys = []
    def core(executable, argv, **kwargs):
        keys.append(argv[argv.index('--idempotency-key') + 1])
        return submit_response('task-legacy', 'run-legacy')
    monkeypatch.setattr(bridge, 'invoke_asterun', core)
    migrated = bridge.Ledger(path)
    try:
        assert migrated.namespace
        result = bridge.cmd_submit(migrated, request, 10)
        assert result['task_id'] == 'task-legacy'
        assert keys == ([] if submitted else [old_key])
        row = migrated.conn.execute('SELECT request_id, core_idempotency_key FROM submissions').fetchone()
        assert (row['request_id'], row['core_idempotency_key']) == ('request', old_key)
    finally:
        migrated.close()
