from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from asterun.backup import DB_NAME, _validate_snapshot, backup_state, restore_state
from asterun.errors import AsterunError
from asterun.plugin_store import PluginStore, validate_snapshot
from asterun.quota.models import (
    canonical_utc, freshness, merge_observations, normalize_observation, normalize_probe_state,
    used_bp_from_percent,
)
from asterun.sqlite_store import SqliteStore


def window(**overrides):
    value = {
        "scope_ref": "backend:codex",
        "bucket_id": "weekly",
        "window_kind": "weekly",
        "source_kind": "manual",
        "confidence": "low",
        "paid_overage": False,
        "observed_at": "2026-10-01T00:00:00Z",
        "valid_until": "2026-10-01T12:00:00Z",
        "used_bp": 4800,
        "resets_at": "2026-10-08T00:00:00Z",
    }
    value.update(overrides)
    return value


def at(moment: datetime) -> str:
    return canonical_utc(moment)


def reject(fn, *args, **kwargs):
    with pytest.raises(AsterunError) as caught:
        fn(*args, **kwargs)
    assert caught.value.code == "INVALID_REQUEST"
    return caught.value


@contextmanager
def quota_store():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        yield PluginStore(conn)
    finally:
        conn.close()


def test_normalize_observation_canonicalizes_and_is_idempotent():
    raw = window(
        observed_at="2026-10-01T00:00:00+00:00",
        valid_until="2026-10-01T12:00:00+00:00",
        resets_at="2026-10-08T01:02:03+00:00",
        expiring_grants=[{"count": 2, "expires_at": "2026-10-23T05:02:00+00:00", "kind": "reset"}],
        raw_digest="ab" * 32,
    )
    got = normalize_observation(raw)
    assert got["observed_at"] == "2026-10-01T00:00:00Z"
    assert got["valid_until"] == "2026-10-01T12:00:00Z"
    assert got["resets_at"] == "2026-10-08T01:02:03Z"
    assert got["window_epoch"] == "2026-10-08T01:02:00Z"
    assert got["schema_version"] == 1
    assert got["native_time"] is None
    assert got["exhausted_hint"] is None
    assert got["expiring_grants"] == [{"count": 2, "expires_at": "2026-10-23T05:02:00Z", "kind": "reset"}]
    assert got["raw_digest"] == "ab" * 32
    assert normalize_observation(got) == got
    omitted = window()
    del omitted["used_bp"]
    assert normalize_observation(omitted)["used_bp"] is None
    assert normalize_observation(window(used_bp=None))["used_bp"] is None
    assert normalize_observation(window(used_bp=0))["used_bp"] == 0
    assert normalize_observation(window(used_bp=10000))["used_bp"] == 10000


@pytest.mark.parametrize("mutate", [
    lambda value: value.update(used_bp=-1),
    lambda value: value.update(used_bp=10001),
    lambda value: value.update(used_bp=True),
    lambda value: value.update(used_bp=1.5),
    lambda value: value.update(used_bp="4800"),
    lambda value: value.update(observed_at="2026-10-01T00:00:00"),
    lambda value: value.update(observed_at="2026-10-01T08:00:00+08:00"),
    lambda value: value.update(valid_until="2026-10-01T00:00:00Z"),
    lambda value: value.update(valid_until="2026-10-09T00:00:00Z"),
    lambda value: value.update(window_epoch="2026-10-08T00:01:00Z"),
    lambda value: value.update(window_kind="daily"),
    lambda value: value.update(source_kind="probe_"),
    lambda value: value.update(source_kind="user"),
    lambda value: value.update(confidence="certain"),
    lambda value: value.update(paid_overage=1),
    lambda value: value.update(schema_version=2),
    lambda value: value.update(raw_digest="abc"),
    lambda value: value.update(remaining_units=1),
    lambda value: value.__delitem__("valid_until"),
    lambda value: value.update(email="user@example.com"),
    lambda value: value.update(token="secret"),
    lambda value: value.update(balance=12),
    lambda value: value.update(access_token="secret"),
    lambda value: value.update(note="user@host"),
    lambda value: value.update(scope_ref="user@host"),
    lambda value: value.update(expiring_grants=[{"count": 1, "expires_at": "2026-10-02T00:00:00Z", "kind": "reset", "id": "x"}]),
])
def test_normalize_observation_rejects_invalid_unknown_and_sensitive_values(mutate):
    value = window()
    mutate(value)
    error = reject(normalize_observation, value)
    if "remaining_units" in value:
        assert error.details["unknown"] == ["remaining_units"]
    if "valid_until" not in value:
        assert "valid_until" in error.details["missing"]


def test_normalize_observation_rejects_manual_constraints():
    reject(normalize_observation, window(confidence="high"))
    reject(normalize_observation, window(paid_overage=True))
    reject(normalize_observation, window(window_kind="latch"))
    reject(normalize_observation, window(
        observed_at="2026-10-01T00:00:00Z",
        valid_until="2026-10-02T00:00:01Z",
        resets_at="2026-10-09T00:00:00Z",
    ))
    allowed = normalize_observation(window(
        observed_at="2026-10-01T00:00:00Z",
        valid_until="2026-10-02T00:00:00Z",
        resets_at="2026-10-09T00:00:00Z",
    ))
    assert allowed["source_kind"] == "manual"
    assert allowed["confidence"] == "low"
    assert allowed["paid_overage"] is False
    assert allowed["valid_until"] == "2026-10-02T00:00:00Z"


def test_window_epoch_must_be_absent_without_resets_at():
    reject(normalize_observation, window(resets_at=None, window_epoch="2026-10-08T00:00:00Z"))
    got = normalize_observation(window(resets_at=None))
    assert got["window_epoch"] is None
    assert got["resets_at"] is None


@pytest.mark.parametrize(("text", "bp"), [
    ("0", 0),
    ("0.01", 1),
    ("0.10", 10),
    ("9.9", 990),
    ("48", 4800),
    ("48.5", 4850),
    ("48.50", 4850),
    ("99.99", 9999),
    ("100", 10000),
    ("100.0", 10000),
    ("100.00", 10000),
])
def test_used_percent_converts_decimal_strings_to_basis_points(text, bp):
    assert used_bp_from_percent(text) == bp


@pytest.mark.parametrize("text", ["", " ", "100.01", "100.001", "101", "-1", "48.555", "1e2", "+10", "10%", None, 48.5, 48, True])
def test_used_percent_rejects_non_decimal_strings(text):
    reject(used_bp_from_percent, text)


def test_freshness_branches():
    now = datetime(2026, 10, 1, 1, 0, tzinfo=timezone.utc)
    fresh = window(valid_until="2026-10-01T06:00:00Z", used_bp=4800)
    cases = {
        "absent": (None, {"state": "absent", "issues": [], "age_s": None}),
        "not-object": ("nope", {"state": "unknown", "issues": [], "age_s": None}),
        "bad-time": ({"observed_at": "yesterday"}, {"state": "unknown", "issues": [], "age_s": None}),
        "fresh": (fresh, {"state": "fresh", "issues": [], "age_s": 3600}),
        "fresh-without-reset": (window(resets_at=None, valid_until="2026-10-01T06:00:00Z"), {"state": "fresh", "issues": [], "age_s": 3600}),
        "stale": (window(valid_until="2026-10-01T00:30:00Z"), {"state": "stale", "issues": [], "age_s": 3600}),
        "stale-at-valid-until": (window(valid_until="2026-10-01T01:00:00Z", resets_at="2026-10-08T00:00:00Z"), {"state": "stale", "issues": [], "age_s": 3600}),
        "exhausted-used": (window(valid_until="2026-10-01T06:00:00Z", used_bp=10000), {"state": "exhausted", "issues": [], "age_s": 3600}),
        "exhausted-hint": (window(valid_until="2026-10-01T06:00:00Z", used_bp=10, exhausted_hint="quota"), {"state": "exhausted", "issues": [], "age_s": 3600}),
        "stale-not-exhausted": (window(valid_until="2026-10-01T00:30:00Z", used_bp=10000), {"state": "stale", "issues": [], "age_s": 3600}),
        "empty-hint-stays-fresh": (window(valid_until="2026-10-01T06:00:00Z", exhausted_hint=""), {"state": "fresh", "issues": [], "age_s": 3600}),
        "flipped": (window(valid_until="2026-10-01T00:30:00Z", resets_at="2026-10-01T00:30:00Z"), {"state": "unknown", "issues": ["window_flipped"], "age_s": 3600}),
        "latched-past-ttl": (window(window_kind="latch", source_kind="latch", resets_at=None, valid_until="2026-10-01T00:30:00Z"), {"state": "latched", "issues": [], "age_s": 3600}),
        "latched-by-source": (window(source_kind="latch", resets_at="2026-10-08T00:00:00Z", valid_until="2026-10-01T00:30:00Z"), {"state": "latched", "issues": [], "age_s": 3600}),
        "latch-flipped": (window(window_kind="latch", source_kind="latch", observed_at="2026-09-30T00:00:00Z", resets_at="2026-10-01T00:30:00Z", valid_until="2026-10-01T00:20:00Z"), {"state": "unknown", "issues": ["window_flipped"], "age_s": 90000}),
        "clock-reset-before-observed": ({
            "observed_at": "2026-10-01T02:00:00Z", "valid_until": "2026-10-01T03:00:00Z", "resets_at": "2026-10-01T01:30:00Z",
        }, {"state": "unknown", "issues": ["clock_inconsistent"], "age_s": -3600}),
        "clock-span": (window(window_duration_s=60, resets_at="2026-10-01T02:00:00Z", valid_until="2026-10-01T01:30:00Z"), {"state": "unknown", "issues": ["clock_inconsistent"], "age_s": 3600}),
        "span-without-duration-stays-fresh": (window(valid_until="2026-10-01T06:00:00Z", resets_at="2026-10-08T00:00:00Z"), {"state": "fresh", "issues": [], "age_s": 3600}),
    }
    for name, (obs, expected) in cases.items():
        assert freshness(obs, now) == expected, name
    future = at(now + timedelta(minutes=2, seconds=1))
    assert freshness(window(observed_at=future, valid_until=at(now + timedelta(hours=2)), resets_at=None), now)["issues"] == ["clock_inconsistent"]
    boundary = at(now + timedelta(minutes=2))
    assert freshness(window(observed_at=boundary, valid_until=at(now + timedelta(hours=2)), resets_at=None), now)["state"] == "fresh"
    exact = window(observed_at=at(now), window_duration_s=3600, resets_at=at(now + timedelta(seconds=3900)), valid_until=at(now + timedelta(seconds=1800)))
    assert freshness(exact, "2026-10-01T01:00:00Z")["state"] == "fresh"
    over = window(observed_at=at(now), window_duration_s=3600, resets_at=at(now + timedelta(seconds=3901)), valid_until=at(now + timedelta(seconds=1800)))
    assert freshness(over, now)["issues"] == ["clock_inconsistent"]
    assert freshness(fresh, "2026-10-01T01:00:00Z")["state"] == "fresh"
    reject(freshness, fresh, datetime(2026, 10, 1, 1, 0))


def _merged(observations):
    result = merge_observations([normalize_observation(item) for item in observations])
    return [(item["observation"]["source_kind"], item["observation"]["used_bp"], item["issues"]) for item in result]


def test_merge_keeps_latest_and_flags_cross_source_conflict():
    base = datetime(2026, 10, 1, tzinfo=timezone.utc)
    older = at(base)
    newer = at(base + timedelta(minutes=1))
    assert _merged([
        window(source_kind="manual", used_bp=1000, observed_at=older),
        window(source_kind="manual", used_bp=2000, observed_at=newer),
    ]) == [("manual", 2000, [])]
    conflict = _merged([
        window(source_kind="probe_codex", used_bp=9000, observed_at=older),
        window(source_kind="manual", used_bp=1000, observed_at=newer),
    ])
    assert conflict == [("probe_codex", 9000, ["source_conflict"])]
    assert _merged([
        window(source_kind="manual", used_bp=1000, observed_at=older),
        window(source_kind="probe_codex", used_bp=2000, observed_at=newer),
    ]) == [("probe_codex", 2000, [])]
    assert _merged([
        window(source_kind="manual", used_bp=1000, observed_at=older),
        window(source_kind="manual", used_bp=9000, observed_at=newer),
    ]) == [("manual", 9000, [])]
    edge = at(base + timedelta(minutes=5))
    assert _merged([
        window(source_kind="manual", used_bp=1000, observed_at=older),
        window(source_kind="passive_codex", used_bp=2500, observed_at=edge),
    ]) == [("passive_codex", 2500, ["source_conflict"])]
    outside = at(base + timedelta(minutes=5, seconds=1))
    assert _merged([
        window(source_kind="manual", used_bp=1000, observed_at=older),
        window(source_kind="probe_codex", used_bp=9000, observed_at=outside),
    ]) == [("probe_codex", 9000, [])]
    separate = merge_observations([
        normalize_observation(window(bucket_id="b")),
        normalize_observation(window(bucket_id="a", window_kind="5h")),
    ])
    assert [(item["observation"]["bucket_id"], item["observation"]["window_kind"]) for item in separate] == [("a", "5h"), ("b", "weekly")]
    assert merge_observations([]) == []
    reject(merge_observations, {"scope_ref": "backend:codex"})


def test_merge_prefers_newer_reset_window_without_cross_window_conflict():
    # 99% 与 1% 只隔两分钟，但 resets_at 不属于同一窗口。结果取较新窗口的 1%。
    base = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert _merged([
        window(
            source_kind="probe_codex", confidence="high", used_bp=9900,
            observed_at=at(base + timedelta(minutes=2)), resets_at=at(base + timedelta(days=7)),
        ),
        window(
            source_kind="manual", used_bp=100,
            observed_at=at(base), resets_at=at(base + timedelta(days=14)),
        ),
    ]) == [("manual", 100, [])]


def test_normalize_probe_state_roundtrip_and_rejection():
    state = {
        "scope_ref": "backend:codex", "probe_id": "codex.app_server.rate_limits",
        "last_attempt_at": "2026-10-01T00:00:00+00:00", "last_error_code": "TIMEOUT",
    }
    got = normalize_probe_state(state)
    assert got["last_attempt_at"] == "2026-10-01T00:00:00Z"
    assert got["last_ok_at"] is None and got["lease_until"] is None
    assert normalize_probe_state(got) == got
    reject(normalize_probe_state, {**got, "token": "x"})
    reject(normalize_probe_state, {**got, "last_error_code": "402"})
    reject(normalize_probe_state, {**got, "note": "a@b"})


def test_append_normalizes_and_lists_in_observed_order():
    now = datetime.now(timezone.utc)
    earlier = at(now - timedelta(hours=2))
    later = at(now - timedelta(hours=1))
    valid = at(now + timedelta(hours=2))
    resets = at(now + timedelta(days=6))
    with quota_store() as store:
        first = store.append_quota_snapshot(window(
            observed_at=later.replace("Z", "+00:00"), valid_until=valid, resets_at=resets))
        second = store.append_quota_snapshot(window(
            observed_at=earlier, valid_until=valid, resets_at=resets,
            source_kind="probe_codex", confidence="high"))
        assert first["observation"]["observed_at"] == later
        assert first["id"].startswith("qsn_")
        rows = store.quota_snapshots()
        assert [row["observed_at"] for row in rows] == [earlier, later]
        assert rows[1] == first["observation"]
        assert store.quota_snapshots("backend:codex") == rows
        assert store.quota_snapshots("backend:other") == []
        assert len(rows) == 2 and second["observation"]["source_kind"] == "probe_codex"
        column = store._conn.execute("SELECT observed_at FROM plugin_quota_snapshots WHERE id=?", (first["id"],)).fetchone()
        assert column[0] == later
        validate_snapshot(store._conn)
        reject(store.append_quota_snapshot, window(email="user@example.com"))
        assert len(store.quota_snapshots()) == 2


def test_retention_keeps_newest_500_per_key_and_drops_older_than_30_days():
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=2)
    valid = at(now + timedelta(hours=1))
    with quota_store() as store:
        for index in range(501):
            store.append_quota_snapshot(window(
                resets_at=None, observed_at=at(start + timedelta(seconds=index)), valid_until=valid, used_bp=index))
        store.append_quota_snapshot(window(
            bucket_id="5h", window_kind="5h", resets_at=None,
            observed_at=at(start), valid_until=valid))
        rows = store.quota_snapshots("backend:codex")
        weekly = [row for row in rows if row["window_kind"] == "weekly"]
        assert len(weekly) == 500
        assert weekly[0]["observed_at"] == at(start + timedelta(seconds=1))
        assert weekly[-1]["used_bp"] == 500
        assert len([row for row in rows if row["window_kind"] == "5h"]) == 1
        old = now - timedelta(days=31)
        fresh = now - timedelta(days=29)
        store.append_quota_snapshot(window(
            scope_ref="backend:agy", resets_at=None, observed_at=at(old), valid_until=at(old + timedelta(hours=1))))
        assert store.quota_snapshots("backend:agy") == []
        store.append_quota_snapshot(window(
            scope_ref="backend:agy", resets_at=None, observed_at=at(fresh), valid_until=at(fresh + timedelta(hours=1))))
        assert [row["observed_at"] for row in store.quota_snapshots("backend:agy")] == [at(fresh)]
        store.append_quota_snapshot(window(
            scope_ref="backend:agy", resets_at=None, source_kind="probe_codex", confidence="high",
            observed_at=at(old), valid_until=at(old + timedelta(hours=1))))
        assert [row["source_kind"] for row in store.quota_snapshots("backend:agy")] == ["manual"]


def test_clear_manual_leaves_probe_latch_and_other_scopes():
    now = datetime.now(timezone.utc)
    valid = at(now + timedelta(hours=2))
    resets = at(now + timedelta(days=6))

    def sample(hours, **overrides):
        value = window(observed_at=at(now - timedelta(hours=hours)), valid_until=valid, resets_at=resets)
        value.update(overrides)
        return value

    with quota_store() as store:
        store.append_quota_snapshot(sample(5, scope_ref="backend:agy", source_kind="manual"))
        store.append_quota_snapshot(sample(4, scope_ref="backend:agy", source_kind="probe_codex", confidence="high"))
        store.append_quota_snapshot(sample(3, scope_ref="backend:agy", source_kind="passive_codex", confidence="medium"))
        store.append_quota_snapshot(sample(
            2, scope_ref="backend:agy", window_kind="latch", source_kind="latch", resets_at=None))
        store.append_quota_snapshot(sample(5, scope_ref="backend:other", source_kind="manual"))
        store.append_quota_snapshot(sample(5, scope_ref="backend:agy", source_kind="manual"), namespace="ns_other")
        state = {"scope_ref": "backend:agy", "probe_id": "codex.app", "last_error_code": "TIMEOUT"}
        assert store.put_probe_state(state)["last_error_code"] == "TIMEOUT"
        assert store.clear_manual("backend:agy") == 1
        left = store.quota_snapshots("backend:agy")
        assert [row["source_kind"] for row in left] == ["probe_codex", "passive_codex", "latch"]
        assert store.quota_snapshots("backend:other")[0]["source_kind"] == "manual"
        assert store.quota_snapshots("backend:agy", namespace="ns_other")[0]["source_kind"] == "manual"
        assert store.clear_manual("backend:missing") == 0
        assert store.get_probe_state("backend:agy", "codex.app")["last_error_code"] == "TIMEOUT"
        replaced = store.put_probe_state({**state, "last_ok_at": at(now - timedelta(minutes=5)), "last_error_code": None})
        assert store.get_probe_state("backend:agy", "codex.app") == replaced
        assert store.get_probe_state("backend:agy", "other") is None
        assert store._conn.execute("SELECT COUNT(*) FROM plugin_quota_probe_state").fetchone()[0] == 1
        validate_snapshot(store._conn)


def test_quota_schema_and_backup_roundtrip(tmp_path):
    now = datetime.now(timezone.utc)
    root = tmp_path / "source"
    root.mkdir()
    db = SqliteStore(root / DB_NAME)
    try:
        store = PluginStore(db._conn)
        columns = {row[1] for row in store._conn.execute("PRAGMA table_info(plugin_quota_snapshots)")}
        assert columns == {
            "namespace_id", "id", "scope_ref", "bucket_id", "window_kind", "source_kind",
            "window_epoch", "observed_at", "payload",
        }
        probe_columns = {row[1] for row in store._conn.execute("PRAGMA table_info(plugin_quota_probe_state)")}
        assert probe_columns == {
            "namespace_id", "scope_ref", "probe_id", "last_attempt_at", "last_ok_at",
            "last_error_code", "lease_until",
        }
        indexed = [row[2] for row in store._conn.execute("PRAGMA index_info(idx_plugin_quota_snapshots_key)")]
        assert indexed == ["namespace_id", "scope_ref", "bucket_id", "window_kind", "observed_at"]
        observation = normalize_observation(window(
            observed_at=at(now - timedelta(hours=1)), valid_until=at(now + timedelta(hours=2)),
            resets_at=at(now + timedelta(days=6))))
        store.append_quota_snapshot(observation)
        probe = normalize_probe_state({
            "scope_ref": "backend:codex", "probe_id": "codex.app",
            "last_attempt_at": at(now - timedelta(minutes=5)), "lease_until": at(now + timedelta(minutes=1)),
        })
        store.put_probe_state(probe)
        validate_snapshot(store._conn)
    finally:
        db.close()
    archive = tmp_path / "state.tar.gz"
    assert backup_state(root, archive)["schema_version"] == 6
    restored = tmp_path / "restored"
    assert restore_state(archive, restored)["schema_version"] == 6
    opened = SqliteStore(restored / DB_NAME)
    try:
        store = PluginStore(opened._conn)
        assert store.quota_snapshots() == [observation]
        assert store.get_probe_state("backend:codex", "codex.app") == probe
        validate_snapshot(opened._conn)
    finally:
        opened.close()


def test_old_backup_without_quota_tables_still_restores(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    db = SqliteStore(root / DB_NAME)
    db.close()
    with closing(sqlite3.connect(root / DB_NAME)) as conn:
        conn.execute("DROP TABLE plugin_quota_snapshots")
        conn.execute("DROP TABLE plugin_quota_probe_state")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert _validate_snapshot(root / DB_NAME) == 6
    archive = tmp_path / "old.tar.gz"
    assert backup_state(root, archive)["schema_version"] == 6
    restored = tmp_path / "restored"
    assert restore_state(archive, restored)["schema_version"] == 6
    with closing(sqlite3.connect(restored / DB_NAME)) as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "plugin_plans" in names
    assert "plugin_quota_snapshots" not in names
    assert "plugin_quota_probe_state" not in names


def test_quota_backup_rejects_partial_schema_and_corrupt_rows(tmp_path):
    now = datetime.now(timezone.utc)
    root = tmp_path / "source"
    root.mkdir()
    db = SqliteStore(root / DB_NAME)
    try:
        store = PluginStore(db._conn)
        store.append_quota_snapshot(window(
            observed_at=at(now - timedelta(hours=1)), valid_until=at(now + timedelta(hours=2)),
            resets_at=at(now + timedelta(days=6))))
        store.put_probe_state({"scope_ref": "backend:codex", "probe_id": "codex.app"})
        store._conn.execute("UPDATE plugin_quota_snapshots SET scope_ref='backend:other'")
        reject(validate_snapshot, store._conn)
        store._conn.execute("UPDATE plugin_quota_snapshots SET scope_ref='backend:codex'")
        store._conn.execute(
            "UPDATE plugin_quota_snapshots SET payload=replace(payload, '\"used_bp\":4800', '\"used_bp\":10001')")
        reject(validate_snapshot, store._conn)
        store._conn.execute(
            "UPDATE plugin_quota_snapshots SET payload=replace(payload, '\"used_bp\":10001', '\"used_bp\":4800')")
        validate_snapshot(store._conn)
        store._conn.execute("DROP INDEX idx_plugin_quota_snapshots_key")
        reject(validate_snapshot, store._conn)
    finally:
        db.close()
    reject(backup_state, root, tmp_path / "bad-index.tar.gz")
    with closing(sqlite3.connect(root / DB_NAME)) as conn:
        conn.execute("DROP TABLE plugin_quota_probe_state")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    error = reject(backup_state, root, tmp_path / "partial.tar.gz")
    assert "表结构" in error.message
    with closing(sqlite3.connect(root / DB_NAME)) as conn:
        conn.execute(
            """CREATE TABLE plugin_quota_probe_state (
                namespace_id TEXT NOT NULL, scope_ref TEXT NOT NULL, probe_id TEXT NOT NULL,
                last_attempt_at TEXT, last_ok_at TEXT, last_error_code TEXT, lease_until TEXT,
                PRIMARY KEY(namespace_id, scope_ref, probe_id))""")
        conn.execute("ALTER TABLE plugin_quota_snapshots RENAME COLUMN payload TO body")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    error = reject(backup_state, root, tmp_path / "columns.tar.gz")
    assert "表结构" in error.message
