from copy import deepcopy

from asterun.usage.observations import capture, session_observations
from asterun.usage.report import build_usage_report, paginate_usage_report
from asterun.contracts import Run, RunStatus
from asterun.ids import RunId, TaskId, BackendId


def notification(total, sid='native-thread'):
    return {'threadId': sid, 'turnId': 'native-turn', 'tokenUsage': {'total': {
        'inputTokens': total, 'cachedInputTokens': total // 2, 'outputTokens': 0, 'totalTokens': total}}}


def run(id, total, epoch=None):
    observation = capture({}, notification(total))
    observation['scope_epoch'] = epoch
    return Run(RunId(id), TaskId('task'), BackendId('codex'), RunStatus.SUCCEEDED, '2026-09-24',
               native={'token_usage': notification(total), 'usage_observation': observation})


def test_cumulative_duplicates_order_reset_and_epoch_never_attributed_to_runs():
    rows = [run(str(i), total) for i, total in enumerate([100, 150, 150])]
    report = session_observations(rows, [])
    assert len(report) == 1 and report[0]['observed_tokens']['total_tokens'] == 150
    assert not report[0]['aggregation_eligible'] and report[0]['occurred_at'] is None
    assert build_usage_report(rows)['totals']['tokens']['total_tokens']['known_sum'] is None
    reordered = session_observations([rows[1], rows[0]], [])
    assert reordered[0]['observed_tokens']['total_tokens'] == 150
    assert reordered[0]['reset_or_reordering_possible']
    reset = session_observations([*rows, run('reset', 5, 'explicit-new-epoch')], [])
    assert len(reset) == 2
    prior = {}
    for total in [100, 150, 150, 10]: prior = capture(prior, notification(total))
    assert prior['maximum']['totalTokens'] == 150 and prior['last']['totalTokens'] == 10
    assert prior['reset_or_reordering_possible']
    assert prior['native_request_id'] is None


def test_observations_page_without_changing_default_totals_or_cursor():
    runs = [run(str(i), i, str(i)) for i in range(5)]
    base = build_usage_report(runs)
    legacy = paginate_usage_report(base, page_size=1)
    data = {**base, 'observations': session_observations(runs, [])}
    cursor = None; seen = []
    while True:
        page = paginate_usage_report(data, page_size=1, cursor=cursor)
        assert page['totals'] == legacy['totals']
        seen.extend(page['observations'])
        if not page['pagination']['has_more']: break
        cursor = page['pagination']['next_cursor']
    assert len(seen) == 5


def test_report_query_does_not_invent_collection_time_and_identical_retry_costs_stay():
    r = run('first', 100); r.native.pop('usage_observation')
    first = session_observations([r], [])
    assert first == session_observations([r], []) and first[0]['observed_at'] is None
    r.native['usage_observation'] = {}
    assert session_observations([r], []) == first
    r.native = {'usage_source': 'native_headless_report', 'usage': {'input_tokens': 10, 'cache_read_input_tokens': 0,
        'cache_creation_input_tokens': 0, 'output_tokens': 2, 'total_tokens': 12}}
    second = deepcopy(r); second.id = RunId('second'); second.status = RunStatus.FAILED
    assert build_usage_report([r, second])['totals']['tokens']['total_tokens']['known_sum'] == 24


def test_session_observations_multitask_import_and_restart(app, isolated_env):
    from dataclasses import replace
    from asterun.ids import AccountRef, RuntimeRef
    from asterun.sqlite_store import SqliteStore
    submitted = app.handle('task.submit', {'workspace': 'demo', 'text': 'test'})
    original = app.store.get_task(TaskId(submitted.ids['task_id']))
    a = replace(original, id=TaskId('a'), backend=BackendId('codex'), account_ref=AccountRef('account'), runtime_ref=RuntimeRef('runtime'))
    b = replace(a, id=TaskId('b'))
    runs = [run('r1', 100), run('r2', 150)]
    runs[0].task_id = a.id; runs[1].task_id = b.id
    a.external_observations = {'import': {'session_id': 'native-thread', 'last_reported_usage': {'total_tokens': 150}}}
    observed = session_observations(runs, [a,b])
    live = [r for r in observed if r['source'] == 'native_event']
    assert len(live) == 1 and live[0]['task_ids'] == ['a', 'b']
    assert live[0]['observed_tokens']['total_tokens'] == 150
    assert all(not r['aggregation_eligible'] for r in observed)
    unknown = replace(b, account_ref=None)
    assert len([r for r in session_observations(runs,[a,unknown]) if r['source']=='native_event']) == 2
    db = isolated_env / 'usage.sqlite'; store = SqliteStore(db)
    for t in [a,b]: store.save_task(t)
    for r in runs: store.save_run(r)
    store.close(); store = SqliteStore(db)
    try:
        restored = session_observations([store.get_run(r.id) for r in runs], [store.get_task(t.id) for t in [a,b]])
        assert restored == observed
    finally: store.close()


def test_real_adapter_notification_handler_preserves_maximum_and_unknowns():
    from asterun.backends.codex import CodexSession, CodexAppServerTransport
    session = CodexSession(session_id='native-thread', transport=CodexAppServerTransport(), cwd='/tmp/fixture',
                           model=None, started_at='2026-09-24')
    for total in [100, 150, 150, 10]:
        session.handle_message({'method': 'thread/tokenUsage/updated', 'params': notification(total)})
    result = session.to_status_dict()['usage_observation']
    assert result['maximum']['totalTokens'] == 150 and result['last']['totalTokens'] == 10
    assert result['observed_at'] and result['occurred_at'] is None and result['native_request_id'] is None
    assert result['reset_or_reordering_possible'] and result['adapter_version']
