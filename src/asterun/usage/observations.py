"""累计原生计数的未归属观察；绝不把首个快照分配给当前运行。"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

FIELDS = ('inputTokens', 'cachedInputTokens', 'cacheWriteInputTokens', 'outputTokens',
          'reasoningOutputTokens', 'totalTokens')


def capture(previous, notification):
    if not isinstance(notification, dict):
        return previous
    usage = notification.get('tokenUsage', {})
    values = usage.get('total') if isinstance(usage, dict) else None
    sid = notification.get('threadId')
    if not isinstance(values, dict) or not isinstance(sid, str) or not 1 <= len(sid) <= 512:
        return previous
    values = {k: v for k, v in values.items() if k in FIELDS and type(v) is int and 0 <= v < 2**63}
    if not values:
        return previous
    same = previous and previous.get('native_session_id') == sid
    prior = deepcopy(previous) if same else {}
    maximum = prior.get('maximum', {})
    lower = any(k in maximum and v < maximum[k] for k, v in values.items())
    turn = notification.get('turnId')
    from asterun import __version__
    return {'adapter_version': __version__, 'observation_schema_version': 1, 'source': 'native_event', 'native_session_id': sid, 'native_turn_id': turn if isinstance(turn, str) and len(turn) <= 512 else None,
            'native_request_id': None, 'native_event_id': None, 'source_sequence': None, 'scope_epoch': None,
            'occurred_at': None, 'observed_at': datetime.now(timezone.utc).isoformat(),
            'counter_mode': 'cumulative', 'granularity': 'session', 'native_version': None,
            'last': values, 'maximum': {k: max(v, maximum.get(k, v)) for k, v in {**maximum, **values}.items()},
            'reset_or_reordering_possible': prior.get('reset_or_reordering_possible', False) or lower,
            'coverage': 'notifications_seen_by_adapter', 'run_attribution': 'unavailable'}


def session_observations(runs, tasks):
    from asterun.usage.report import _row, _tokens
    from asterun.quality import digest
    tasks = {t.id.value: t for t in tasks}
    groups = {}
    rows = []
    for run in runs:
        task = tasks.get(run.task_id.value)
        observation = run.native.get('usage_observation') or capture({}, run.native.get('token_usage'))
        if not observation:
            continue
        # 旧记录没有可信采集时刻；不得用本次查询时间伪造历史发生时间。
        observation = deepcopy(observation)
        if not run.native.get('usage_observation'):
            observation['observed_at'] = None
            observation['adapter_version'] = None
        dimensions = _row(run, task)['dimensions']
        connection = [task.workspace, task.workspace_root] if task else None
        # 身份不完整时不能跨任务合并同名原生会话。
        if not dimensions.get('account_ref') or not dimensions.get('runtime_ref'):
            connection = [connection, run.task_id.value]
        key = digest([dimensions, connection, observation['native_session_id'], observation.get('scope_epoch'),
                      'codex_app_server_cumulative'])
        group = groups.setdefault(key, {'scope_id': key, 'source': 'native_event', 'dimensions': dimensions,
            'native_session_id': observation['native_session_id'],
            'adapter_version': observation.get('adapter_version'), 'native_version': observation.get('native_version'),
            'connection_scope_id': digest(connection), 'scope_epoch': observation.get('scope_epoch'),
            'granularity': 'session', 'counter_mode': 'cumulative', 'aggregation_eligible': False,
            'run_ids': [], 'task_ids': [], 'maximum': {}, 'reset_or_reordering_possible': False,
            'occurred_at': None, 'observed_at': None, 'daily_bucket': 'unknown_occurrence',
            'input_includes_cached': True, 'output_includes_reasoning': True,
            'run_attribution': 'unavailable', 'quota_units': None, 'actual_charge_usd': None})
        group['reset_or_reordering_possible'] |= any(k in group['maximum'] and v < group['maximum'][k]
                                                     for k, v in observation.get('maximum', {}).items())
        for key2, value in observation.get('maximum', {}).items():
            group['maximum'][key2] = max(value, group['maximum'].get(key2, value))
        group['reset_or_reordering_possible'] |= observation.get('reset_or_reordering_possible', False)
        group['run_ids'].append(run.id.value)
        if run.task_id.value not in group['task_ids']:
            group['task_ids'].append(run.task_id.value)
        if observation.get('observed_at'):
            group['observed_at'] = max(group['observed_at'] or '', observation['observed_at'])
    for group in groups.values():
        issues, sources = [], {}
        group['observed_tokens'] = _tokens(group.pop('maximum'), convention='codex_app_server', issues=issues, sources=sources)
        group['quality'] = 'inconsistent' if issues else 'incomplete'
        group['issues'] = issues + ['historical_baseline_unknown', 'epoch_and_native_order_unverified', 'not_added_to_run_totals']
        group['missing_fields'] = [k for k, v in group['observed_tokens'].items() if v is None]
        rows.append(group)
    for task in tasks.values():
        for ref, value in task.external_observations.items():
            rows.append({'scope_id': ref, 'task_id': task.id.value, 'source': 'imported_log',
                'native_session_id': value['session_id'], 'granularity': 'unknown', 'counter_mode': 'unknown',
                'occurred_at': None, 'observed_at': value.get('observed_at'), 'daily_bucket': 'unknown_occurrence',
                'aggregation_eligible': False, 'quality': 'incomplete', 'run_attribution': 'unavailable',
                'raw_reported_counters': deepcopy(value.get('last_reported_usage')),
                'matched_run_ids': [r.id.value for r in runs if r.native.get('session_id', r.native.get('backend_session_id')) == value['session_id']
                                    and r.task_id == task.id],
                'issues': ['import_not_new_execution', 'counter_scope_unknown', 'not_added_to_run_totals']})
    return rows
