"""版本绑定的派生交接视图，使用既有任务和 blob；不执行恢复或接管。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json

from asterun.contracts import (
    SLOT_HELD_NATIVE_KEY,
    SLOT_RELEASED_NATIVE_KEY,
    TERMINAL_RUN_STATUSES,
    UNAPPLIED_APPROVAL_NATIVE_KEY,
    UNAPPLIED_RESULT_NATIVE_KEY,
)
from asterun.errors import AsterunError
from asterun.quality import digest, snapshot
from asterun.policy import enforce
from asterun.ids import RunId


def state(store, task, run):
    data = task.to_dict()
    data.pop('handoff_snapshot', None)
    current = deepcopy(run.to_dict())
    current['native'].pop('payload_manifests', None)
    current['native'].pop(UNAPPLIED_RESULT_NATIVE_KEY, None)
    current['native'].pop(UNAPPLIED_APPROVAL_NATIVE_KEY, None)
    current['native'].pop(SLOT_RELEASED_NATIVE_KEY, None)
    current['native'].pop(SLOT_HELD_NATIVE_KEY, None)
    approval = store.find_pending_approval(run.id)
    operation = store.find_operation_for_run(run.id)
    return {'operation': {'id': operation.id.value, 'status': operation.status.value} if operation else None,
            'task_sha256': digest(data), 'run_sha256': digest(current),
            'source_event_sequence': store.last_event_sequence(run.id),
            'pending_approval': {'id': approval.id.value, 'sha256': digest(approval.to_dict())} if approval else None}


def commit(store, task, run, expected, expected_revision, content):
    current_task = store.get_task(task.id)
    current_run = store.get_run(run.id)
    if current_task.handoff_snapshot.get('revision', 0) != expected_revision or state(store, current_task, current_run) != expected:
        raise AsterunError('REVISION_CONFLICT', '交接状态已由其他操作更新，不能覆盖；重新读取当前版本')
    sha = store.put_checkpoint_blob(content)
    updated = replace(current_task, handoff_snapshot={'revision': expected_revision + 1, 'sha256': sha})
    store.save_task(updated)
    return updated.handoff_snapshot


def authorized(app, payload):
    task = app._require_task(payload['task_id'])
    for action in ('task.get', 'workspace.read'):
        enforce(app.policy.authorize(app.principal, action, task.workspace))
    app._require_grant(task.workspace)
    app._assert_task_binding(task)
    run = app.store.get_run(RunId(payload['expected_run_id']))
    if run.task_id != task.id or task.current_run_id != run.id:
        raise AsterunError('REVISION_CONFLICT', '请求必须绑定任务当前运行')
    return task, run


def verify(app, task, run):
    from asterun.contracts import AcceptanceStatus
    from asterun.stage_gates import apply_gate, quality_bound, policy_for
    from asterun.dependencies import gate
    from asterun.external_evaluation import validate_external_evaluation
    if task.external_evaluation and not validate_external_evaluation(task, run, app.config.get_workspace(task.workspace).root, app.state_dir):
        raise AsterunError('REVISION_CONFLICT', '外部验收报告已失效')
    if task.acceptance == AcceptanceStatus.PASSED:
        if task.evidence_stale:
            raise AsterunError('REVISION_CONFLICT', '验收证据已经失效')
        if task.quality:
            if app.quality._capture(task)[0] != task.quality['target'] or app.quality._checks(task)[1].acceptance != AcceptanceStatus.PASSED:
                raise AsterunError('REVISION_CONFLICT', '核心验收目标或检查已失效')
        clone = deepcopy(task)
        if policy_for(app, task):
            bound = task.stage_gate.get('bound') or (quality_bound(task, run) if task.quality else None)
            apply_gate(app, clone, run, bound)
        gate(app, clone, run)
        if clone.acceptance != AcceptanceStatus.PASSED:
            raise AsterunError('REVISION_CONFLICT', '阶段证据或上游依赖已失效')


def create(app, payload):
    task, run = authorized(app, payload)
    app._require_applied_config()
    enforce(app.policy.authorize(app.principal, 'workflow.evaluate', task.workspace))
    verify(app, task, run)
    task, run = deepcopy(task), deepcopy(run)
    bound = state(app.store, task, run)
    target, _ = snapshot(app.config.get_workspace(task.workspace).root, payload['target_paths'], state_dir=app.state_dir)
    artifact_refs = []
    for key in ('baseline_sha256', 'latest_sha256'):
        sha = run.native.get('checkpoint', {}).get(key)
        if sha and sha not in artifact_refs:
            app.store.get_checkpoint_blob(sha)
            artifact_refs.append(sha)
    terminal = (run.status in TERMINAL_RUN_STATUSES and not run.cancel_requested
                and bound['operation'] is not None and bound['operation']['status'] in {'completed', 'failed'})
    document = {'schema_version': 1, 'task_id': task.id.value, 'source_run_id': run.id.value,
        'revision': payload['expected_handoff_revision'] + 1, 'config_revision': task.config_revision,
        'original_input_hash': task.input_hash, 'policy_revision': digest(app.config.workflow.to_dict()),
        'authority': bound, 'workspace_binding': target, 'objective_ref': {'task_id': task.id.value, 'field': 'text'},
        'constraints_refs': [{'task_id': task.id.value, 'field': 'text', 'complete_original_required': True}],
        'verified_facts': [{'statement': '执行状态', 'value': run.status.value, 'evidence_source': 'runtime',
                            'evidence_ref': {'run_id': run.id.value, 'field': 'status'}},
                           {'statement': '验收状态', 'value': task.acceptance.value, 'evidence_source':
                            task.external_evaluation.get('source', 'runtime'),
                            'evidence_ref': {'task_id': task.id.value, 'field': 'acceptance'}, 'valid_for_version': task.quality.get('target', {}).get('hash') or task.external_evaluation.get('target_hash'),
                            'acceptance_target_hash': task.quality.get('target', {}).get('hash') or task.external_evaluation.get('target_hash')}],
        'unverified_claims': ['原生内部上下文、真实账户与 Token 净收益没有由此快照验证'],
        'open_issues': [{'id': f.get('id'), 'status': f.get('status', 'open'), 'source': f.get('source_run_id')}
                        for f in task.findings[:64]], 'open_issue_count': len(task.findings),
        'pending_approvals': [bound['pending_approval']['id']] if bound['pending_approval'] else [],
        'unresolved_actions': [] if terminal else [{'run_id': run.id.value, 'status': run.status.value}],
        'artifact_refs': artifact_refs, 'next_actions': ['按当前权限复核完整契约、审批和版本；本接口不会派发'],
        'offline_reconstruction_ready': terminal and not bound['pending_approval'],
        'native_session_created': False, 'scope': 'selected_files_only'}
    document['checksum'] = digest(document)
    content = json.dumps(document, ensure_ascii=False, sort_keys=True).encode()
    if len(content) > 256 * 1024:
        raise AsterunError('INVALID_REQUEST', '交接元数据超限，请缩小范围')
    if snapshot(app.config.get_workspace(task.workspace).root, payload['target_paths'], state_dir=app.state_dir)[0] != target:
        raise AsterunError('REVISION_CONFLICT', '保存交接包期间工作区改变')
    reference = app.store.commit_handoff(task, run, bound, payload['expected_handoff_revision'], content)
    return {**reference, 'checksum': document['checksum'], 'native_session_created': False,
            'offline_reconstruction_ready': document['offline_reconstruction_ready']}


def read(app, payload):
    task, run = authorized(app, payload)
    reference = task.handoff_snapshot
    if reference.get('revision') != payload['expected_handoff_revision']:
        raise AsterunError('REVISION_CONFLICT', '交接 revision 不是当前版本，不回退到旧验收')
    raw = app.store.get_checkpoint_blob(reference['sha256'])
    if len(raw) > 256 * 1024:
        raise AsterunError('INVALID_REQUEST', '交接内容超限')
    try:
        document = json.loads(raw)
        if document['schema_version'] != 1 or document['checksum'] != digest({k:v for k,v in document.items() if k != 'checksum'}):
            raise ValueError()
    except (ValueError, TypeError, KeyError) as exc:
        raise AsterunError('INVALID_REQUEST', '交接 schema 或 checksum 无效') from exc
    if (document['task_id'] != task.id.value or document['source_run_id'] != run.id.value
            or document['policy_revision'] != digest(app.config.workflow.to_dict())
            or document['authority'] != state(app.store, task, run)):
        raise AsterunError('REVISION_CONFLICT', '权威任务、运行、策略、事件或审批已变化；旧交接包失效')
    target, files = snapshot(app.config.get_workspace(task.workspace).root,
                             [f['path'] for f in document['workspace_binding']['files']], state_dir=app.state_dir)
    if target != document['workspace_binding']:
        raise AsterunError('REVISION_CONFLICT', '工作区版本已变化，不能复用旧交接事实')
    verify(app, task, run)
    for sha in document['artifact_refs']:
        app.store.get_checkpoint_blob(sha)
    result = {'document': document, 'sha256': reference['sha256'], 'valid': True, 'native_session_created': False}
    if payload.get('reconstruct', False):
        # 原目标/约束重新从权威 Task 读取，绝不从短摘要猜测；没有上下文驻留假设。
        approval = app.store.find_pending_approval(run.id)
        if bool(approval) != bool(document['pending_approvals']):
            raise AsterunError('REVISION_CONFLICT', '审批状态已改变')
        from asterun.stage_gates import policy_for
        materials = {'acceptance_contract': {'checks': deepcopy(task.quality.get('checks', [])),
                         'external_evaluation': deepcopy(task.external_evaluation),
                         'require_review': app.config.workflow.require_review or task.external_require_review,
                         'review_status': task.review_status, 'review_backend': app.config.workflow.review_backend,
                         'approved_substitute': app.config.workflow.approved_substitute},
                     'findings': deepcopy(task.findings), 'dependencies': list(task.dependencies),
                     'dependency_bindings': deepcopy(run.native.get('dependency_bindings', {})),
                     'original_objective_and_constraints': task.text, 'current_run_prompt': run.prompt,
                     'selected_files': files, 'pending_approval': approval.to_dict() if approval else None,
                     'external_require_review': task.external_require_review, 'stage_policy': policy_for(app, task),
                     'repair_count': task.repair_count, 'max_runs_per_task': app.config.scheduler.max_runs_per_task,
                     'max_repairs': app.config.workflow.max_repairs, 'read_scope': task.read_scope,
                     'not_a_native_resume': True, 'untrusted_source_material': True}
        if len(json.dumps(materials, ensure_ascii=False).encode()) > 512 * 1024:
            raise AsterunError('INVALID_REQUEST', '完整交接材料超过响应限额；缩小选定文件，不能删去用户限制')
        result['materials'] = materials
    if snapshot(app.config.get_workspace(task.workspace).root, [f['path'] for f in target['files']], state_dir=app.state_dir)[0] != target:
        raise AsterunError('REVISION_CONFLICT', '重建期间工作区变化')
    current_task, current_run = authorized(app, payload)
    if current_task.handoff_snapshot != reference or state(app.store, current_task, current_run) != document['authority']:
        raise AsterunError('REVISION_CONFLICT', '读取期间权威状态或交接版本变化')
    return result
