"""Unit tests for the context-budget admission ledger."""

import sqlite3
from types import SimpleNamespace

import pytest

from asterun.context_budget import ledger
from asterun.contracts import TERMINAL_RUN_STATUSES
from asterun.control_store import ControlStore
from asterun.errors import AsterunError

_TERMINAL_STATUS = next(iter(TERMINAL_RUN_STATUSES))
_PROFILE_KEY = "grok-code/implementation"


class _ExplodingConn:
    def __getattr__(self, _name):
        raise AssertionError("database accessed")


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    ControlStore(connection)
    try:
        yield connection
    finally:
        connection.close()


def _app(conn, config=None):
    return SimpleNamespace(
        store=SimpleNamespace(_conn=conn),
        config=object() if config is None else config,
    )


def _run(
    run_id="run_1",
    task_id="tsk_1",
    *,
    status=None,
    native=None,
    role=None,
):
    return SimpleNamespace(
        id=SimpleNamespace(value=run_id),
        task_id=SimpleNamespace(value=task_id),
        backend=SimpleNamespace(value="grok-code"),
        role=role,
        status=_TERMINAL_STATUS if status is None else status,
        native={} if native is None else native,
    )


def _settings(mode="enforce", task_hard=100, segment_soft=60, *, include_hard=True):
    profile = {}
    if include_hard:
        profile["task_hard"] = task_hard
    if segment_soft is not None:
        profile["segment_soft"] = segment_soft
    return {"mode": mode, "profiles": {_PROFILE_KEY: profile}}


def _install(monkeypatch, settings, breakdown=None, *, seen=None):
    monkeypatch.setattr(
        "asterun.context_budget.ledger.context_budget_settings",
        lambda config: settings,
    )

    def _match(app, backend, role, profiles):
        if seen is not None:
            seen["app"] = app
            seen["backend"] = backend
            seen["role"] = role
            seen["profiles"] = profiles
        return (_PROFILE_KEY, "implementation")

    monkeypatch.setattr("asterun.context_budget.ledger._matched_profile", _match)
    if breakdown is not None:
        monkeypatch.setattr(
            "asterun.context_budget.ledger.normalize_breakdown",
            lambda native: breakdown,
        )


def _load_budget(conn, task_id):
    store = ControlStore(conn)
    try:
        return store.get("Budget", ledger.budget_id(task_id), ledger.NAMESPACE)
    except AsterunError as exc:
        if exc.code == "NOT_FOUND":
            return None
        raise


def _load_reservation(conn, run_id):
    store = ControlStore(conn)
    return store.get("Reservation", ledger.reservation_id(run_id), ledger.NAMESPACE)


@pytest.mark.parametrize("mode", ["off", "observe"])
def test_non_enforce_returns_none_without_budget_or_database(monkeypatch, mode, conn):
    app = _app(conn)
    _install(monkeypatch, {"mode": mode})

    assert ledger.guard_dispatch(app, _run()) is None
    assert _load_budget(conn, "tsk_1") is None

    exploding = _app(_ExplodingConn())
    assert ledger.guard_dispatch(exploding, _run()) is None


def test_enforce_reserve_succeeds_and_repeat_is_idempotent(monkeypatch, conn):
    app = _app(conn)
    seen = {}
    _install(monkeypatch, _settings(task_hard=100, segment_soft=60), seen=seen)
    run = _run(role=None)

    first = ledger.guard_dispatch(app, run)
    second = ledger.guard_dispatch(app, run)

    assert seen["backend"] == "grok-code"
    assert seen["role"] == "implementation"
    assert first["id"] == second["id"] == ledger.reservation_id("run_1")
    assert first["status"] == second["status"] == "HELD"
    assert first["action_id"] == second["action_id"] == "run_1"
    assert first["actual"] is None
    assert first["reserved"] == second["reserved"]
    assert first["reserved"][0]["amount"] == 60
    assert ledger.budget_id("tsk_1").startswith("bud_cb_")
    assert len(ledger.budget_id("tsk_1")) == len("bud_cb_") + 24

    budget = _load_budget(conn, "tsk_1")
    assert budget is not None
    assert budget["limits"][0]["limit"] == 100
    view = ledger.budget_view(conn, "tsk_1")
    assert view["held"] == 60
    assert view["settled"] == 0


def test_second_reservation_raises_budget_exhausted_with_totals(monkeypatch, conn):
    app = _app(conn)
    _install(monkeypatch, _settings(task_hard=100, segment_soft=60))

    first = ledger.guard_dispatch(app, _run("run_1"))
    assert first["status"] == "HELD"

    with pytest.raises(AsterunError) as exc:
        ledger.guard_dispatch(app, _run("run_2"))

    err = exc.value
    assert err.code == "BUDGET_EXHAUSTED"
    assert err.details == {
        "budget_id": ledger.budget_id("tsk_1"),
        "task_hard": 100,
        "settled": 0,
        "held": 60,
        "requested": 60,
    }
    assert _load_reservation(conn, "run_1")["status"] == "HELD"
    with pytest.raises(AsterunError) as missing:
        _load_reservation(conn, "run_2")
    assert missing.value.code == "NOT_FOUND"
    view = ledger.budget_view(conn, "tsk_1")
    assert view["settled"] == 0
    assert view["held"] == 60
    assert view["task_hard"] == 100


def test_settle_counts_processed_and_repeat_is_idempotent(monkeypatch, conn):
    app = _app(conn)
    _install(
        monkeypatch,
        _settings(task_hard=100, segment_soft=60),
        breakdown={"processed": 40, "attributable": True},
    )
    run = _run(native={"usage": {"processed": 40}})

    ledger.guard_dispatch(app, run)
    ledger.settle_run(app, run)

    assert _load_reservation(conn, "run_1")["status"] == "SETTLED"
    view = ledger.budget_view(conn, "tsk_1")
    assert view["settled"] == 40
    assert view["held"] == 0
    assert view["uncertain_reservations"] == 0

    ledger.settle_run(app, run)
    again = ledger.budget_view(conn, "tsk_1")
    assert again["settled"] == 40
    assert again["held"] == 0
    assert _load_reservation(conn, "run_1")["status"] == "SETTLED"


def test_unattributable_usage_marks_uncertain_and_keeps_hold(monkeypatch, conn):
    app = _app(conn)
    _install(
        monkeypatch,
        _settings(task_hard=100, segment_soft=60),
        breakdown={"processed": None, "attributable": False},
    )
    run = _run(native={})

    ledger.guard_dispatch(app, run)
    ledger.settle_run(app, run)

    assert _load_reservation(conn, "run_1")["status"] == "UNCERTAIN"
    view = ledger.budget_view(conn, "tsk_1")
    assert view["settled"] == 0
    assert view["held"] == 60
    assert view["uncertain_reservations"] == 1

    ledger.settle_run(app, run)
    assert _load_reservation(conn, "run_1")["status"] == "UNCERTAIN"
    assert ledger.budget_view(conn, "tsk_1")["held"] == 60
    assert ledger.budget_view(conn, "tsk_1")["uncertain_reservations"] == 1


def test_existing_budget_limit_stays_when_settings_change(monkeypatch, conn):
    app = _app(conn)
    holder = {"settings": _settings(task_hard=100, segment_soft=60)}
    monkeypatch.setattr(
        "asterun.context_budget.ledger.context_budget_settings",
        lambda config: holder["settings"],
    )
    monkeypatch.setattr(
        "asterun.context_budget.ledger._matched_profile",
        lambda app, backend, role, profiles: (_PROFILE_KEY, "implementation"),
    )

    ledger.guard_dispatch(app, _run("run_1"))
    holder["settings"] = _settings(task_hard=999, segment_soft=10)
    second = ledger.guard_dispatch(app, _run("run_2"))

    assert second["reserved"][0]["amount"] == 10
    budget = _load_budget(conn, "tsk_1")
    assert budget["limits"][0]["limit"] == 100
    assert budget["extensions"]["asterun.profile_key"] == _PROFILE_KEY
    view = ledger.budget_view(conn, "tsk_1")
    assert view["task_hard"] == 100
    assert view["held"] == 70
    assert view["profile_key"] == _PROFILE_KEY
    assert view["enforcement"] == "admission_only"


@pytest.mark.parametrize(
    "profile",
    [
        {"segment_soft": 60},
        {"task_hard": 0, "segment_soft": 60},
        {"task_hard": -5},
        {"task_hard": "100"},
        {"task_hard": 1.5},
        {"task_hard": False},
        {},
    ],
)
def test_missing_or_non_positive_task_hard_does_not_block(monkeypatch, profile, conn):
    app = _app(conn)
    _install(
        monkeypatch,
        {"mode": "enforce", "profiles": {_PROFILE_KEY: profile}},
    )

    assert ledger.guard_dispatch(app, _run()) is None
    assert _load_budget(conn, "tsk_1") is None


def test_budget_view_reports_fields_and_none_without_budget(monkeypatch, conn):
    assert ledger.budget_view(conn, "tsk_missing") is None

    app = _app(conn)
    _install(monkeypatch, _settings(task_hard=100, segment_soft=60))
    ledger.guard_dispatch(app, _run())

    assert ledger.budget_view(conn, "tsk_1") == {
        "budget_id": ledger.budget_id("tsk_1"),
        "task_hard": 100,
        "settled": 0,
        "held": 60,
        "uncertain_reservations": 0,
        "profile_key": _PROFILE_KEY,
        "enforcement": "admission_only",
    }
    assert ledger.budget_view(conn, "tsk_other") is None


def test_repair_after_settle_stays_on_first_limit(monkeypatch, conn):
    """implementation 结算 70 后，repair 仍受首次上限约束。"""
    app = _app(conn)
    holder = {
        "settings": {
            "mode": "enforce",
            "profiles": {
                "grok-code/implementation": {"task_hard": 100, "segment_soft": 60},
            },
        }
    }
    monkeypatch.setattr(
        "asterun.context_budget.ledger.context_budget_settings",
        lambda config: holder["settings"],
    )
    monkeypatch.setattr(
        "asterun.context_budget.ledger.normalize_breakdown",
        lambda native: {"processed": 70, "attributable": True},
    )
    run = _run("run_1", role="implementation", native={"usage": {"processed": 70}})
    ledger.guard_dispatch(app, run)
    ledger.settle_run(app, run)
    assert ledger.budget_view(conn, "tsk_1")["settled"] == 70
    assert _load_budget(conn, "tsk_1")["limits"][0]["limit"] == 100

    holder["settings"] = {
        "mode": "enforce",
        "profiles": {"grok-code/repair": {"segment_soft": 60}},
    }
    with pytest.raises(AsterunError) as soft_only:
        ledger.guard_dispatch(app, _run("run_soft", role="repair"))
    assert soft_only.value.code == "BUDGET_EXHAUSTED"
    assert soft_only.value.details["task_hard"] == 100
    assert soft_only.value.details["settled"] == 70
    assert soft_only.value.details["held"] == 0
    assert soft_only.value.details["requested"] == 60
    with pytest.raises(AsterunError) as missing:
        _load_reservation(conn, "run_soft")
    assert missing.value.code == "NOT_FOUND"

    holder["settings"] = {"mode": "enforce", "profiles": {}}
    bare = ledger.guard_dispatch(app, _run("run_bare", role="repair"))
    assert bare is not None
    assert bare["status"] == "HELD"
    assert bare["reserved"][0]["amount"] == 30
    assert _load_budget(conn, "tsk_1")["limits"][0]["limit"] == 100
    view = ledger.budget_view(conn, "tsk_1")
    assert view["task_hard"] == 100
    assert view["settled"] == 70
    assert view["held"] == 30

    with pytest.raises(AsterunError) as again:
        ledger.guard_dispatch(app, _run("run_bare_2", role="repair"))
    assert again.value.code == "BUDGET_EXHAUSTED"
    assert again.value.details == {
        "budget_id": ledger.budget_id("tsk_1"),
        "task_hard": 100,
        "settled": 70,
        "held": 30,
        "requested": 1,
    }


def test_missing_segment_soft_reserves_remaining(monkeypatch, conn):
    app = _app(conn)
    holder = {"settings": _settings(task_hard=100, segment_soft=60)}
    monkeypatch.setattr(
        "asterun.context_budget.ledger.context_budget_settings",
        lambda config: holder["settings"],
    )
    monkeypatch.setattr(
        "asterun.context_budget.ledger._matched_profile",
        lambda app, backend, role, profiles: (_PROFILE_KEY, "implementation"),
    )
    monkeypatch.setattr(
        "asterun.context_budget.ledger.normalize_breakdown",
        lambda native: {"processed": 40, "attributable": True},
    )
    ledger.guard_dispatch(app, _run("run_1"))
    ledger.settle_run(app, _run("run_1"))
    holder["settings"] = _settings(task_hard=500, segment_soft=None)
    second = ledger.guard_dispatch(app, _run("run_2"))
    assert second["status"] == "HELD"
    assert second["reserved"][0]["amount"] == 60
    assert _load_budget(conn, "tsk_1")["limits"][0]["limit"] == 100
    view = ledger.budget_view(conn, "tsk_1")
    assert view["settled"] == 40
    assert view["held"] == 60
    assert view["task_hard"] == 100


def test_budget_view_reads_without_opening_a_transaction(monkeypatch, conn):
    app = _app(conn)
    _install(monkeypatch, _settings(task_hard=100, segment_soft=60))
    ledger.guard_dispatch(app, _run())
    assert conn.in_transaction is False

    def _explode(self):
        raise AssertionError("transaction opened")

    monkeypatch.setattr(ControlStore, "transaction", _explode)
    assert conn.in_transaction is False
    view = ledger.budget_view(conn, "tsk_1")
    assert conn.in_transaction is False
    assert view == {
        "budget_id": ledger.budget_id("tsk_1"),
        "task_hard": 100,
        "settled": 0,
        "held": 60,
        "uncertain_reservations": 0,
        "profile_key": _PROFILE_KEY,
        "enforcement": "admission_only",
    }


def test_settle_ignores_non_terminal_and_swallows_errors(monkeypatch, conn):
    app = _app(conn)
    _install(
        monkeypatch,
        _settings(task_hard=100, segment_soft=60),
        breakdown={"processed": 10, "attributable": True},
    )
    run = _run(status="not-a-terminal-status")
    assert run.status not in TERMINAL_RUN_STATUSES

    ledger.guard_dispatch(app, run)
    ledger.settle_run(app, run)
    assert _load_reservation(conn, "run_1")["status"] == "HELD"

    def _boom(_config):
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(
        "asterun.context_budget.ledger.context_budget_settings",
        _boom,
    )
    ledger.settle_run(app, run)
    assert _load_reservation(conn, "run_1")["status"] == "HELD"


@pytest.mark.parametrize("mode", ["observe", "off"])
def test_terminal_settle_after_mode_switch(monkeypatch, conn, mode):
    app = _app(conn)
    holder = {"settings": _settings(task_hard=100, segment_soft=60)}
    monkeypatch.setattr(
        "asterun.context_budget.ledger.context_budget_settings",
        lambda config: holder["settings"],
    )
    monkeypatch.setattr(
        "asterun.context_budget.ledger._matched_profile",
        lambda app, backend, role, profiles: (_PROFILE_KEY, "implementation"),
    )
    monkeypatch.setattr(
        "asterun.context_budget.ledger.normalize_breakdown",
        lambda native: {"processed": 40, "attributable": True},
    )
    run = _run(native={"usage": {"processed": 40}})

    ledger.guard_dispatch(app, run)
    holder["settings"] = _settings(mode=mode, task_hard=100, segment_soft=60)
    ledger.settle_run(app, run)

    assert _load_reservation(conn, "run_1")["status"] == "SETTLED"
    view = ledger.budget_view(conn, "tsk_1")
    assert view["settled"] == 40
    assert view["held"] == 0


def test_repeat_guard_without_segment_soft_returns_same_reservation(monkeypatch, conn):
    app = _app(conn)
    _install(monkeypatch, _settings(task_hard=100, segment_soft=None))
    run = _run()

    first = ledger.guard_dispatch(app, run)
    second = ledger.guard_dispatch(app, run)

    assert first["id"] == second["id"] == ledger.reservation_id("run_1")
    assert first["status"] == second["status"] == "HELD"
    assert first["action_id"] == second["action_id"] == "run_1"
    assert first["task_id"] == second["task_id"]
    assert first["budget_id"] == second["budget_id"] == ledger.budget_id("tsk_1")
    assert first["reserved"] == second["reserved"]
    assert first["reserved"][0]["amount"] == 100
    view = ledger.budget_view(conn, "tsk_1")
    assert view["held"] == 100
    assert view["settled"] == 0
