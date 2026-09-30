"""只读导入直调日志索引；不创建受管会话、不把历史记录算成模型运行。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
from urllib.parse import quote

from asterun.contracts import TERMINAL_RUN_STATUSES
from asterun.errors import AsterunError
from asterun.external_evaluation import _unique_pairs
from asterun.policy import enforce
from asterun.quality import _open_file, digest

MAX_LOG_BYTES = 16 * 1024 * 1024
COUNTERS = {'inputTokens', 'outputTokens', 'cachedInputTokens', 'totalTokens', 'reasoningTokens',
            'cacheReadInputTokens', 'cacheWriteInputTokens', 'input_tokens', 'output_tokens',
            'cache_read_input_tokens', 'cache_creation_input_tokens', 'total_tokens', 'reasoning_tokens'}


def _read(path, expected):
    # 从 / 逐级使用 dirfd/NOFOLLOW，拒绝父目录与文件符号链接；不读取认证文件。
    try:
        fd = _open_file(Path('/'), path.relative_to('/'))
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_LOG_BYTES:
                raise ValueError()
            raw = stream.read(MAX_LOG_BYTES + 1)
            after = os.fstat(stream.fileno())
        if (len(raw) > MAX_LOG_BYTES or (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                or hashlib.sha256(raw).hexdigest() != expected):
            raise ValueError()
        return raw
    except (OSError, ValueError) as exc:
        raise AsterunError('REVISION_CONFLICT', '原生日志缺失、非普通文件、超过 16 MiB、路径不安全或内容与指定摘要不符') from exc


def observe(app, payload):
    task = app._require_task(payload['task_id'])
    for action in ('task.get', 'workspace.read'):
        enforce(app.policy.authorize(app.principal, action, task.workspace))
    app._require_grant(task.workspace)
    app._require_applied_config()
    app._assert_task_binding(task)
    run = app.store.get_run(task.current_run_id) if task.current_run_id else None
    if run is None or run.status not in TERMINAL_RUN_STATUSES:
        raise AsterunError('INVALID_REQUEST', '只向已确认终态的任务附加历史观察，不干预活动或未决运行')
    backend = app.config.backends.get(payload['backend'])
    if backend is None or backend.kind != 'grok' or not backend.home:
        raise AsterunError('CAPABILITY_UNSUPPORTED', '只读取已配置 Grok 后端的显式 home')
    home = Path(backend.home).expanduser()
    if not home.is_absolute() or '..' in home.parts:
        raise AsterunError('INVALID_REQUEST', '原生 home 必须为绝对规范路径')
    workspace = app.config.get_workspace(task.workspace).root.resolve()
    sid = payload['session_id']
    if app.store.find_session_by_native(payload['backend'], sid):
        raise AsterunError('BINDING_MISMATCH', '已有受管会话不能再作为直调记录导入')
    root = home / 'sessions' / quote(str(workspace), safe='') / sid
    paths = {name: root / name for name in ('summary.json', 'updates.jsonl')}
    hashes = {'summary.json': payload['summary_sha256'], 'updates.jsonl': payload['updates_sha256']}
    raws = {name: _read(path, hashes[name]) for name, path in paths.items()}
    usage = None
    turns = 0
    try:
        summary = json.loads(raws['summary.json'], object_pairs_hook=_unique_pairs)
        if (not isinstance(summary, dict) or not isinstance(summary.get('info'), dict)
                or summary['info'].get('id') != sid or summary['info'].get('cwd') != str(workspace)):
            raise ValueError()
        updates = raws['updates.jsonl']
        if not updates or not updates.endswith(b'\n'):
            raise ValueError()
        for line in updates.splitlines():
            if len(line) > 256 * 1024:
                raise ValueError()
            row = json.loads(line, object_pairs_hook=_unique_pairs)
            if not isinstance(row, dict) or not isinstance(row.get('params'), dict) or row['params'].get('sessionId') != sid:
                raise ValueError()
            update = row['params'].get('update', {})
            if not isinstance(update, dict):
                raise ValueError()
            if row.get('method') == 'session/update' and update.get('sessionUpdate') == 'turn_completed':
                turns += 1
                raw_usage = update.get('usage')
                if isinstance(raw_usage, dict):
                    usage = {key: value for key, value in raw_usage.items()
                             if key in COUNTERS and type(value) is int and 0 <= value < 2**63}
    except (ValueError, TypeError, RecursionError) as exc:
        raise AsterunError('INVALID_REQUEST', '直调日志格式、会话或工作区绑定不明；未导入') from exc
    reference = digest({'home': str(home), 'workspace': str(workspace), 'session_id': sid})
    observations = deepcopy(task.external_observations)
    old = observations.get(reference)
    if old and old['log_hashes'] != hashes:
        raise AsterunError('REVISION_CONFLICT', '已导入日志发生漂移，保留旧观察，不覆盖或接管')
    for other_id in app.store.list_task_ids():
        if other_id != task.id and reference in app.store.get_task(other_id).external_observations:
            raise AsterunError('IDEMPOTENCY_CONFLICT', '该直调会话已附加到另一任务，不重复导入')
    if not old and len(observations) >= 16:
        raise AsterunError('RATE_LIMITED', '单任务最多附加 16 个历史直调会话')
    # 处理后再按请求哈希重读，变化的日志不能写成稳定历史快照。
    for name, path in paths.items():
        _read(path, hashes[name])
    record = {'source': 'external_observed', 'observed_at': datetime.now(timezone.utc).isoformat(), 'occurred_at': None, 'session_id': sid, 'backend': payload['backend'],
              'log_hashes': hashes, 'completed_turn_events': turns, 'last_reported_usage': usage,
              'usage_scope': 'unknown', 'usage_included_in_totals': False, 'ownership_verified': False,
              'resume_supported': False, 'acceptance_unchanged': True, 'model_dispatched': False}
    if not old:
        observations[reference] = record
        app.store.save_task(replace(task, external_observations=observations))
    return {**(old or record), 'reference': reference, 'reused': bool(old)}
