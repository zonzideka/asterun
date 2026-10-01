from copy import deepcopy
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from asterun.backends.codex_context import compact, preflight
from asterun.backends.codex_runtime import CodexRuntime
from asterun.errors import AsterunError


class Transport:
    alive = True
    def __init__(self, mode='success'):
        self.mode = mode; self.calls = []; self.handlers = []
    def on_message(self, handler): self.handlers.append(handler)
    def request(self, method, params, **kwargs):
        self.calls.append((method,params))
        if self.mode == 'ack_only': return {}
        thread = 'other-thread' if self.mode == 'wrong_thread' else 'thread'
        tid = 'old-turn' if self.mode == 'old_turn' else 'compact-turn'
        events = [dict(method='turn/started', params={'threadId':thread,'turn':{'id':tid}})]
        if self.mode != 'no_item':
            events.append(dict(method='item/completed', params={'threadId':thread,'turnId':tid,'item':{'type':'contextCompaction','id':'compact-item'}}))
        events.append(dict(method='turn/completed',params={'threadId':thread,'turn':{'id':tid,'status':'completed'}}))
        for event in events:
            for handler in self.handlers: handler(event)
        return {}


def fixture(mode='success'):
    transport = Transport(mode)
    session = SimpleNamespace(session_id='thread', cwd='/tmp/fixture', pending_requests={}, to_status_dict=lambda:{})
    def read(*args,**kwargs):
        return {'turns':[{'id':'compact-turn','status':'completed','items':[] if mode=='no_history_item' else [{'type':'contextCompaction'}]}]}
    runtime = SimpleNamespace(_read=read)
    record = {'strategy':'native_compact', 'source_native':{'turn_id':'old-turn'}}
    return runtime, transport, session, record


@pytest.mark.parametrize('mode',['ack_only','wrong_thread','old_turn','no_item','no_history_item'])
def test_ack_and_unbound_or_incomplete_events_are_not_success(mode):
    args = fixture(mode); updates=[]
    with pytest.raises(AsterunError): compact(*args, lambda r:updates.append(deepcopy(r)), Event(),lambda:False,timeout=.04)
    assert len(args[1].calls) == 1
    assert all(r['native']['context_transition']['phase'] != 'compaction_completed' for r in updates)


@pytest.mark.parametrize('phase,calls',[('compaction_requested',0),('compaction_acknowledged',1),('compaction_completed',1)])
def test_each_compaction_crash_boundary_fails_without_second_request(phase,calls):
    args=fixture(); updates=[]
    def publish(value):
        updates.append(deepcopy(value))
        if value['native']['context_transition']['phase']==phase: raise OSError('disk full')
    with pytest.raises(OSError): compact(*args,publish,Event(),lambda:False,timeout=.04)
    assert len(args[1].calls)==calls


def test_cancel_after_ack_never_completes():
    args=fixture(); cancel=[False]
    def publish(value):
        if value['native']['context_transition']['phase']=='compaction_acknowledged':cancel[0]=True
    with pytest.raises(AsterunError):compact(*args,publish,Event(),lambda:cancel[0],timeout=.04)


@pytest.mark.parametrize('phase',['prepared','session_create_requested','session_bound','compaction_requested','compaction_acknowledged','compaction_completed','prompt_requested'])
def test_reconcile_never_turns_old_completion_into_successor_success(phase):
    runtime=CodexRuntime(SimpleNamespace())
    runtime._connect=lambda *a,**k:pytest.fail('uncertain transition must not connect or replay')
    result=runtime.reconcile({'turn_id':'old-turn','context_transition':{'phase':phase}},Path('/tmp/fixture'))
    assert result['status']=='pending_reconcile' and not result['terminated']


def test_protocol_preflight_cannot_accept_unknown_installation(tmp_path):
    binary=tmp_path/'unsupported';binary.write_text('#!/bin/sh\nexit 0\n');binary.chmod(0o700)
    with pytest.raises(AsterunError,match='schema'):preflight(str(binary))
