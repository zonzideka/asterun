from copy import deepcopy
import json

from tests.test_local_checks import setup, check


def test_unit_success_does_not_claim_wiring_review_integration_or_deployment(isolated_env, workspace_root):
    app, bound = setup(isolated_env, workspace_root, 'print("ok")')
    try:
        check(app, bound)
        response = app.handle('workflow.evidence', bound)
        assert response.ok, response.error
        stages = response.data['stages']
        assert stages['coding']['status'] == 'passed'
        assert all(stages[key]['status'] == 'unverified' for key in ('consumer','independent_review','integration','deployment'))
        assert not response.data['all_stages_verified']
        task = app._require_task(bound['task_id'])
        task.review_status = 'passed'; task.external_evaluation = {'source':'external_reported'}
        app.store.save_task(task)
        assert app.handle('workflow.evidence', bound).data['stages']['independent_review']['status'] == 'unverified'
        (workspace_root / 'note.txt').write_text('changed')
        assert app.handle('workflow.evidence', bound).error['code'] == 'REVISION_CONFLICT'
    finally:
        app.close()


def test_stage_load_failures_and_compact_diagnostics(isolated_env, workspace_root):
    source = '''import json,pathlib
pathlib.Path('counts.json').write_text(json.dumps(dict(passed=5,failed=0,skipped=0,load_errors=12)))
print('src/main.ts:4 TS2322 type mismatch')
print('src/main.ts:5 TS2322 type mismatch')
'''
    app, bound = setup(isolated_env, workspace_root, source, result_file=True)
    app.config.workflow.local_checks['compile']['stage'] = 'consumer'
    try:
        check(app, bound)
        value = app.handle('workflow.evidence', bound).data
        assert value['stages']['coding']['status'] == 'unverified'
        assert value['stages']['consumer']['status'] == 'failed'
        diagnostics = value['stages']['consumer']['checks'][0]['diagnostics']
        assert diagnostics[0]['path'] == 'src/main.ts' and diagnostics[0]['category'] == 'TS2322' and diagnostics[0]['count'] == 2
        app.config.workflow.local_checks['compile']['timeout_seconds'] = 6
        value = app.handle('workflow.evidence', bound).data
        assert value['stages']['consumer']['status'] == 'unverified'
        assert value['stages']['consumer']['checks'][0]['status'] == 'stale'
    finally:
        app.close()


def test_evidence_cli_mcp_contract(isolated_env, workspace_root):
    from asterun.cli import build_parser, _load_request
    from asterun.mcp_server import handle_rpc
    app, bound = setup(isolated_env, workspace_root, 'print("ok")')
    try:
        path = isolated_env / 'bound.json'; path.write_text(json.dumps(bound))
        args = build_parser().parse_args(['workflow-evidence',bound['task_id'],'--request',str(path)])
        payload = _load_request(args)
        rpc = handle_rpc(app, {'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'workflow_evidence','arguments':payload}})
        assert not rpc.get('error')
    finally:
        app.close()
