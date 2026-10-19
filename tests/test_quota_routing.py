"""Direct tests for quota routing entry points."""

import sqlite3
from types import SimpleNamespace

import pytest

from asterun.errors import AsterunError
from asterun.quota import routing_entry

_OPEN = []


@pytest.fixture(autouse=True)
def _close_connections():
    yield
    while _OPEN:
        _OPEN.pop().close()


def _code(exc):
    for attr in ("code", "error_code", "kind"):
        if hasattr(exc, attr):
            value = getattr(exc, attr)
            return getattr(value, "value", value)
    if exc.args:
        return exc.args[0]
    return str(exc)


def _app(*, schema_version=2, routing=None, default_backend=None, admission=None):
    if routing is None:
        routing = {
            "candidate_groups": {
                "g": {
                    "role": "coder",
                    "members": ["a", "b"],
                    "primary": "a",
                }
            },
            "quota_preference": {},
        }
    conn = sqlite3.connect(":memory:")
    _OPEN.append(conn)
    return SimpleNamespace(
        config=SimpleNamespace(
            schema_version=schema_version,
            default_backend=default_backend,
            routing=routing,
        ),
        store=SimpleNamespace(_conn=conn),
        admission=admission,
    )


def _patch_mode(monkeypatch, mode):
    monkeypatch.setattr(routing_entry, "quota_preference_mode", lambda app: mode)


def _patch_eval(monkeypatch, result, sticky_box=None):
    def last_chosen(conn, namespace, group):
        if sticky_box is not None:
            sticky_box["group"] = group
            sticky_box["namespace"] = namespace
        return "sticky-alias"

    def evaluate_group(app, group, sticky=None):
        if sticky_box is not None:
            sticky_box["eval_group"] = group
            sticky_box["sticky"] = sticky
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(routing_entry, "last_chosen", last_chosen)
    monkeypatch.setattr(routing_entry, "evaluate_group", evaluate_group)


def test_explicit_backend_returns_none():
    name, routing = routing_entry.resolve_backend(_app(), "plain")
    assert name == "plain"
    assert routing is None


def test_off_skips_evaluate(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("evaluate_group should not run when preference is off")

    _patch_mode(monkeypatch, "off")
    monkeypatch.setattr(routing_entry, "evaluate_group", fail)
    monkeypatch.setattr(routing_entry, "last_chosen", fail)

    name, routing = routing_entry.resolve_backend(_app(), "@g")

    assert name == "a"
    assert routing == {
        "mode": "off",
        "group": "g",
        "primary": "a",
        "chosen": "a",
        "reason_code": "preference_off",
        "would_choose": None,
    }


def test_dry_run_keeps_primary_and_records_would_choose(monkeypatch):
    seen = {}
    _patch_mode(monkeypatch, "dry_run")
    res = {
        "chosen": "b",
        "primary": "a",
        "mode": "dry_run",
        "reason_code": "lower_used",
        "candidates": ["a", "b"],
    }
    _patch_eval(monkeypatch, res, seen)

    name, routing = routing_entry.resolve_backend(_app(), "@g")

    assert name == "a"
    assert routing["mode"] == "dry_run"
    assert routing["group"] == "g"
    assert routing["primary"] == "a"
    assert routing["chosen"] == "a"
    assert routing["would_choose"] == "b"
    assert routing["reason_code"] == "lower_used"
    assert routing["_result"] is res
    assert seen["sticky"] == "sticky-alias"
    assert seen["eval_group"] == "g"
    assert seen["group"] == "g"


def test_on_uses_chosen(monkeypatch):
    seen = {}
    _patch_mode(monkeypatch, "on")
    res = {
        "chosen": "b",
        "primary": "a",
        "mode": "on",
        "reason_code": "lower_used",
        "candidates": ["a", "b"],
    }
    _patch_eval(monkeypatch, res, seen)

    name, routing = routing_entry.resolve_backend(_app(), "@g")

    assert name == "b"
    assert routing["mode"] == "on"
    assert routing["chosen"] == "b"
    assert routing["primary"] == "a"
    assert routing["would_choose"] is None
    assert routing["reason_code"] == "lower_used"
    assert routing["_result"] is res
    assert seen["sticky"] == "sticky-alias"


def test_unknown_group_is_invalid_request():
    with pytest.raises(AsterunError) as exc:
        routing_entry.resolve_backend(_app(), "@missing")
    assert _code(exc.value) == "INVALID_REQUEST"


def test_v1_group_is_invalid_config():
    with pytest.raises(AsterunError) as exc:
        routing_entry.resolve_backend(_app(schema_version=1), "@g")
    assert _code(exc.value) == "INVALID_CONFIG"


def test_evaluate_exception_falls_back_to_primary(monkeypatch):
    _patch_mode(monkeypatch, "on")
    _patch_eval(monkeypatch, RuntimeError("quota store down"))

    name, routing = routing_entry.resolve_backend(_app(), "@g")

    assert name == "a"
    assert routing["mode"] == "on"
    assert routing["group"] == "g"
    assert routing["primary"] == "a"
    assert routing["chosen"] == "a"
    assert routing["reason_code"] == "quota_state_invalid"
    assert routing["would_choose"] is None
    assert "_result" not in routing


def _window(freshness, *, used_bp=0, confidence="high", paid=False):
    return {
        "freshness": freshness,
        "observation": {
            "used_bp": used_bp,
            "confidence": confidence,
            "paid_overage": paid,
        },
    }


def test_paid_guard_allows_when_flag_set(monkeypatch):
    app = _app(
        routing={
            "quota_preference": {"allow_paid_on_explicit": True},
        }
    )

    def fail(*args, **kwargs):
        raise AssertionError("windows should not be read when paid overage is allowed")

    monkeypatch.setattr(routing_entry, "_member_windows", fail)
    routing_entry.paid_guard(app, "plain")


def test_paid_guard_swallows_window_errors(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("no quota state")

    monkeypatch.setattr(routing_entry, "_member_windows", boom)
    routing_entry.paid_guard(_app(), "plain")


def test_paid_guard_ignores_exhausted_without_paid_overage(monkeypatch):
    monkeypatch.setattr(
        routing_entry,
        "_member_windows",
        lambda app, name: [_window("exhausted", confidence="high", paid=False)],
    )
    routing_entry.paid_guard(_app(), "plain")


def test_paid_guard_blocks_exhausted_paid_overage(monkeypatch):
    monkeypatch.setattr(
        routing_entry,
        "_member_windows",
        lambda app, name: [
            _window("fresh", used_bp=10000, confidence="medium", paid=True),
        ],
    )
    with pytest.raises(AsterunError) as exc:
        routing_entry.paid_guard(_app(), "plain")
    assert _code(exc.value) == "BUDGET_EXHAUSTED"
    assert exc.value.details == {"backend": "plain", "auto_retry": False}


def test_record_swallows_decision_errors(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(routing_entry, "record_decision", boom)
    routing_entry.record(
        _app(),
        "task-1",
        "run-1",
        "coder",
        {
            "mode": "off",
            "group": "@g",
            "primary": "a",
            "chosen": "a",
            "reason_code": "preference_off",
        },
    )


def test_view_reads_would_choose_from_payload(monkeypatch):
    monkeypatch.setattr(
        routing_entry,
        "latest_for_task",
        lambda conn, namespace, task_id: {
            "mode": "dry_run",
            "group_name": "@g",
            "chosen": "a",
            "primary_alias": "a",
            "reason_code": "lower_used",
            "payload": {"would_choose": "b"},
        },
    )
    view = routing_entry.view(_app(), "task-1")
    assert view == {
        "mode": "dry_run",
        "group": "@g",
        "chosen": "a",
        "primary": "a",
        "reason_code": "lower_used",
        "would_choose": "b",
    }


def test_view_returns_none_on_error(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("missing table")

    monkeypatch.setattr(routing_entry, "latest_for_task", boom)
    assert routing_entry.view(_app(), "task-1") is None
