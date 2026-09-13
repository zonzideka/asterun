from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "integrations/grokbot/scripts/bridge.py"
spec = importlib.util.spec_from_file_location("grokbot_inbox_fairness", SCRIPT)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


@pytest.fixture
def ledger(tmp_path):
    state = bridge.Ledger(tmp_path.resolve() / "private" / "ledger.sqlite")
    state.conn.execute(
        "INSERT INTO bindings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("primary", "/fixture/asterun", "/fixture/config", "/fixture/state", "local",
         '["demo"]', '["fake"]', "fixture-chat", "fixture-fingerprint", bridge.utc_now()),
    )
    yield state
    state.close()


def add_delivery(ledger, number, state, created_at):
    key = f"{number:064x}"
    content = f"fixture result {number}"
    ledger.conn.execute(
        "INSERT INTO deliveries(delivery_key, binding, destination, task_id, run_id, "
        "status, delivery_state, content, content_sha256, summary, evidence, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (key, "primary", "fixture-chat", f"fixture-task-{number}", f"fixture-run-{number}",
         "succeeded", state, content, bridge.sha256_text(content), content, "{}", created_at, created_at),
    )
    return key


def test_new_pending_delivery_precedes_three_unconfirmed_sends(ledger):
    sending = [add_delivery(ledger, number, "sending", f"2026-01-01T00:00:0{number}Z")
               for number in range(1, 4)]
    pending = add_delivery(ledger, 4, "pending", "2026-01-01T00:00:04Z")

    result = bridge.cmd_inbox(ledger, argparse.Namespace(binding="primary", limit=3), 30)

    assert result["count"] == 3
    assert [item["delivery_key"] for item in result["items"]] == [pending, *sending[:2]]
    assert [item["delivery_state"] for item in result["items"]] == ["pending", "sending", "sending"]
    all_items = bridge.cmd_inbox(ledger, argparse.Namespace(binding="primary", limit=4), 30)
    assert [item["delivery_key"] for item in all_items["items"]] == [pending, *sending]
    status = bridge.cmd_status(ledger, argparse.Namespace(binding="primary"), 30)
    assert status["deliveries"] == {"pending": 1, "sending": 3, "host_reported_delivered": 0}
    assert status["unconfirmed"] == 3
    assert status["needs_followup"] is True


def test_inbox_order_is_stable_with_equal_timestamps(ledger):
    created_at = "2026-01-01T00:00:00Z"
    pending_later = add_delivery(ledger, 3, "pending", created_at)
    sending_later = add_delivery(ledger, 4, "sending", created_at)
    pending_first = add_delivery(ledger, 1, "pending", created_at)
    sending_first = add_delivery(ledger, 2, "sending", created_at)

    result = bridge.cmd_inbox(ledger, argparse.Namespace(binding="primary", limit=4), 30)

    assert [item["delivery_key"] for item in result["items"]] == [
        pending_first, pending_later, sending_first, sending_later,
    ]
    limited = bridge.cmd_inbox(ledger, argparse.Namespace(binding="primary", limit=2), 30)
    assert limited["count"] == 2
    assert [item["delivery_key"] for item in limited["items"]] == [pending_first, pending_later]
