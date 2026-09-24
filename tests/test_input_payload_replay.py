"""固定合成工作流样本，只比较可见载荷；不模拟真实模型行为或净收益。"""
import hashlib
import json

import pytest

from asterun.ids import RunId, TaskId
from asterun.input_optimization import manifest
from tests.test_input_optimization import setup, submitted, bound, material

# 先固定样本、必需契约和输出，再测量；不按结果筛掉没有收益的样本。
CASES = [
    ('short', '修复空输入解析，保持公共返回格式。', 'empty input regression passed', []),
    ('long_diagnostic', '定位泛型类型错误，保留每个错误的位置供复核。', '\n'.join(
        f'src/{module}.ts:{line}: TS2322 {actual} is not assignable to {expected}; caller must narrow the optional field before serialization'
        for module, actual, expected in [('parser','undefined','string'),('client','null','Response'),('schema','number','Literal'),('adapter','unknown','Config')]
        for line in (12, 27, 43, 68)), []),
    ('large_log', '检查构建和测试失败；保留错误退出码及完整证据引用。', '\n'.join(
        f'tests/{module}/test_{case}.py::{scenario} PASSED'
        for module in ('parser','protocol','runtime','storage','permissions')
        for case in range(20) for scenario in ('empty_input','invalid_schema','restart_replay')), []),
    ('cross_file', '同时修改解析器与调用者，保留类型和异常契约。', 'parser and caller regression passed', ['parser.py', 'client.py', 'parser.py']),
    ('recovery', '恢复排查前重新读取源码；不得重派未决动作，审查要求不变。', 'run unknown; reconcile required before any dispatch', ['parser.py']),
    ('failed_evidence', '失败验收：不得删除报告字段，保留独立审查与原始失败证据。',
        'FAILED tests/test_protocol.py::test_invalid_schema\nexpected field: native_session_id\nactual: missing\nexit=1; acceptance=failed', ['parser.py', 'client.py']),
]
FILES = {'parser.py': 'def parse(value):\n    if not value:\n        raise ValueError("empty input")\n    return {"value": value}\n',
         'client.py': 'from parser import parse\n\ndef request(value):\n    return parse(value)\n'}


@pytest.mark.parametrize('name,objective,output,paths', CASES, ids=[row[0] for row in CASES])
def test_fixed_payload_replay(isolated_env, workspace_root, name, objective, output, paths):
    for path, content in FILES.items(): (workspace_root / path).write_text(content)
    specs = [{**material(workspace_root, path), 'end_line': len(FILES[path].splitlines())} for path in paths]
    app = setup(isolated_env, workspace_root)
    try:
        result = submitted(app, text=objective, dispatch_materials=specs)
        b = bound(result); run = app.store.get_run(RunId(b['expected_run_id']))
        run.summary = output; app.store.save_run(run)
        response = app.handle('task.get', {'task_id': b['task_id']}); assert response.ok
        manifests = app.handle('workflow.payload', b).data['manifests']
        assert app.store.get_task(TaskId(b['task_id'])).text == objective
        assert app.backends['fake'].dispatch_count == 1
        dispatch = manifests['dispatch_prompt']; caller = manifests['caller_result']
        assert dispatch['sent'] == dispatch['baseline'] and caller['sent'] == caller['baseline']
        assert caller['sent'] == manifest(response.data, 'caller_result')
        if name == 'cross_file': assert dispatch['candidate']['serialized_bytes'] < dispatch['baseline']['serialized_bytes']
        else: assert dispatch['candidate'] == dispatch['baseline']
        row = {'sample': name, 'fixture_sha256': hashlib.sha256(json.dumps([objective,output,paths,FILES], sort_keys=True).encode()).hexdigest(),
               'dispatch_bytes': [dispatch[k]['serialized_bytes'] for k in ('baseline','candidate')],
               'caller_bytes': [caller[k]['serialized_bytes'] for k in ('baseline','candidate')],
               'payload_estimate': 'measured', 'native_usage': 'not_measured', 'end_to_end_quality': 'not_measured', 'subscription_quota_effect': 'unknown'}
        print('PAYLOAD_SAMPLE ' + json.dumps(row, sort_keys=True))
    finally: app.close()
