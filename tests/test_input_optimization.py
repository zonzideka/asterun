import hashlib
import json

import pytest

from asterun.application import Application
from asterun.config import load_config
from asterun.errors import AsterunError
from asterun.ids import TaskId, RunId
from asterun.input_optimization import validate, manifest
from asterun.policy import Decision
from tests.conftest import write_config


def setup(env, root, mode='observe', scopes=None, limit=16000):
    cfg = write_config(env / 'config.json', root, extra={'workflow': {'input_optimization': {
        'mode': mode, 'scopes': scopes or ['dispatch_prompt', 'caller_result'], 'max_dispatch_bytes': limit}}})
    return Application(load_config(cfg))


def material(root, path='note.txt'):
    return {'path': path, 'sha256': hashlib.sha256((root / path).read_bytes()).hexdigest(), 'start_line': 1, 'end_line': 1}


def submitted(app, **kw):
    return app.handle('task.submit', {'workspace': 'demo', 'text': '修复解析错误；保持原接口，必须通过已有测试，禁止部署。', **kw})


def sent(app):
    return [c.payload for c in app.backends['fake'].calls if c.method == 'dispatch'][-1]['text']


def bound(result):
    assert result.ok, result.error
    return {'task_id': result.ids['task_id'], 'expected_run_id': result.ids['run_id']}


@pytest.mark.parametrize('policy', [None, [], {'mode': []}, {'mode': 'other'}, {'scopes': ['owned_model_request']},
    {'strict_native_token_cap': True}, {'mode': 'enforce', 'scopes': ['dispatch_prompt']}, {'max_dispatch_bytes': True}])
def test_reject_unsupported_or_ambiguous_policy(policy):
    with pytest.raises(AsterunError): validate(policy)


def test_off_observe_keep_outbound_and_parameters_enforce_only_same_source_material(isolated_env, workspace_root):
    spec = material(workspace_root)
    (workspace_root / 'other.txt').write_bytes((workspace_root / 'note.txt').read_bytes())
    materials = [spec, spec, material(workspace_root, 'other.txt')]
    outputs = {}
    for mode in ('off', 'observe', 'enforce'):
        app = setup(isolated_env, workspace_root, mode)
        try:
            result = submitted(app, dispatch_materials=materials)
            b = bound(result)
            outputs[mode] = sent(app)
            task = app.store.get_task(TaskId(b['task_id']))
            assert outputs[mode].startswith(task.text)
            meta = app.handle('workflow.payload', b).data
            assert not meta['raw_payloads_retained']
            if mode == 'off': assert meta['manifests'] == {}
            else:
                m = meta['manifests']['dispatch_prompt']
                assert m['sent']['sha256'] == hashlib.sha256(outputs[mode].encode()).hexdigest()
                assert m['mandatory_preserved'] and not m['context_residency_assumed']
                assert m['candidate_omissions'] == 1
                assert m['candidate']['serialized_bytes'] < m['baseline']['serialized_bytes']
                assert 'other.txt' in outputs[mode]
        finally: app.close()
    assert outputs['observe'] == outputs['off']
    assert outputs['enforce'].count('材料（低信任') == 2


def test_default_has_no_manifest_and_observe_plain_prompt_identical(app):
    first = submitted(app)
    assert sent(app) == app.store.get_task(TaskId(first.ids['task_id'])).text
    assert app.handle('workflow.payload', bound(first)).data['manifests'] == {}


@pytest.mark.parametrize('failure', ['budget', 'version', 'path', 'symlink', 'permission', 'storage'])
def test_fail_closed_before_dispatch(isolated_env, workspace_root, monkeypatch, failure):
    app = setup(isolated_env, workspace_root, 'enforce', limit=1 if failure == 'budget' else 16000)
    spec = material(workspace_root)
    if failure == 'version': spec['sha256'] = '0' * 64
    if failure == 'path': spec['path'] = '../outside.txt'
    if failure == 'symlink':
        (workspace_root / 'link.txt').symlink_to(workspace_root / 'note.txt'); spec['path'] = 'link.txt'
    if failure == 'permission':
        original = app.policy.authorize
        monkeypatch.setattr(app.policy, 'authorize', lambda p,a,w: Decision(False, 'denied') if a == 'workspace.read' else original(p,a,w))
    if failure == 'storage':
        original = app.store.save_run
        def fail(run):
            if run.native.get('payload_manifests'): raise OSError('disk full')
            original(run)
        monkeypatch.setattr(app.store, 'save_run', fail)
    try:
        response = submitted(app, dispatch_materials=[spec])
        assert not response.ok, response.data
        assert app.backends['fake'].dispatch_count == 0
    finally: app.close()


def test_materials_reread_even_if_previously_seen(isolated_env, workspace_root):
    app = setup(isolated_env, workspace_root, 'enforce')
    try:
        spec = material(workspace_root)
        first = submitted(app, dispatch_materials=[spec]); assert first.ok
        original = sent(app)
        second = submitted(app, dispatch_materials=[spec]); assert second.ok
        assert sent(app) == original and '非 Git 输入' in sent(app)
        (workspace_root / 'note.txt').write_text('新版本\n')
        assert not submitted(app, dispatch_materials=[spec]).ok
        assert app.backends['fake'].dispatch_count == 2
    finally: app.close()


def test_caller_observe_no_recursive_metadata_then_enforce_compact(isolated_env, workspace_root):
    from dataclasses import replace
    app = setup(isolated_env, workspace_root)
    try:
        b = bound(submitted(app))
        run = app.store.get_run(RunId(b['expected_run_id']))
        run.summary = '\n'.join(f'src/parser.py:{n}: field case_{n} failed validation' for n in range(150))
        app.store.save_run(run)
        first = app.handle('task.get', {'task_id': b['task_id']}).data
        second = app.handle('task.get', {'task_id': b['task_id']}).data
        assert first == second
        meta = app.handle('workflow.payload', b).data['manifests']['caller_result']
        assert meta['sent'] == manifest(first, 'caller_result')
        assert meta['candidate']['serialized_bytes'] < meta['baseline']['serialized_bytes']
        app.config = replace(app.config, workflow=replace(app.config.workflow, input_optimization={
            'mode': 'enforce', 'scopes': ['caller_result']}))
        compact = app.handle('task.get', {'task_id': b['task_id']})
        assert compact.ok and len(json.dumps(compact.data)) < len(json.dumps(first))
        assert app.backends['fake'].dispatch_count == 1
    finally: app.close()


def test_existing_log_range_read_is_bounded_version_bound_and_no_new_run(isolated_env, workspace_root):
    from tests.test_local_checks import setup as setup_check, check
    source = "for i in range(350): print(f'src/parser.py:{i}: failure case_{i}')\nraise SystemExit(2)"
    app, b = setup_check(isolated_env, workspace_root, source)
    try:
        result = check(app, b)
        run = app.store.get_run(RunId(b['expected_run_id']))
        job = next(iter(run.native['local_checks']))
        payload = {k:b[k] for k in ('task_id', 'expected_run_id')}
        payload.update(job_id=job, limit=600)
        text = ''; offset = 0
        while True:
            response = app.handle('workflow.check-detail', {**payload, 'offset': offset})
            assert response.ok, response.error
            page = response.data
            assert page['returned_chars'] <= 600 and not page['full_log_retained']
            text += page['content']; offset = page['next_offset']
            if not page['has_more']: break
        assert text == result['diagnostic'] and result['output_truncated']
        assert result['output_bytes'] > result['retained_diagnostic_bytes']
        assert len(text) <= 3000 and app.backends['fake'].dispatch_count == 1
        (workspace_root / 'note.txt').write_text('changed')
        assert not app.handle('workflow.check-detail', payload).ok
    finally: app.close()


def test_cli_mcp_new_routes_schema_and_usage_flag(app, isolated_env):
    from asterun.cli import build_parser, _load_request, COMMAND_METHODS
    from asterun.mcp_server import handle_rpc
    b = bound(submitted(app))
    requests = {'workflow.payload': b, 'workflow.handoff-create': {**b, 'target_paths': ['note.txt'], 'expected_handoff_revision': 0},
                'workflow.handoff-read': {**b, 'expected_handoff_revision': 1, 'reconstruct': True}}
    for method, body in requests.items():
        path = isolated_env / 'request.json'; path.write_text(json.dumps(body))
        name = method.replace('.', '-', 1)
        args = build_parser().parse_args([name, '--request', str(path)])
        assert _load_request(args) == body and COMMAND_METHODS[name] == method
        rpc = handle_rpc(app, {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': method.replace('.', '_').replace('-', '_'), 'arguments': body}})
        assert not rpc.get('error'), rpc
        assert not rpc['result'].get('isError'), rpc
    args = build_parser().parse_args(['usage-report', '--task-id', b['task_id'], '--include-observations'])
    assert _load_request(args)['include_observations']


def test_redacted_summary_is_derived_and_does_not_replace_authorized_evidence(isolated_env, workspace_root):
    from tests.test_local_checks import setup as setup_check, check
    secret = 'sk-fixture_not_a_real_credential'
    app, b = setup_check(isolated_env, workspace_root, f"print('src/auth.py:12 TypeError {secret}')\nraise SystemExit(2)")
    try:
        result = check(app, b)
        run = app.store.get_run(RunId(b['expected_run_id']))
        job = next(iter(run.native['local_checks']))
        response = app.handle('workflow.check-detail', {'task_id': b['task_id'], 'expected_run_id': b['expected_run_id'], 'job_id': job})
        assert response.ok, response.error
        data = response.data
        assert secret not in json.dumps(data['summary']) and '[redacted]' in json.dumps(data['summary'])
        assert data['content'] == result['diagnostic'] and secret in data['content']
        assert data['sha256_of_retained_diagnostic'] == hashlib.sha256(data['content'].encode()).hexdigest()
        assert data['summary_is_redacted_derivative'] and data['exit_code'] == 2
        assert app.store.get_run(run.id).native['local_checks'][job]['result']['diagnostic'] == result['diagnostic']
    finally: app.close()


@pytest.mark.parametrize('kind', ['fake', 'grok', 'codex', 'claude'])
def test_observe_plain_boundary_preserves_exact_text_with_known_adapters(app, kind):
    from dataclasses import replace
    from asterun.input_optimization import dispatch, capabilities
    first = submitted(app); b = bound(first)
    task = app.store.get_task(TaskId(b['task_id'])); run = app.store.get_run(RunId(b['expected_run_id']))
    app.config.backends['fake'] = replace(app.config.backends['fake'], kind=kind)
    app.config.workflow.input_optimization.update(mode='observe', scopes=['dispatch_prompt'])
    text = dispatch(app, task, run)
    assert text.encode() == task.text.encode()
    caps = capabilities(app.config.backends['fake'])
    assert caps['adapter_revision'] and caps['native_client_version'] is None
    assert caps['capabilities']['per_model_request_rewrite'] == 'unsupported'
    assert caps['capabilities']['per_model_request_visibility'] == 'unknown'
    assert app.backends['fake'].dispatch_count == 1


def test_unverified_plugin_and_fixed_scope_enforce_do_not_fallback(app):
    from dataclasses import replace
    from asterun.input_optimization import dispatch
    first = submitted(app); b = bound(first)
    task = app.store.get_task(TaskId(b['task_id'])); run = app.store.get_run(RunId(b['expected_run_id']))
    app.config.workflow.input_optimization.update(mode='enforce', scopes=['dispatch_prompt'], max_dispatch_bytes=16000)
    original = app.config.backends['fake']
    app.config.backends['fake'] = replace(original, kind='unknown-plugin')
    with pytest.raises(AsterunError, match='未验证'): dispatch(app, task, run)
    app.config.backends['fake'] = original
    for updates in ({'capability': 'typed'}, {'read_scope': {'manifest_path':'fixed'}}):
        with pytest.raises(AsterunError, match='固定读取'): dispatch(app, replace(task, **updates), run)
    with pytest.raises(AsterunError, match='独立审查'): dispatch(app, task, replace(run, role='review'))
    assert app.backends['fake'].dispatch_count == 1
