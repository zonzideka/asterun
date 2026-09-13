from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from asterun.backends.codex import CodexSession, CodexAppServerTransport
from asterun.backends.codex_output import final_text
from asterun.errors import AsterunError
from asterun.review_follow import inbox, acknowledge, watch_review
from asterun.review_recipe import read_review, prepare_review, submit_review, _read, _write
from tests.test_review_recipe import setup, complete, prepare, Hub


def messages():
    return [{"id": "progress", "type": "agentMessage", "phase": "commentary", "text": "正在检查"},
            {"id": "answer", "type": "agentMessage", "phase": "final_answer", "text": '{"answer":42}'}]


def test_codex_terminal_uses_final_phase_and_keeps_progress_during_turn():
    session = CodexSession("thread", SimpleNamespace(alive=True), "/tmp", None, "now")
    session.reset_for_turn()
    session.handle_message({"method": "turn/started", "params": {"threadId": "thread", "turnId": "turn"}})
    for item in messages():
        session.handle_message({"method": "item/agentMessage/delta", "params": {"turnId": "turn", "delta": item["text"]}})
        session.handle_message({"method": "item/completed", "params": {"turnId": "turn", "item": item}})
    assert "正在检查" in session.message_text
    session.handle_message({"method": "turn/completed", "params": {"turn": {"id": "turn", "status": "completed", "items": messages()}}})
    assert session.message_text == '{"answer":42}'
    assert final_text(messages()) == session.message_text  # thread/read 对账使用同一选择规则
    session.reset_for_turn()
    session.handle_message({"method": "turn/completed", "params": {"turn": {"id": "next", "status": "completed", "items": messages()[:1]}}})
    assert session.message_text == ""  # commentary-only 不能复用上轮 final


def test_synchronous_codex_path_also_uses_final_items():
    class Transport(CodexAppServerTransport):
        def on_message(self, callback): self.callback = callback
        def start_thread(self, *args, **kwargs): return {"thread": {"id": "thread"}}
        def start_turn(self, *args, **kwargs):
            self.callback({"method": "item/agentMessage/delta", "params": {"delta": "前言与混合文本"}})
            self.callback({"method": "turn/completed", "params": {"turn": {"id": "turn", "status": "completed", "items": messages()}}})
            return {"turn": {"id": "turn"}}
    assert Transport().run_text_prompt(text="test", cwd="/tmp", timeout=1).message == '{"answer":42}'


@pytest.mark.parametrize("phase", ["commentary", "unknown"])
def test_explicit_nonfinal_never_uses_delta_fallback(phase):
    assert final_text([{"type": "agentMessage", "phase": phase, "text": "不是最终答复"}], "猜测结果") == ""


def test_legacy_unphased_messages_keep_previous_compatibility():
    assert final_text([{"type": "agentMessage", "text": "一"}, {"type": "agentMessage", "text": "二"}]) == "一\n二"
    assert final_text([], "旧版 delta") == "旧版 delta"


def test_status_returns_gate_without_requesting_publication(setup):
    prepared, _ = complete(setup)
    status = read_review(setup[0], setup[1], prepared["id"])
    assert status["result"]["gate_verdict"] == "REQUEST_CHANGES"
    assert status["result"]["counts"] == {"P0": 0, "P1": 1, "P2": 0}
    assert status["publication"]["status"] == "not_published"
    assert not setup[2].posts


@pytest.mark.parametrize("change", ["mixed", "wrong_target", "invalid_line", "drift"])
def test_success_is_not_valid_review_when_evidence_invalid(setup, change):
    prepared, _ = complete(setup)
    client, state, hub = setup
    if change == "mixed": client.summary = "我正在检查\n" + client.summary
    elif change == "wrong_target":
        value = json.loads(client.summary);value["target_hash"] = "0" * 64;client.summary = json.dumps(value)
    elif change == "invalid_line":
        value = json.loads(client.summary);value["findings"][0]["line"] = 100;client.summary = json.dumps(value)
    else:
        source = Path(prepared["snapshot_path"]) / "code.py";source.chmod(0o600);source.write_text("changed")
    status = read_review(client, state, prepared["id"])
    assert status["execution"]["run"]["status"] == "succeeded"
    assert status["result"]["stage"] == "invalid_result"
    assert status["result"]["gate_verdict"] is None and not status["result"]["publication_ready"]
    assert inbox(client, state, consumer_id="chat")["notifications"][0]["stage"] == "invalid_result"
    assert not hub.posts


def test_inbox_reads_do_not_ack_and_exact_receipt_survives_restart(setup):
    prepared, _ = complete(setup);client, state, _ = setup
    first = inbox(client, state, consumer_id="chat")["notifications"]
    assert first == inbox(client, state, consumer_id="chat")["notifications"]
    notice = first[0]
    acknowledge(client, state, prepared["id"], consumer_id="chat", notification_sha256=notice["notification_sha256"])
    assert inbox(client, Path(str(state)), consumer_id="chat")["notifications"] == []
    assert inbox(client, state, consumer_id="another")["notifications"] == first
    assert inbox(client, state, consumer_id="chat", principal="other-subject")["notifications"] == first
    value = json.loads(client.summary);value["findings"] = [];client.summary = json.dumps(value)
    assert inbox(client, state, consumer_id="chat")["notifications"][0]["gate_verdict"] == "APPROVE"
    with pytest.raises(AsterunError, match="通知已变化"):
        acknowledge(client, state, prepared["id"], consumer_id="chat", notification_sha256=notice["notification_sha256"])


def test_inbox_never_exposes_unauthorized_task(setup):
    complete(setup)
    class Denied:
        def handle(self, *args): return {"ok": False, "error": {"code": "FORBIDDEN"}}
    assert inbox(Denied(), setup[1], consumer_id="chat")["notifications"] == []


def test_corrupt_receipt_keeps_notification_pending(setup):
    from asterun.review_follow import _receipt
    prepared,_=complete(setup);client,state,_=setup
    notice=inbox(client,state,consumer_id='chat')['notifications'][0]
    acknowledge(client,state,prepared['id'],consumer_id='chat',notification_sha256=notice['notification_sha256'])
    _receipt(state,prepared['id'],'chat','local').write_text('{broken')
    assert inbox(client,state,consumer_id='chat')['notifications']==[notice]


def test_policy_lost_reply_returns_unknown_without_retry(setup):
    prepared,_=complete(setup);base,state,_=setup;calls=[]
    class Lost:
        def handle(self,method,payload):
            calls.append(method)
            if method=='approval.apply-policy':return {'ok':False,'error':{'code':'BACKEND_UNAVAILABLE'}}
            result=deepcopy(base.handle(method,payload));result['data']['run']['status']='waiting_input'
            result['data']['approval']={'id':'pending','state':'pending','target_hash':'a'*64}
            return result
    result=watch_review(Lost(),state,prepared['id'],apply_policy=True)
    assert result['reason']=='unknown' and result['last_snapshot']['execution']['approval']['id']=='pending'
    assert calls.count('approval.apply-policy')==1 and 'approval.respond' not in calls


def test_watch_reads_same_review_and_never_resubmits(setup):
    prepared, _ = complete(setup);client, state, hub = setup;client.calls.clear()
    result = watch_review(client, state, prepared["id"])
    assert result["reason"] == "review_complete"
    assert result["last_snapshot"]["result"]["gate_verdict"] == "REQUEST_CHANGES"
    assert all(method == "task.get" for method, _ in client.calls) and not hub.posts


@pytest.mark.parametrize("apply_policy,forwarded", [(False, False), (True, False), (True, True)])
def test_watch_only_applies_configured_policy_when_requested(setup, apply_policy, forwarded):
    prepared, _ = complete(setup);base, state, _ = setup
    calls=[]
    class Waiting:
        done=False
        def handle(self, method, payload):
            calls.append((method, payload))
            if method == "approval.apply-policy":
                self.done=forwarded
                return {"ok": True,"data": {"forwarded":forwarded,"manual_required":not forwarded}}
            result=deepcopy(base.handle(method,payload))
            if not self.done:
                result["data"]["run"]["status"]="waiting_input"
                result["data"]["approval"]={"id":"approval-1","state":"pending","target_hash":"a"*64}
            return result
    result=watch_review(Waiting(),state,prepared["id"],apply_policy=apply_policy)
    assert result["reason"] == ("review_complete" if apply_policy and forwarded else "waiting_input")
    assert sum(m=="approval.apply-policy" for m,_ in calls)==int(apply_policy)
    assert not any(m in {"task.submit","approval.respond"} for m,_ in calls)


def test_watch_deadline_returns_resumable_reference(setup):
    prepared,_=complete(setup);client,state,_=setup
    class Running:
        def handle(self,method,payload):
            result=deepcopy(client.handle(method,payload));result["data"]["run"]["status"]="running";return result
    now=[0.0]
    result=watch_review(Running(),state,prepared["id"],timeout=2,interval=1,clock=lambda:now[0],sleep=lambda n:now.__setitem__(0,now[0]+n))
    assert result["reason"]=="deadline" and result["resume_review_id"]==prepared["id"]


def test_linked_review_binds_previous_findings_and_cannot_raise_limit(setup):
    prior,_=complete(setup);client,state,hub=setup;hub.head="c"*40
    next_review=prepare_review(client,state,workspace="review",backend="codex",repo="owner/repo",pr=55,
        attempt="two",github=hub,previous_review=prior["id"],max_rounds=2)
    manifest=_read(state/'reviews'/next_review['id']/'manifest.json')
    assert manifest['lineage']['round']==2
    assert manifest['lineage']['previous_head']==prior['identity']['head']
    assert '[P1]' in manifest['prompt'] and 'previous_findings' in manifest['prompt']
    with pytest.raises(AsterunError,match="上轮上下文"):
        prepare_review(client,state,workspace="review",backend="codex",repo="owner/repo",pr=55,attempt="two",github=hub)
    submit_review(client,state,next_review['id'],github=hub);client.input_hash=manifest['task_input_hash']
    client.summary=json.dumps({'target_hash':manifest['target']['hash'],'findings':[]})
    with pytest.raises(AsterunError,match="轮次上限"):
        prepare_review(client,state,workspace="review",backend="codex",repo="owner/repo",pr=55,attempt="three",github=hub,previous_review=next_review['id'],max_rounds=2)
    with pytest.raises(AsterunError,match="不能提高"):
        prepare_review(client,state,workspace="review",backend="codex",repo="owner/repo",pr=55,attempt="three",github=hub,previous_review=next_review['id'],max_rounds=3)


def test_real_sqlite_mcp_status_inbox_ack_and_reopen(isolated_env):
    from asterun.application import Application
    from asterun.mcp_server import handle_rpc
    from tests.conftest import write_config
    root=isolated_env/'review-workspace';root.mkdir()
    config=write_config(isolated_env/'review.json',root)
    state=isolated_env/'state'
    app=Application.from_paths(config,state)
    try:
        prepared=prepare_review(app,state,workspace='demo',backend='fake',repo='owner/repo',pr=55,attempt='mcp',github=Hub())
        submit_review(app,state,prepared['id'],github=Hub())
        manifest=_read(state/'reviews'/prepared['id']/'manifest.json')
        from asterun.ids import TaskId
        task=app.store.get_task(TaskId(manifest['task_id']));run=app.store.get_run(task.current_run_id)
        run.summary=json.dumps({'target_hash':manifest['target']['hash'],'findings':[]});app.store.save_run(run)
        def call(name,args):
            r=handle_rpc(app,{'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':args}})
            obj=json.loads(r['result']['content'][0]['text']);assert obj['ok'],obj;return obj['data']
        assert call('review_status',{'review_id':prepared['id']})['result']['gate_verdict']=='APPROVE'
        notice=call('review_inbox',{'consumer_id':'chat'})['notifications'][0]
        assert call('review_ack',{'review_id':prepared['id'],'consumer_id':'chat','notification_sha256':notice['notification_sha256']})['acknowledged']
        assert call('review_inbox',{'consumer_id':'chat'})['notifications']==[]
        assert len(app.store.list_task_ids())==1
    finally:app.close()
    reopened=Application.from_paths(config,state)
    try:assert reopened.handle('review.inbox',{'consumer_id':'chat'}).data['notifications']==[]
    finally:reopened.close()
