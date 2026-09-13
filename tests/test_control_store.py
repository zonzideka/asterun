from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

import pytest

from asterun.control_store import ControlStore, SCHEMA, TABLE_COLUMNS
from asterun.errors import AsterunError


NS = "nsp_test"
NOW = "2026-09-10T00:00:00Z"
LATER = "2026-09-10T00:10:00Z"


def entity(kind, id, namespace=NS, **values):
    return {
        "schema_version": "runner-control/v1", "kind": kind, "id": id,
        "namespace_id": namespace, "revision": 1, "created_at": NOW,
        "updated_at": NOW, "extensions": {}, **values,
    }


def operation(id="opr_test", namespace=NS):
    return entity("Operation", id, namespace, method="run.start", status="ACCEPTED")


def event(id="evt_test", namespace=NS, task_id="tsk_test"):
    return entity("Event", id, namespace, task_id=task_id, event_type="task.created")


def meter(amount, pool=None, name="turns"):
    return {"meter": name, "amount": amount, "billing_pool_ref": pool}


def budget(limit=10, namespace=NS):
    return entity("Budget", "bud_test", namespace, task_id="tsk_test", limits=[{
        "meter": "turns", "limit": limit, "billing_pool_ref": None, "enforcement": "hard",
    }])


def reservation(id="res_test", action="act_test", amount=4, namespace=NS):
    return entity("Reservation", id, namespace, task_id="tsk_test", budget_id="bud_test",
                  action_id=action, status="HELD", reserved=[meter(amount)], actual=None,
                  receipt_evidence_ids=[])


@contextmanager
def opened(path=":memory:"):
    conn = sqlite3.connect(path, isolation_level=None, timeout=10)
    try:
        yield ControlStore(conn)
    finally:
        conn.close()


@pytest.fixture
def store():
    with opened() as value:
        yield value


def error(code, function, *args, **kwargs):
    with pytest.raises(AsterunError) as exc:
        function(*args, **kwargs)
    assert exc.value.code == code


def test_entity_namespace_isolation_and_no_shared_mutable_value(store):
    value = entity("Task", "tsk_test")
    store.put(value)
    value["extensions"]["test.mutable"] = True
    assert store.get("Task", "tsk_test", NS)["extensions"] == {}
    error("NOT_FOUND", store.get, "Task", "tsk_test", "nsp_other")
    assert store.list("Task", "nsp_other") == []
    assert len(store.list("Task", NS)) == 1


def test_compound_acceptance_rolls_back_every_ledger(store):
    store.put(budget())
    with pytest.raises(RuntimeError, match="fault before commit"):
        with store.transaction():
            store.save_operation(operation(), "pri_test", "request-key-0001", "a" * 64)
            store.put(entity("Action", "act_test"))
            store.reserve(reservation())
            store.enqueue("act_test", NS, {"run_id": "run_test"})
            store.append_event(event())
            store.claim_lease(NS, "run_test", "wrk_test", LATER, now=NOW)
            store.put_blob(NS, b"temporary artifact")
            raise RuntimeError("fault before commit")
    assert store.find_operation(NS, "pri_test", "run.start", "request-key-0001") is None
    assert store.list("Action", NS) == []
    assert store.list("Reservation", NS) == []
    assert store.events(NS, "tsk_test") == []
    assert store.pending(NS) == []
    assert store.budget_totals(NS, "bud_test") == []
    assert store._conn.execute("SELECT count(*) FROM control_blobs").fetchone()[0] == 0
    error("LEASE_LOST", store.check_lease, NS, "run_test", "wrk_test", 1, now=NOW)


def test_nested_failure_rolls_back_savepoint_without_committing_parent(store):
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.put(entity("Task", "tsk_outer"))
            with pytest.raises(ValueError):
                with store.transaction():
                    store.put(entity("Task", "tsk_inner"))
                    raise ValueError("inner")
            assert store.get("Task", "tsk_outer", NS)
            error("NOT_FOUND", store.get, "Task", "tsk_inner", NS)
            raise RuntimeError("outer")
    assert store.list("Task", NS) == []


def test_initializer_does_not_commit_callers_transaction():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        ControlStore(conn)
        conn.execute("ROLLBACK")
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='control_entities'").fetchone() is None
    finally:
        conn.close()


def test_operation_duplicate_returns_original_and_preserves_progress(store):
    original = store.save_operation(operation(), "pri_test", "request-key-0001", "a" * 64)
    original["status"] = "SUCCEEDED"
    store.put(original)
    retried = store.save_operation(operation("opr_second"), "pri_test", "request-key-0001", "a" * 64)
    assert retried["id"] == "opr_test"
    assert retried["status"] == "SUCCEEDED"
    assert len(store.list("Operation", NS)) == 1
    error("IDEMPOTENCY_CONFLICT", store.save_operation, operation("opr_third"), "pri_test", "request-key-0001", "b" * 64)


def test_operation_id_cannot_be_rebound_to_another_key(store):
    store.save_operation(operation(), "pri_test", "request-key-0001", "a" * 64)
    error("IDEMPOTENCY_CONFLICT", store.save_operation, operation(), "pri_test", "request-key-0002", "a" * 64)
    assert store.find_operation(NS, "pri_test", "run.start", "request-key-0002") is None


def test_idempotency_scope_includes_principal_method_and_namespace(store):
    store.save_operation(operation(), "pri_test", "request-key-0001", "a" * 64)
    store.save_operation(operation("opr_other"), "pri_other", "request-key-0001", "b" * 64)
    store.save_operation(operation(namespace="nsp_other"), "pri_test", "request-key-0001", "c" * 64)
    other_method = operation("opr_create")
    other_method["method"] = "task.create"
    store.save_operation(other_method, "pri_test", "request-key-0001", "d" * 64)
    assert len(store.list("Operation", NS)) == 3


def test_event_sequences_are_per_task_and_namespace_and_append_only(store):
    assert store.append_event(event())["sequence"] == 1
    assert store.append_event(event("evt_second"))["sequence"] == 2
    assert store.append_event(event("evt_other", task_id="tsk_other"))["sequence"] == 1
    assert store.append_event(event(namespace="nsp_other"))["sequence"] == 1
    assert [row["id"] for row in store.events(NS, "tsk_test", limit=1)] == ["evt_test"]
    assert [row["id"] for row in store.events(NS, "tsk_test", after=1)] == ["evt_second"]
    error("IDEMPOTENCY_CONFLICT", store.append_event, event())
    error("INVALID_REQUEST", store.put, event())
    assert store.get("Event", "evt_test", NS)["sequence"] == 1


def test_retention_floor_survives_reopen_without_sequence_reuse(tmp_path):
    path = tmp_path / "control.sqlite"
    with opened(path) as store:
        for n in range(1, 4):
            store.append_event(event(f"evt_{n:03}"))
        store.prune_events(NS, "tsk_test", through=2)
    with opened(path) as store:
        error("CURSOR_EXPIRED", store.events, NS, "tsk_test", after=0)
        error("CURSOR_EXPIRED", store.events, NS, "tsk_test", after=1)
        error("INVALID_REQUEST", store.events, NS, "tsk_test", after=4)
        assert [e["sequence"] for e in store.events(NS, "tsk_test", after=2)] == [3]
        assert store.events(NS, "tsk_test", after=3) == []
        store.prune_events(NS, "tsk_test", through=3)
        assert store.append_event(event("evt_fourth"))["sequence"] == 4
        assert [e["sequence"] for e in store.events(NS, "tsk_test", after=3)] == [4]
        error("IDEMPOTENCY_CONFLICT", store.append_event, event("evt_001"))


@pytest.mark.parametrize("after,limit", [(True, 1), (-1, 1), (0, 0), (0, True), (0, 10001), (2**53, 1)])
def test_event_pagination_rejects_invalid_integers(store, after, limit):
    error("INVALID_REQUEST", store.events, NS, "tsk_test", after=after, limit=limit)


def test_outbox_delivery_uncertainty_survives_reopen(tmp_path):
    path = tmp_path / "control.sqlite"
    with opened(path) as store:
        store.enqueue("act_test", NS, {"payload": "bound"})
        store.mark_delivery("act_test", NS)
    with opened(path) as store:
        assert store.pending(NS) == []
        assert store.uncertain_deliveries(NS)[0]["action_id"] == "act_test"
        error("UNKNOWN_REMOTE", store.mark_delivery, "act_test", NS)
        assert store.enqueue("act_test", NS, {"payload": "bound"})["status"] == "DELIVERING"
        error("IDEMPOTENCY_CONFLICT", store.enqueue, "act_test", NS, {"payload": "changed"})
        store.finish_delivery("act_test", NS)
        store.finish_delivery("act_test", NS)
        assert store.uncertain_deliveries(NS) == []
        error("INVALID_STATE", store.mark_delivery, "act_test", NS)


def test_outbox_namespace_must_be_unambiguous(store):
    store.enqueue("act_test", NS, {})
    store.enqueue("act_test", "nsp_other", {})
    error("INVALID_REQUEST", store.mark_delivery, "act_test")
    store.mark_delivery("act_test", NS)
    assert len(store.pending("nsp_other")) == 1


def test_lease_epochs_reject_old_workers_after_restart_and_release(tmp_path):
    path = tmp_path / "control.sqlite"
    with opened(path) as store:
        first = store.claim_lease(NS, "run_test", "wrk_first", LATER, now=NOW)
        assert first["fencing_token"] == 1
        error("LEASE_CONFLICT", store.claim_lease, NS, "run_test", "wrk_second", LATER, now=NOW)
    with opened(path) as store:
        expired = "2026-09-10T00:10:01Z"
        renewed = store.claim_lease(NS, "run_test", "wrk_second", "2026-09-10T00:20:00Z", now=expired)
        assert renewed["fencing_token"] == 2
        error("LEASE_LOST", store.check_lease, NS, "run_test", "wrk_first", 1, now=NOW)
        error("LEASE_LOST", store.release_lease, NS, "run_test", "wrk_first", 1)
        assert store.check_lease(NS, "run_test", "wrk_second", 2, now=expired) == renewed
        store.release_lease(NS, "run_test", "wrk_second", 2)
        error("LEASE_LOST", store.check_lease, NS, "run_test", "wrk_second", 2, now=expired)
        assert store.claim_lease(NS, "run_test", "wrk_third", "2026-09-10T00:20:00Z", now=expired)["fencing_token"] == 3


def test_lease_check_in_transaction_blocks_stale_authoritative_write(store):
    store.claim_lease(NS, "run_test", "wrk_test", LATER, now=NOW)
    store.claim_lease(NS, "run_test", "wrk_test", LATER, now=NOW)
    with pytest.raises(AsterunError, match="fencing"):
        with store.transaction():
            store.put(entity("Task", "tsk_new"))
            store.check_lease(NS, "run_test", "wrk_test", 1, now=NOW)
    assert store.list("Task", NS) == []


def test_budget_settlement_once_releases_difference_and_keeps_unknown_held(store):
    store.put(budget())
    store.reserve(reservation(amount=6))
    store.mark_uncertain(NS, "res_test")
    store.mark_uncertain(NS, "res_test")
    error("BUDGET_EXHAUSTED", store.reserve, reservation("res_other", "act_other", 5))
    settled = store.settle(NS, "res_test", [meter(2)], ["evd_receipt"])
    assert settled["status"] == "SETTLED"
    assert store.settle(NS, "res_test", [meter(2)]) == settled
    error("IDEMPOTENCY_CONFLICT", store.settle, NS, "res_test", [meter(3)])
    store.reserve(reservation("res_other", "act_other", 8))
    assert store.budget_totals(NS, "bud_test") == [{"meter": "turns", "billing_pool_ref": None, "settled": 2, "held": 8}]
    assert store.reserve(reservation(amount=6))["status"] == "SETTLED"


def test_reservation_identity_and_action_retries_never_double_count(store):
    store.put(budget())
    first = store.reserve(reservation())
    assert store.reserve(reservation("res_other")) == first
    error("IDEMPOTENCY_CONFLICT", store.reserve, reservation(amount=3))
    error("IDEMPOTENCY_CONFLICT", store.reserve, reservation(action="act_other"))
    assert store.budget_totals(NS, "bud_test")[0]["held"] == 4


def test_reservation_id_and_action_cannot_collide_with_two_records(store):
    store.put(budget())
    store.reserve(reservation("res_first", "act_first", 2))
    store.reserve(reservation("res_second", "act_second", 2))
    error("IDEMPOTENCY_CONFLICT", store.reserve, reservation("res_first", "act_second", 2))
    assert store.budget_totals(NS, "bud_test")[0]["held"] == 4


def test_budget_meters_pools_and_tasks_do_not_mix(store):
    value = budget()
    value["limits"].extend([
        {"meter": "turns", "limit": 3, "billing_pool_ref": "pol_other", "enforcement": "hard"},
        {"meter": "tokens", "limit": 100, "billing_pool_ref": None, "enforcement": "admission_only"},
    ])
    store.put(value)
    request = reservation()
    request["reserved"] = [meter(8), meter(3, "pol_other"), meter(100, name="tokens")]
    store.reserve(request)
    other = reservation("res_second", "act_second", 2)
    store.reserve(other)
    third = reservation("res_third", "act_third", 1)
    third["reserved"] = [meter(1, "pol_other")]
    error("BUDGET_EXHAUSTED", store.reserve, third)
    third["reserved"] = [meter(1, "pol_unknown")]
    error("BUDGET_UNKNOWN", store.reserve, third)
    third["task_id"] = "tsk_other"
    error("BINDING_MISMATCH", store.reserve, third)


def test_budget_rejects_duplicate_and_unsafe_amounts(store):
    store.put(budget())
    for amounts in ([meter(1), meter(1)], [meter(True)], [meter(-1)], [meter(2**53)]):
        value = reservation()
        value["reserved"] = amounts
        error("INVALID_REQUEST", store.reserve, value)
    store.reserve(reservation())
    error("INVALID_REQUEST", store.settle, NS, "res_test", [meter(1, "pol_other")])


def test_actual_overrun_is_recorded_and_blocks_new_admission(store):
    # A provider can exceed an admission-only estimate; preserve observed cost.
    value = budget(10)
    value["limits"][0]["enforcement"] = "admission_only"
    store.put(value)
    store.reserve(reservation(amount=2))
    store.settle(NS, "res_test", [meter(12)])
    assert store.budget_totals(NS, "bud_test")[0]["settled"] == 12
    error("BUDGET_EXHAUSTED", store.reserve, reservation("res_next", "act_next", 1))


def test_blob_is_immutable_namespaced_and_in_database_backup(tmp_path):
    path, backup = tmp_path / "state.sqlite", tmp_path / "backup.sqlite"
    with opened(path) as store:
        digest = store.put_blob(NS, b"\x00artifact\xff")
        assert store.put_blob(NS, b"\x00artifact\xff") == digest
        assert store.get_blob(NS, digest) == b"\x00artifact\xff"
        error("NOT_FOUND", store.get_blob, "nsp_other", digest)
        target = sqlite3.connect(backup)
        try:
            store._conn.backup(target)
        finally:
            target.close()
    with opened(backup) as store:
        assert store.get_blob(NS, digest) == b"\x00artifact\xff"
        store._conn.execute("UPDATE control_blobs SET content=?", (b"corrupted",))
        error("INVALID_STATE", store.get_blob, NS, digest)


def test_schema_export_matches_backup_column_contract(store):
    assert "CREATE TABLE" in SCHEMA
    for table, columns in TABLE_COLUMNS.items():
        actual = {row[1] for row in store._conn.execute(f"PRAGMA table_info({table})")}
        assert actual == columns


def test_competing_connections_cannot_oversubscribe_budget(tmp_path):
    path = tmp_path / "control.sqlite"
    with opened(path) as store:
        store.put(budget(5))
    barrier = threading.Barrier(6)

    def compete(number):
        with opened(path) as store:
            barrier.wait(timeout=10)
            try:
                store.reserve(reservation(f"res_{number:03}", f"act_{number:03}", 1))
                return "HELD"
            except AsterunError as exc:
                return exc.code

    with ThreadPoolExecutor(max_workers=6) as workers:
        outcomes = list(workers.map(compete, range(6)))
    assert outcomes.count("HELD") == 5
    assert outcomes.count("BUDGET_EXHAUSTED") == 1
    with opened(path) as store:
        assert store.budget_totals(NS, "bud_test")[0]["held"] == 5


def test_competing_operation_admission_returns_one_persistent_operation(tmp_path):
    path = tmp_path / "control.sqlite"
    with opened(path):
        pass
    barrier = threading.Barrier(4)

    def compete(number):
        with opened(path) as store:
            barrier.wait(timeout=10)
            return store.save_operation(operation(f"opr_{number:03}"), "pri_test", "request-key-0001", "a" * 64)["id"]

    with ThreadPoolExecutor(max_workers=4) as workers:
        outcomes = list(workers.map(compete, range(4)))
    assert len(set(outcomes)) == 1
    with opened(path) as store:
        assert len(store.list("Operation", NS)) == 1


@pytest.mark.parametrize("after_delivery", [False, True])
def test_process_death_preserves_only_committed_intent_and_never_replays_delivery(tmp_path, after_delivery):
    path = tmp_path / "control.sqlite"
    script = """
import os, sqlite3, sys
from asterun.control_store import ControlStore
conn = sqlite3.connect(sys.argv[1], isolation_level=None)
store = ControlStore(conn)
if sys.argv[2] == 'True':
    store.enqueue('act_test', 'nsp_test', {'intent': 'durable'})
    store.mark_delivery('act_test', 'nsp_test')
else:
    with store.transaction():
        store.enqueue('act_test', 'nsp_test', {'intent': 'uncommitted'})
        os._exit(93)
os._exit(93)
"""
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    result = subprocess.run([sys.executable, "-c", script, str(path), str(after_delivery)], env=env, capture_output=True, timeout=20)
    assert result.returncode == 93, result.stderr.decode()
    with opened(path) as store:
        assert store.pending(NS) == []
        assert len(store.uncertain_deliveries(NS)) == int(after_delivery)
