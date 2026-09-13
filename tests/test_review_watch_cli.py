from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from asterun import cli
from tests.test_review_recipe import complete, setup


def install_client(monkeypatch, base, mode):
    calls = []
    closed = []

    def handle(method, payload):
        calls.append(method)
        assert method == "task.get"
        if mode == "unknown":
            return {"ok": False, "error": {"code": "BACKEND_UNAVAILABLE", "message": "离线断连"}}
        reply = deepcopy(base.handle(method, payload))
        execution = reply["data"]
        if mode in {"waiting_input", "paused", "execution_failed", "deadline"}:
            execution["run"]["status"] = {
                "execution_failed": "failed", "deadline": "running",
            }.get(mode, mode)
        if mode == "waiting_input":
            execution["approval"] = {"id": "approval-1", "state": "pending", "target_hash": "a" * 64}
        if mode == "invalid_result":
            execution["run"]["summary"] = "模型过程消息，不是评审 JSON"
        if mode == "approve":
            value = json.loads(execution["run"]["summary"])
            value["findings"] = []
            execution["run"]["summary"] = json.dumps(value)
        return reply

    client = SimpleNamespace(handle=handle, close=lambda: closed.append(True))
    monkeypatch.setattr("asterun.service.LocalClient", lambda path: client)
    return calls, closed


@pytest.mark.parametrize("mode,reason,exit_code,verdict", [
    ("approve", "review_complete", 0, "APPROVE"),
    ("request_changes", "review_complete", 0, "REQUEST_CHANGES"),
    ("waiting_input", "waiting_input", 0, None),
    ("paused", "paused", 0, None),
    ("execution_failed", "execution_failed", 1, None),
    ("invalid_result", "invalid_result", 1, None),
    ("unknown", "unknown", 1, None),
    ("deadline", "deadline", 124, None),
])
def test_json_watch_has_one_envelope_and_preserves_observation_semantics(
        setup, monkeypatch, capsys, mode, reason, exit_code, verdict):
    prepared, _ = complete(setup)
    base, state, hub = setup
    calls, closed = install_client(monkeypatch, base, mode)
    result = cli.main(["--state-dir", str(state), "--connect", "review-watch", prepared["id"],
                       "--json", "--timeout", "0.01", "--interval", "0.005"])
    assert result == exit_code
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    assert captured.err == ""
    assert envelope["ok"] is True
    observation = envelope["data"]
    assert observation["reason"] == reason
    assert observation["resume_review_id"] == prepared["id"]
    if reason != "unknown":
        assert observation["last_snapshot"]["result"]["gate_verdict"] == verdict
    if reason == "waiting_input":
        assert observation["last_snapshot"]["execution"]["approval"]["id"] == "approval-1"
    assert calls and set(calls) == {"task.get"}
    assert closed == [True] and not hub.posts


def test_watch_without_json_preserves_existing_progress_stream(setup, monkeypatch, capsys):
    prepared, _ = complete(setup)
    base, state, _ = setup
    install_client(monkeypatch, base, "approve")
    assert cli.main(["--state-dir", str(state), "--connect", "review-watch", prepared["id"]]) == 0
    progress, final = capsys.readouterr().out.split("\n", 1)
    assert json.loads(progress)["type"] == "review_update"
    assert json.loads(final)["data"]["last_snapshot"]["result"]["gate_verdict"] == "APPROVE"


def test_json_watch_validation_error_is_one_error_envelope(setup, monkeypatch, capsys):
    prepared, _ = complete(setup)
    base, state, _ = setup
    calls, closed = install_client(monkeypatch, base, "approve")
    assert cli.main(["--state-dir", str(state), "--connect", "review-watch", prepared["id"],
                     "--json", "--timeout", "61"]) == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is False and envelope["error"]["code"] == "INVALID_REQUEST"
    assert calls == [] and closed == [True]
