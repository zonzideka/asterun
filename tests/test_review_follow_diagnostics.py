from copy import deepcopy

import pytest

from asterun.review_follow import watch_review
from tests.test_review_recipe import complete, setup


@pytest.mark.parametrize("expired", [False, True])
def test_read_transport_failure_distinguishes_watch_deadline(setup, expired):
    prepared, _ = complete(setup)
    now = [0.0]

    class Lost:
        def handle(self, method, payload):
            assert method == "task.get"
            now[0] = 2.0 if expired else 0.5
            return {"ok": False, "error": {"code": "BACKEND_UNAVAILABLE"}}

    result = watch_review(Lost(), setup[1], prepared["id"], timeout=2, clock=lambda: now[0])
    assert result["reason"] == ("deadline" if expired else "unknown")
    assert result["resume_review_id"] == prepared["id"]
    assert result["last_snapshot"] is None and result["error"]


@pytest.mark.parametrize("lost", [False, True])
def test_policy_reason_is_returned_but_lost_forward_is_never_deadline(setup, lost):
    prepared, _ = complete(setup)
    base, state, _ = setup
    now = [0.0]
    calls = []
    assessment = {"decision": "manual", "reason": "manual_mode"}

    class Waiting:
        def handle(self, method, payload):
            calls.append(method)
            if method == "approval.apply-policy":
                if lost:
                    now[0] = 2.0
                    return {"ok": False, "error": {"code": "BACKEND_UNAVAILABLE"}}
                return {"ok": True, "data": {"forwarded": False, "manual_required": True,
                                               "assessment": assessment}}
            value = deepcopy(base.handle(method, payload))
            value["data"]["run"]["status"] = "waiting_input"
            value["data"]["approval"] = {"id": "pending", "state": "pending", "target_hash": "a" * 64}
            return value

    result = watch_review(Waiting(), state, prepared["id"], timeout=2, apply_policy=True,
                          clock=lambda: now[0])
    assert calls == ["task.get", "approval.apply-policy"]
    if lost:
        assert result["reason"] == "unknown" and result["error"]
    else:
        assert result["reason"] == "waiting_input"
        assert result["policy_result"] == {"approval_id": "pending", "forwarded": False,
                                           "manual_required": True, "assessment": assessment}
        assert result["last_snapshot"]["execution"]["approval"]["target_hash"] == "a" * 64
