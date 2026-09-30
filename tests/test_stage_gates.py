import hashlib
import json
from copy import deepcopy

import pytest

from asterun.application import Application
from asterun.config import load_config
from asterun.contracts import Task
from asterun.ids import TaskId
from asterun.sqlite_store import SqliteStore
from asterun.stage_gates import validate_policy
from tests.test_local_checks import setup, check
from tests.test_quality_runtime import make_app, start


def evaluate(app, bound):
    value = app.handle('workflow.evaluate', {**bound, 'checks':[{'kind':'file_exists','path':'note.txt'}]})
    assert value.ok,value.error
    return value.data


def receipt(app, bound, root, stage='deployment', passed=True, counts=None, key='release-1'):
    report = {k:bound['expected_'+k] for k in ('run_id','input_hash','target_hash','revision')}
    report.update(task_id=bound['task_id'], stage=stage, environment='test-host/staging',
                  artifact_sha256='a'*64, release_id='release-one',
                  checks=[{'name':'product-probe','passed':passed,'detail':'synthetic offline receipt'}])
    if counts is not None:report['checks'][0]['counts']=counts
    raw=json.dumps(report).encode();path=root/(key+'.json');path.write_bytes(raw)
    payload={**bound,'stage':stage,'report':{'path':path.name,'sha256':hashlib.sha256(raw).hexdigest()},'idempotency_key':key}
    return payload


def test_gate_missing_consumer_cannot_be_bypassed_by_omitting_checks_or_config(isolated_env,workspace_root):
    app,bound=setup(isolated_env,workspace_root,'print("ok")')
    app.config.workflow.stage_gates={'coding':'core','consumer':'core'}
    app.config.workflow.local_checks['wiring']={**deepcopy(app.config.workflow.local_checks['compile']),'stage':'consumer'}
    try:
        check(app,bound)
        result=evaluate(app,bound)
        assert result['acceptance']=='pending'
        assert not result['task']['stage_gate']['satisfied']
        app.config.workflow.stage_gates={}
        app.config.workflow.local_checks.pop('wiring')
        assert evaluate(app,bound)['acceptance']=='pending'
        assert not app.handle('workflow.evaluate',{'task_id':bound['task_id']}).ok
        wire={**deepcopy(app.config.workflow.local_checks['compile']),'stage':'consumer'}
        app.config.workflow.local_checks['wiring']=wire
        assert app.handle('workflow.check',{**bound,'check_name':'wiring'}).ok
        result=evaluate(app,bound)
        assert result['acceptance']=='passed' and result['task']['stage_gate']['satisfied']
        task=Task.from_dict(result['task'])
        assert task.stage_gate['policy']['consumer']['checks']==['wiring']
        app.config.workflow.local_checks.pop('wiring')
        assert app.handle('task.get',{'task_id':bound['task_id']}).data['task']['acceptance']=='pending'
    finally:app.close()


def test_deployment_receipt_is_explicit_external_evidence_and_tamper_invalidates(isolated_env,workspace_root):
    app,bound=setup(isolated_env,workspace_root,'print("ok")')
    app.config.workflow.stage_gates={'deployment':'external_reported'}
    try:
        assert evaluate(app,bound)['acceptance']=='pending'
        payload=receipt(app,bound,workspace_root)
        imported=app.handle('workflow.attest',payload)
        assert imported.ok,imported.error
        assert imported.data['source']=='external_reported' and not imported.data['deployment_executed']
        assert app.handle('workflow.attest',payload).data['reused']
        assert evaluate(app,bound)['acceptance']=='passed'
        evidence=app.handle('workflow.evidence',bound).data
        assert evidence['stages']['deployment']['source']=='external_reported'
        assert not evidence['all_stages_verified']
        (workspace_root/payload['report']['path']).write_text('changed')
        task=app.handle('task.get',{'task_id':bound['task_id'],'compact':True}).data['task']
        assert task['acceptance']=='pending' and not task['stage_gate']['satisfied']
    finally:app.close()


def test_external_cannot_satisfy_core_gate_and_load_errors_fail(isolated_env,workspace_root):
    app,bound=setup(isolated_env,workspace_root,'print("ok")')
    app.config.workflow.stage_gates={'consumer':'core'}
    try:
        payload=receipt(app,bound,workspace_root,stage='consumer')
        assert app.handle('workflow.attest',payload).ok
        assert evaluate(app,bound)['acceptance']=='pending'
        # 已持久核心要求不能被后续低可信策略降级。
        app.config.workflow.stage_gates={'consumer':'external_reported'}
        assert evaluate(app,bound)['acceptance']=='pending'
        payload=receipt(app,bound,workspace_root,stage='deployment',counts={'passed':5,'failed':0,'skipped':0,'load_errors':12},key='bad-release')
        assert app.handle('workflow.attest',payload).data['status']=='failed'
        app.config.workflow.stage_gates['deployment']='external_reported'
        assert evaluate(app,bound)['acceptance']=='failed'
    finally:app.close()


def test_quality_waits_for_stage_without_duplicate_review_then_finishes(config_path,workspace_root):
    app,reviewer,_=make_app(config_path,workspace_root)
    app.config.workflow.stage_gates={'deployment':'external_reported'}
    try:
        submitted,_=start(app)
        for _ in range(5):app.poll()
        task=app._require_task(submitted.ids['task_id'])
        assert task.quality['phase']=='awaiting_stages' and task.acceptance.value=='pending'
        assert len(reviewer.contexts)==1
        snap=app.handle('workflow.snapshot',{'task_id':task.id.value,'target_paths':['note.txt']}).data
        bound={'task_id':task.id.value,'target_paths':snap['target_paths'],**{'expected_'+k:snap[k] for k in ('run_id','revision','input_hash','target_hash')}}
        payload=receipt(app,bound,workspace_root)
        assert app.handle('workflow.attest',payload).ok
        app.poll()
        task=app._require_task(task.id.value)
        assert task.quality['phase']=='completed' and task.acceptance.value=='passed'
        assert len(reviewer.contexts)==1
        (workspace_root/'release-1.json').unlink()
        assert app.handle('task.get',{'task_id':task.id.value}).data['task']['acceptance']=='pending'
        payload = receipt(app, bound, workspace_root)
        assert app.handle('workflow.attest', payload).ok
        assert app.handle('workflow.evaluate', {'task_id': task.id.value}).data['acceptance'] == 'passed'
        assert len(reviewer.contexts) == 1
    finally:app.close()


def test_simulated_review_cannot_satisfy_independent_stage(config_path,workspace_root):
    app,reviewer,_=make_app(config_path,workspace_root)
    app.config.workflow.stage_gates={'independent_review':'core'}
    try:
        submitted,_=start(app)
        for _ in range(6):app.poll()
        task=app._require_task(submitted.ids['task_id'])
        assert task.quality['phase']=='awaiting_stages'
        assert task.stage_gate['states']['independent_review']['status']=='simulated'
        assert len(reviewer.contexts)==1 and task.acceptance.value=='pending'
    finally:app.close()


def test_receipt_binding_permissions_cli_mcp_and_reload(isolated_env,workspace_root):
    from asterun.cli import build_parser,_load_request
    from asterun.mcp_server import handle_rpc
    from asterun.policy import Decision
    app,bound=setup(isolated_env,workspace_root,'print("ok")')
    app.config.workflow.stage_gates={'deployment':'external_reported'}
    try:
        payload=receipt(app,bound,workspace_root)
        request=isolated_env/'request.json';request.write_text(json.dumps(payload))
        args=build_parser().parse_args(['workflow-attest',bound['task_id'],'--request',str(request)])
        rpc=handle_rpc(app,{'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'workflow_attest','arguments':_load_request(args)}})
        assert not rpc.get('error')
        assert evaluate(app,bound)['acceptance']=='passed'
        store=SqliteStore(isolated_env/'state.sqlite')
        store.bootstrap_applied_revision(app.config.revision)
        task=app._require_task(bound['task_id']);run=app.store.get_run(task.current_run_id)
        store.save_session(app.store.get_session(task.conversation_id));store.save_task(task);store.save_run(run)
        store.save_operation(app.store.find_operation_for_run(run.id));store.close()
        restored=Application(app.config,store=SqliteStore(isolated_env/'state.sqlite'))
        try:
            assert restored.handle('task.get',{'task_id':task.id.value}).data['task']['acceptance']=='passed'
            assert restored.handle('workflow.attest',payload).data['reused']
        finally:restored.close()
        altered={**payload,'stage':'independent_review'}
        assert not app.handle('workflow.attest',altered).ok
        (workspace_root/'note.txt').write_text('drift')
        assert not app.handle('workflow.attest',payload).ok
        class Deny:
            def authorize(self,*args):return Decision(False,'SCOPE_DENIED')
        app.policy=Deny()
        assert app.handle('workflow.attest',payload).error['code']=='SCOPE_DENIED'
    finally:app.close()


@pytest.mark.parametrize('policy',[[],{'unknown':'core'},{'coding':'external_reported'},{'independent_review':'external_reported'},{'deployment':'core'},{'consumer':True}])
def test_invalid_stage_policy(policy):
    with pytest.raises(Exception):validate_policy(policy)


def test_external_success_does_not_hide_failed_local_consumer(isolated_env, workspace_root):
    app, bound = setup(isolated_env, workspace_root, 'raise SystemExit(1)')
    app.config.workflow.local_checks['compile']['stage'] = 'consumer'
    app.config.workflow.stage_gates = {'consumer': 'external_reported'}
    try:
        assert not check(app, bound)['passed']
        payload = receipt(app, bound, workspace_root, stage='consumer')
        assert app.handle('workflow.attest', payload).ok
        assert evaluate(app, bound)['acceptance'] == 'failed'
    finally:
        app.close()
