from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from asterun.application import Application
from asterun.config_migration import migrate_v1_to_v2
from asterun.errors import AsterunError
from asterun.plugin_store import PluginStore, read_quota_snapshots
from asterun.quota.service import handle


@pytest.fixture
def managed(config_path):
    raw = migrate_v1_to_v2(config_path)["config"]
    config_path.write_text(json.dumps(raw))
    app = Application.from_paths(config_path, config_path.parent / "managed-state")
    try:
        yield app
    finally:
        app.close()


@pytest.fixture
def app(config_path):
    # conftest 的 app 是 MemoryStore。额度读写要求 SQLite 状态目录。
    opened = Application.from_paths(config_path, config_path.parent / "quota-state")
    try:
        yield opened
    finally:
        opened.close()


def utc_z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def alias_of(app) -> str:
    return next(iter(app.config.backends))


def manual_payload(alias: str, now: datetime | None = None, **overrides) -> dict:
    moment = now or datetime.now(timezone.utc)
    payload = {
        "backend": alias,
        "bucket_id": "weekly",
        "window_kind": "weekly",
        "used_percent": "48.5",
        "valid_until": utc_z(moment + timedelta(hours=1)),
    }
    payload.update(overrides)
    return payload


def probe_window(scope_ref: str) -> dict:
    # 字段与 tests/test_quota_snapshots.py 的 window() 一致，来源改为探针。
    return {
        "scope_ref": scope_ref,
        "bucket_id": "weekly",
        "window_kind": "weekly",
        "source_kind": "probe_codex",
        "confidence": "low",
        "paid_overage": False,
        "observed_at": "2026-10-01T00:00:00Z",
        "valid_until": "2026-10-01T12:00:00Z",
        "used_bp": 4800,
        "resets_at": "2026-10-08T00:00:00Z",
    }


def _run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {
        "HOME": str((cwd or Path.cwd()) / "unused-home"),
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
    }
    return subprocess.run(
        [sys.executable, "-m", "asterun", *args],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def test_v1_manual_set_inspect_clear(app):
    alias = alias_of(app)
    saved = handle(app, "quota.set", manual_payload(alias))
    assert saved["scope_ref"] == f"backend:{alias}"
    assert saved["observation"]["used_bp"] == 4850
    assert saved["observation"]["source_kind"] == "manual"
    assert saved["observation"]["confidence"] == "low"

    report = handle(app, "quota.inspect", {"backend": alias})
    scope = report["scopes"][0]
    assert scope["windows"][0]["freshness"] == "fresh"
    assert scope["tightest"]["used_bp"] == 4850
    assert report["upstream_queried"] is False

    cleared = handle(app, "quota.clear", {"backend": alias})
    assert cleared["cleared"] == 1
    after = handle(app, "quota.inspect", {"backend": alias})
    assert after["scopes"][0]["windows"] == []


def test_set_quota_rejects_invalid_manual_entry(app):
    alias = alias_of(app)
    now = datetime.now(timezone.utc)
    samples = [
        {"window_kind": "latch"},
        {"valid_until": utc_z(now - timedelta(hours=1))},
        {"valid_until": utc_z(now + timedelta(hours=25))},
        {
            "valid_until": utc_z(now + timedelta(hours=3)),
            "resets_at": utc_z(now + timedelta(hours=2)),
        },
        {"used_percent": "101"},
        {"used_percent": "4.567"},
        {"scope_ref": f"backend:{alias}"},
        {"backend": None, "scope_ref": "backend:missing"},
    ]
    for overrides in samples:
        payload = manual_payload(alias, now, **{key: value for key, value in overrides.items() if value is not None})
        if "backend" in overrides and overrides["backend"] is None:
            payload.pop("backend", None)
        with pytest.raises(AsterunError):
            handle(app, "quota.set", payload)


def test_clear_leaves_non_manual_probe(app):
    alias = alias_of(app)
    scope = f"backend:{alias}"
    PluginStore(app.store._conn).append_quota_snapshot(
        probe_window(scope) | {"bucket_id": "probe-weekly"},
    )
    handle(app, "quota.set", manual_payload(alias))
    cleared = handle(app, "quota.clear", {"backend": alias})
    assert cleared["cleared"] == 1
    windows = handle(app, "quota.inspect", {"backend": alias})["scopes"][0]["windows"]
    assert len(windows) == 1
    assert windows[0]["observation"]["source_kind"] == "probe_codex"


def test_inspect_does_not_write(app):
    conn = app.store._conn
    conn.execute("PRAGMA query_only=ON")
    try:
        report = handle(app, "quota.inspect", {})
    finally:
        conn.execute("PRAGMA query_only=OFF")
    assert report["upstream_queried"] is False
    assert "scopes" in report


def test_read_quota_snapshots_missing_table_creates_nothing():
    conn = sqlite3.connect(":memory:")
    try:
        assert read_quota_snapshots(conn, "backend:fake") == []
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
    finally:
        conn.close()


def test_v2_inspect_scopes_match_billing_pools(managed):
    report = handle(managed, "quota.inspect", {})
    assert {item["scope_ref"] for item in report["scopes"]} == set(managed.config.billing_pools)


def test_cli_rejects_latch_window_and_inspect_prints_ok(config_path: Path, isolated_env: Path):
    rejected = _run([
        "quota-set",
        "--backend", "fake",
        "--bucket", "weekly",
        "--window", "latch",
        "--used-percent", "48.5",
        "--valid-until", "2026-10-20T00:00:00Z",
    ])
    assert rejected.returncode != 0
    assert "invalid choice" in rejected.stderr
    assert "latch" in rejected.stderr

    state = isolated_env / "cli-quota-state"
    opened = Application.from_paths(config_path, state)
    opened.close()
    inspected = _run([
        "--config", str(config_path),
        "--state-dir", str(state),
        "quota-inspect",
    ])
    assert inspected.returncode == 0, inspected.stderr + inspected.stdout
    assert json.loads(inspected.stdout)["ok"] is True
