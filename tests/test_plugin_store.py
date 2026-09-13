from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import sqlite3
import threading

import pytest

from asterun.connections.models import Connection, PreparedPlan, ProviderAccount
from asterun.control_protocol import digest
from asterun.control_store import ControlStore
from asterun.errors import AsterunError
from asterun.plugin_store import PluginStore, TABLE_COLUMNS, validate_snapshot
from asterun.usage.models import BillingPool, UsageObservation

NS, PRINCIPAL = "ns_test", "test-user"
NOW = "2026-09-11T00:00:00Z"


def pools(limit=10, concurrency=10, window="test-window", remaining=None):
    return {"shared": {"billing_mode": "local", "paid_overage_allowed": False,
                       "remaining": remaining, "meter": "requests", "window_id": window,
                       "local_limit": limit, "max_concurrency": concurrency, "estimate_per_action": 1}}


def plan(id="ppl_one", amount=1, connection="first", namespace=NS, principal=PRINCIPAL,
         window="test-window", pool="shared", meter="requests"):
    binding = {"connection_ref": connection, "provider_account_ref": "provider-account", "model": "test-model",
               "workspace_root": "/unused-offline", "input_sha256": "a" * 64}
    return {"id": id, "namespace_id": namespace, "principal_id": principal,
            "binding": binding, "binding_sha256": digest(binding),
            "reservations": [{"pool_ref": pool, "meter": meter, "amount": amount, "window_id": window}],
            "created_at": NOW}


def observation(cursor="one", used=10, *, sequence=1, source="provider_observed", scope="session_cumulative",
                window="test-window", **extra):
    return {"pool_ref": "shared", "meter": "tokens", "scope": scope, "scope_ref": "session-one",
            "window_id": window, "cursor": cursor, "source_kind": source, "used": used,
            "sequence": sequence, "observed_at": NOW, **extra}


@contextmanager
def opened(path=":memory:"):
    conn = sqlite3.connect(path, isolation_level=None, timeout=10)
    try:
        yield PluginStore(conn)
    finally:
        conn.close()


@pytest.fixture
def store():
    with opened() as value:
        yield value


def error(code, callable, *args, **kwargs):
    with pytest.raises(AsterunError) as exc:
        callable(*args, **kwargs)
    assert exc.value.code == code


def admit(store, id="one", **options):
    return store.admit(plan("ppl_" + id, **options), pools(), legacy_run_id="run_" + id)


def test_plan_is_deeply_immutable_and_scoped_to_authenticated_principal(store):
    value = plan()
    saved = store.put_plan(value)
    value["binding"]["model"] = "changed"
    saved["reservations"][0]["amount"] = 99
    assert store.get_plan("ppl_one", PRINCIPAL, NS) == plan()
    error("NOT_FOUND", store.get_plan, "ppl_one", "other-user", NS)
    error("NOT_FOUND", store.get_plan, "ppl_one", PRINCIPAL, "ns_other")
    error("BINDING_MISMATCH", store.put_plan, value)
    changed = plan(amount=2)
    error("BINDING_MISMATCH", store.put_plan, changed)


@pytest.mark.parametrize("bad", [True, -1, 0.1, "1", 2 ** 53])
def test_money_and_other_meters_reject_float_or_unsafe_values(bad):
    error("INVALID_REQUEST", PreparedPlan.from_dict, plan(amount=bad, meter="micro_usd"))
    error("INVALID_REQUEST", BillingPool.from_dict, "pool", {**pools()["shared"], "remaining": bad})


def test_identity_models_keep_provider_account_separate_and_return_copies():
    provider = ProviderAccount.from_dict("provider-google", {"provider": "google", "credential_ref": "env:TEST_ONLY",
                                                            "credential_revision": 2, "revoked": False})
    assert provider.to_dict()["ref"] == "provider-google"
    connection = Connection.from_dict("test-connection", {"plugin_id": "example.fake", "enabled": True,
        "provider_account_ref": "provider-google", "runtime_ref": "test-host", "auth_mode": "none",
        "billing_pool_refs": ["shared"], "options": {"model": "first"}})
    returned = connection.to_dict()
    returned["options"]["model"] = "second"
    assert connection.to_dict()["options"]["model"] == "first"
    error("INVALID_REQUEST", ProviderAccount.from_dict, "account", {"provider": "google", "credential_ref": None,
                                                                    "principal_id": "must-not-merge-identities"})


def test_same_plan_replay_reuses_reservation_but_cannot_start_a_new_run(store):
    first = store.admit(plan(), pools(), legacy_run_id="run_one")
    again = store.admit(plan(), pools(), legacy_run_id="run_one")
    assert first == again
    error("BINDING_MISMATCH", store.admit, plan(), pools(), legacy_run_id="run_two")
    error("BINDING_MISMATCH", store.admit, plan("ppl_two"), pools(), legacy_run_id="run_one")
    error("NOT_FOUND", store.get_plan, "ppl_two", PRINCIPAL, NS)
    assert store.pool_totals("shared", NS, meter="requests", window_id="test-window")["held"] == 1


def test_control_reservation_is_bound_to_legacy_run_without_double_consumption(store):
    first = store.admit(plan(), pools(), control_action_id="act_one")
    bound = store.bind_legacy("act_one", "run_one", NS)
    assert bound["id"] == first["id"]
    assert bound["legacy_run_id"] == "run_one"
    assert store.bind_legacy("act_one", "run_one", NS) == bound
    assert store.binding_for_run("run_one", NS, PRINCIPAL) == store.binding_for_control("act_one", NS, PRINCIPAL)
    assert store.admit(plan(), pools(), control_action_id="act_one") == bound
    error("BINDING_MISMATCH", store.bind_legacy, "act_one", "run_two", NS)
    error("NOT_FOUND", store.binding_for_run, "run_one", NS, "other-user")
    assert len(bound["reservations"]) == 1


def test_outer_transaction_atomically_rolls_back_grant_outbox_plan_pool_and_session(store):
    control = ControlStore(store._conn)
    with pytest.raises(RuntimeError, match="fault after admission"):
        with control.transaction():
            control.put({"namespace_id": NS, "kind": "Grant", "id": "grt_one"})
            control.put({"namespace_id": NS, "kind": "Action", "id": "act_one"})
            control.enqueue("act_one", NS, {"parameters": "pinned"})
            store.admit(plan(), pools(), control_action_id="act_one")
            store.bind_session("cv_one", {"provider": "test-account"}, NS, principal=PRINCIPAL)
            raise RuntimeError("fault after admission")
    assert control.list("Grant", NS) == control.list("Action", NS) == []
    assert control.pending(NS) == []
    assert store.binding_for_control("act_one", NS) is None
    assert store.get_session_lock("cv_one", NS) is None
    error("NOT_FOUND", store.get_plan, "ppl_one", PRINCIPAL, NS)


def test_budget_failure_rolls_back_all_pools_and_leaves_connection_reusable(store):
    value = plan(amount=1)
    value["reservations"].append({"pool_ref": "small", "meter": "requests", "amount": 2, "window_id": "test-window"})
    settings = {**pools(), "small": pools(limit=1)["shared"]}
    error("BUDGET_EXHAUSTED", store.admit, value, settings, legacy_run_id="run_one")
    assert not store._conn.in_transaction
    assert store.pool_totals("shared", NS)["held"] == 0
    error("NOT_FOUND", store.get_plan, "ppl_one", PRINCIPAL, NS)
    assert admit(store)["status"] == "held"


def test_initializer_joins_existing_transaction_instead_of_committing():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        PluginStore(conn)
        conn.execute("ROLLBACK")
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='plugin_plans'").fetchone()
    finally:
        conn.close()


@pytest.mark.parametrize("missing", ["local_limit", "max_concurrency", "meter", "window_id"])
def test_unknown_balance_does_not_remove_required_local_limits(store, missing):
    settings = pools()
    del settings["shared"][missing]
    error("BUDGET_UNKNOWN", store.admit, plan(), settings, legacy_run_id="run_one")


def test_known_zero_balance_cannot_be_overridden_by_large_local_limit(store):
    error("BUDGET_EXHAUSTED", store.admit, plan(), pools(limit=100, remaining=0), legacy_run_id="run_one")
    first = store.admit(plan(), pools(remaining=1), legacy_run_id="run_one")
    error("BUDGET_EXHAUSTED", store.admit, plan("ppl_two", connection="second"), pools(remaining=1), legacy_run_id="run_two")
    store.settle(first["id"])
    error("BUDGET_EXHAUSTED", store.admit, plan("ppl_two"), pools(remaining=1), legacy_run_id="run_two")


def test_two_connections_share_pool_even_when_configuration_aliases_differ(store):
    store.admit(plan(amount=3), pools(limit=5), legacy_run_id="run_one")
    error("BUDGET_EXHAUSTED", store.admit, plan("ppl_two", amount=3, connection="other"), pools(limit=5), legacy_run_id="run_two")
    second = store.admit(plan("ppl_two", amount=2, connection="other"), pools(limit=5), legacy_run_id="run_two")
    assert second["status"] == "held"
    assert store.pool_totals("shared", NS, meter="requests", window_id="test-window")["used"] == 5


def test_concurrent_sqlite_connections_cannot_oversubscribe_shared_pool(tmp_path):
    path = tmp_path / "state.sqlite"
    with opened(path):
        pass
    barrier = threading.Barrier(6)
    def compete(number):
        with opened(path) as store:
            barrier.wait(timeout=10)
            try:
                return store.admit(plan(f"ppl_{number}", connection=f"connection-{number}"), pools(limit=3),
                                   legacy_run_id=f"run_{number}")["status"]
            except AsterunError as exc:
                return exc.code
    with ThreadPoolExecutor(max_workers=6) as workers:
        outcomes = list(workers.map(compete, range(6)))
    assert outcomes.count("held") == 3
    assert outcomes.count("BUDGET_EXHAUSTED") == 3
    with opened(path) as store:
        assert store.pool_totals("shared", NS)["used"] == 3
        validate_snapshot(store._conn)


def test_unknown_remote_survives_restart_and_window_refresh_keeps_concurrency(tmp_path):
    path = tmp_path / "state.sqlite"
    with opened(path) as store:
        first = store.admit(plan(), pools(concurrency=1), legacy_run_id="run_one")
        store.mark_uncertain(first["id"])
    with opened(path) as store:
        uncertain = store.binding_for_run("run_one", NS)
        assert uncertain["status"] == "uncertain"
        assert store.mark_uncertain(first["id"])["status"] == "uncertain"
        error("BUDGET_EXHAUSTED", store.admit, plan("ppl_new", window="next-window"),
              pools(concurrency=1, window="next-window"), legacy_run_id="run_new")
        totals = store.pool_totals("shared", NS, meter="requests", window_id="next-window")
        assert totals["used"] == 0 and totals["active"] == 1
        validate_snapshot(store._conn)


def test_unknown_actual_stays_null_after_completion_and_corrections_preserve_estimate(store):
    first = admit(store, amount=4)
    reservation = first["reservations"][0]
    evidence = {"source": "local_lifecycle", "billed_usage_verified": False}
    completed = store.settle(first["id"], evidence=evidence)
    assert completed["status"] == "committed"
    assert completed["reservations"][0]["actual"] is None
    assert completed["reservations"][0]["actual_source"] is None
    assert store.pool_totals("shared", NS)["used"] == 4
    store.settle(first["id"], evidence=evidence)
    assert len(store.reservation_history(reservation["id"], NS)) == 2
    corrected = store.settle(first["id"], actual={"shared": 12}, evidence=["evd_provider_bill"])
    assert corrected["reservations"][0]["reserved"] == 4
    assert corrected["reservations"][0]["actual"] == 12
    history = store.reservation_history(reservation["id"], NS)
    assert [item["actual"] for item in history] == [None, None, 12]
    assert history[-1]["kind"] == "correction"
    error("BUDGET_EXHAUSTED", store.admit, plan("ppl_next"), pools(), legacy_run_id="run_next")
    validate_snapshot(store._conn)


def test_proven_not_applied_can_correct_estimate_without_rewriting_history(store):
    first = admit(store)
    store.settle(first["id"], evidence={"source": "local_lifecycle"})
    error("INVALID_STATE", store.settle, first["id"], not_applied=True)
    released = store.settle(first["id"], not_applied=True, evidence={"source": "control_lifecycle", "turns": 0})
    assert released["status"] == "released"
    assert released["reservations"][0]["actual"] == 0
    assert store.settle(first["id"])["status"] == "released"
    assert store.settle(first["id"], not_applied=True)["status"] == "released"
    assert len(store.reservation_history(first["reservations"][0]["id"], NS)) == 3
    assert store.pool_totals("shared", NS)["used"] == 0
    error("INVALID_STATE", store.settle, first["id"], actual={"shared": 1})
    validate_snapshot(store._conn)


def test_known_billed_usage_cannot_be_erased_as_not_applied(store):
    first = admit(store)
    store.settle(first["id"], actual={"shared": 2}, source_kind="billed")
    error("INVALID_STATE", store.settle, first["id"], not_applied=True, evidence={"claim": "nothing happened"})
    assert store.binding_for_run("run_one", NS)["reservations"][0]["actual"] == 2
    error("INVALID_STATE", store.mark_uncertain, first["id"])


def test_observed_usage_and_equal_final_bill_keep_distinct_history(store):
    first = admit(store)
    observed = store.settle(first["id"], actual={"shared": 2}, evidence=["evd_observed"])
    assert observed["reservations"][0]["actual_source"] == "provider_observed"
    billed = store.settle(first["id"], actual={"shared": 2}, source_kind="billed", evidence=["evd_billed"])
    assert billed["reservations"][0]["actual_source"] == "billed"
    history = store.reservation_history(first["reservations"][0]["id"], NS)
    assert [item["actual_source"] for item in history] == [None, "provider_observed", "billed"]
    error("INVALID_STATE", store.settle, first["id"], actual={"shared": 1})
    validate_snapshot(store._conn)


def test_zero_estimate_cannot_allow_unbounded_requests(store):
    error("BUDGET_UNKNOWN", store.admit, plan(amount=0), pools(), legacy_run_id="run_one")
    error("NOT_FOUND", store.get_plan, "ppl_one", PRINCIPAL, NS)


def test_pool_report_refuses_to_add_different_meter_units(store):
    first = admit(store)
    store.settle(first["id"])
    settings = pools(window="money-window")
    settings["shared"]["meter"] = "micro_usd"
    store.admit(plan("ppl_money", meter="micro_usd", window="money-window"), settings, legacy_run_id="run_money")
    error("INVALID_REQUEST", store.pool_totals, "shared", NS)
    assert store.pool_totals("shared", NS, meter="requests")["used"] == 1
    assert store.pool_totals("shared", NS, meter="micro_usd")["used"] == 1


def test_invalid_settlement_cannot_add_pool_or_partially_adjust_existing_reservation(store):
    first = admit(store)
    for actual in ({"other": 2}, {"shared": -1}, {"shared": True}, {"shared": 1.5}):
        error("INVALID_REQUEST", store.settle, first["id"], actual=actual)
    assert store.binding_for_run("run_one", NS)["status"] == "held"
    assert len(store.reservation_history(first["reservations"][0]["id"], NS)) == 1


def test_native_session_account_lock_is_immutable_and_principal_scoped(store):
    lock = {"provider_account_ref": "test-account", "model": "model-a", "credential_revision": 1,
            "connection_ref": "connection-a", "billing_pool_refs": ["shared"]}
    first = store.bind_session("cv_one", lock, NS, principal=PRINCIPAL)
    lock["model"] = "model-b"
    assert store.get_session_lock("cv_one", NS, PRINCIPAL) == first
    error("BINDING_MISMATCH", store.bind_session, "cv_one", lock, NS, principal=PRINCIPAL)
    error("BINDING_MISMATCH", store.bind_session, "cv_one", first["lock"], NS, principal="other-user")
    error("NOT_FOUND", store.get_session_lock, "cv_one", NS, "other-user")
    assert store.get_session_lock("cv_missing", NS) is None
    assert store.bind_session("cv_one", first["lock"], NS, principal=PRINCIPAL) == first
    validate_snapshot(store._conn)


def test_cumulative_observations_deduplicate_diff_reset_and_preserve_unknowns(store):
    first = store.record_observation(observation(), NS)
    assert first["delta"] is None and first["derivation"] == "baseline"
    assert store.record_observation(observation(), NS) == first
    error("IDEMPOTENCY_CONFLICT", store.record_observation, observation(used=11), NS)
    assert store.record_observation(observation("two", 15, sequence=2), NS)["delta"] == 5
    reset = store.record_observation(observation("three", 2, sequence=3), NS)
    assert reset["delta"] is None and reset["derivation"] == "counter_reset"
    assert store.record_observation(observation("four", 5, sequence=4), NS)["delta"] == 3
    unknown = store.record_observation(observation("unknown", None, sequence=5), NS)
    assert unknown["delta"] is None and not unknown["baseline_advanced"]
    assert store.record_observation(observation("six", 6, sequence=6), NS)["delta"] == 1
    assert sum(item["delta"] or 0 for item in store.observations("shared", NS)) == 9
    validate_snapshot(store._conn)


def test_late_cumulative_event_cannot_reset_baseline_or_double_count(store):
    store.record_observation(observation("one", 10, sequence=10), NS)
    stale = store.record_observation(observation("older", 2, sequence=2), NS)
    assert stale["derivation"] == "out_of_order" and stale["delta"] is None
    assert store.record_observation(observation("eleven", 15, sequence=11), NS)["delta"] == 5
    validate_snapshot(store._conn)


def test_observation_windows_sessions_and_sources_do_not_share_cumulative_baseline(store):
    store.record_observation(observation(), NS)
    for newer in (observation(window="next-window"), observation(source="billed"),
                  observation(scope_ref="other-session")):
        assert store.record_observation(newer, NS)["derivation"] == "baseline"
    request = store.record_observation(observation(scope="request", used=3), NS)
    assert request["delta"] == 3 and request["derivation"] == "reported"
    assert store.record_observation(observation(), "ns_other")["derivation"] == "baseline"
    validate_snapshot(store._conn)


def test_cumulative_baseline_survives_database_backup_and_reopen(tmp_path):
    path, backup = tmp_path / "state.sqlite", tmp_path / "snapshot.sqlite"
    with opened(path) as store:
        store.record_observation(observation(), NS)
        target = sqlite3.connect(backup)
        try:
            store._conn.backup(target)
        finally:
            target.close()
    with opened(backup) as store:
        assert store.record_observation(observation("next", 14, sequence=2), NS)["delta"] == 4
        validate_snapshot(store._conn)


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(used=1.5),
    lambda value: value.update(used=True),
    lambda value: value.update(meter="CCU"),
    lambda value: value.update(unit="USD"),
    lambda value: value.update(scope_ref=None),
    lambda value: value.update(remaining=-1),
    lambda value: value.update(valid_until="2026-09-10T00:00:00Z"),
    lambda value: value.update(cumulative=False),
])
def test_usage_observation_rejects_ambiguous_or_invalid_units_and_unknowns(mutation):
    value = observation()
    mutation(value)
    error("INVALID_REQUEST", UsageObservation.from_dict, value)


@pytest.mark.parametrize("table,mutate", [
    ("plugin_plans", lambda value: value["binding"].update(model="tampered")),
    ("plugin_execution_bindings", lambda value: value.update(plan_id="ppl_missing")),
    ("plugin_pool_reservations", lambda value: value.update(reserved=99)),
    ("plugin_reservation_changes", lambda value: value.update(status="released")),
    ("plugin_usage_observations", lambda value: value.update(delta=99)),
    ("plugin_session_locks", lambda value: value.update(model="tampered")),
])
def test_snapshot_validation_rejects_corrupted_plan_reservation_history_or_session(store, table, mutate):
    admit(store)
    store.record_observation(observation(), NS)
    store.bind_session("cv_one", {"model": "original"}, NS, principal=PRINCIPAL)
    validate_snapshot(store._conn)
    value = json.loads(store._conn.execute(f"SELECT payload FROM {table}").fetchone()[0])
    mutate(value)
    store._conn.execute(f"UPDATE {table} SET payload=?", (json.dumps(value),))
    error("INVALID_REQUEST", validate_snapshot, store._conn)


def test_snapshot_requires_ledger_history_and_parent_references_when_core_tables_exist(store):
    first = admit(store)
    store._conn.execute("CREATE TABLE runs (id TEXT PRIMARY KEY,payload TEXT)")
    error("INVALID_REQUEST", validate_snapshot, store._conn)
    store._conn.execute("INSERT INTO runs VALUES('run_one','{}')")
    validate_snapshot(store._conn)
    store._conn.execute("DELETE FROM plugin_reservation_changes")
    error("INVALID_REQUEST", validate_snapshot, store._conn)
    assert first["reservations"]


def test_schema_export_matches_backup_contract(store):
    for table, columns in TABLE_COLUMNS.items():
        assert {row[1] for row in store._conn.execute(f"PRAGMA table_info({table})")} == columns
