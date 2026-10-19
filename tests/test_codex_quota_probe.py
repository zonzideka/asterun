from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from asterun.config import BackendConfig
from asterun.errors import AsterunError
from asterun.plugin_store import PluginStore
from asterun.quota.codex_probe import (
    CLOSE_GRACE_S,
    SPAWN_GRACE_S,
    parse_rate_limits,
    probe_codex,
    probe_lifetime_s,
    scrub_rate_limits,
)
from asterun.quota.models import canonical_utc, normalize_observation
from asterun.quota.service import PROBE_ID, handle
from asterun.sqlite_store import SqliteStore

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
SCOPE = "backend:codex"


def _unix(moment: datetime) -> int:
    return int(moment.timestamp())


def _future(delta: timedelta) -> int:
    return _unix(NOW + delta)


def _sample() -> dict:
    weekly = {"usedPercent": 48, "windowDurationMins": 10080, "resetsAt": _future(timedelta(days=5))}
    five_hour = {"usedPercent": 12.5, "windowDurationMins": 300, "resetsAt": _future(timedelta(hours=2))}
    return {
        "rateLimits": {"primary": dict(weekly), "secondary": None},
        "rateLimitsByLimitId": {
            "codex": {"primary": dict(weekly), "secondary": None},
            "codex_other": {"primary": dict(five_hour), "secondary": None},
        },
        "credits": {"hasCredits": True, "unlimited": False, "balance": "61639.42"},
        "planType": "pro",
        "email": "person@example.com",
        "rateLimitResetCredits": [
            {"expiresAt": _future(timedelta(days=1))},
            {"expiresAt": _future(timedelta(days=2))},
            {"expiresAt": _future(timedelta(days=2))},
        ],
    }


def test_parse_rate_limits_maps_windows_credits_and_grants():
    parsed = parse_rate_limits(_sample(), scope_ref=SCOPE, observed_at=NOW)
    assert [(item["bucket_id"], item["window_kind"]) for item in parsed] == [
        ("codex", "weekly"),
        ("codex_other", "5h"),
        ("codex-credits", "other"),
    ]
    weekly, five_hour, credits = parsed
    assert weekly["used_bp"] == 4800
    assert five_hour["used_bp"] == 1250
    assert five_hour["window_kind"] == "5h"
    assert "window_duration_s" not in weekly
    assert "window_duration_s" not in five_hour
    assert credits["paid_overage"] is True
    assert credits["confidence"] == "high"
    assert credits["window_kind"] == "other"
    assert "used_bp" not in credits
    grants = sorted(weekly["expiring_grants"], key=lambda item: item["expires_at"])
    assert [item["count"] for item in grants] == [1, 2]
    assert grants[0]["expires_at"] == canonical_utc(NOW + timedelta(days=1))
    assert grants[1]["expires_at"] == canonical_utc(NOW + timedelta(days=2))
    assert {item["kind"] for item in grants} == {"codex_full_reset"}
    assert "expiring_grants" not in five_hour
    assert "expiring_grants" not in credits
    dumped = json.dumps(parsed, ensure_ascii=False)
    assert "61639" not in dumped
    assert "planType" not in dumped
    assert "balance" not in dumped
    assert "@" not in dumped
    assert dumped.count("pro") == dumped.count("probe_codex")
    for item in parsed:
        assert item["source_kind"] == "probe_codex"
        assert item["confidence"] == "high"
        if item["bucket_id"] != "codex-credits":
            assert item["paid_overage"] is False
        normalize_observation(item)
    assert weekly["valid_until"] == "2026-10-19T12:10:00Z"
    assert weekly["resets_at"] == "2026-10-24T12:00:00Z"


def test_parse_rate_limits_rejects_malformed_input():
    for payload in (None, [], "x", 1, True, {"rateLimits": "no"}, {"rateLimits": []}):
        assert parse_rate_limits(payload, scope_ref=SCOPE, observed_at=NOW) == []
    past = {
        "rateLimits": {
            "primary": {"usedPercent": 10, "windowDurationMins": 300, "resetsAt": _unix(NOW - timedelta(seconds=1))},
            "secondary": None,
        }
    }
    assert parse_rate_limits(past, scope_ref=SCOPE, observed_at=NOW) == []
    hidden = {"credits": {"hasCredits": False, "balance": "61639.42"}, "planType": "pro", "email": "a@b.c"}
    assert parse_rate_limits(hidden, scope_ref=SCOPE, observed_at=NOW) == []


def test_huge_integers_in_rate_limit_notice_are_dropped():
    from asterun.backends.codex import CodexAppServerTransport, CodexSession

    huge = 10**400
    notice = {
        "rateLimits": {
            "primary": {"usedPercent": huge, "windowDurationMins": 10080, "resetsAt": huge},
            "secondary": None,
        }
    }
    scrubbed = scrub_rate_limits(notice)
    assert scrubbed["rateLimits"]["primary"] == {"windowDurationMins": 10080}
    session = CodexSession(
        session_id="thread-1",
        transport=CodexAppServerTransport(),
        cwd="/tmp/project",
        model="gpt-5.5",
        started_at="2026-10-19T00:00:00+00:00",
    )
    session.handle_message({"method": "account/rateLimits/updated", "params": notice})
    status = session.to_status_dict()
    captured = status["quota_rate_limits"]
    assert session.method_counts["account/rateLimits/updated"] == 1
    assert captured["params"]["rateLimits"]["primary"] == {"windowDurationMins": 10080}
    dumped = json.dumps(status)
    assert "1" + "0" * 400 not in dumped


def test_parse_rate_limits_skips_bad_bucket_and_marks_only_the_reached_window():
    future = _future(timedelta(hours=3))
    payload = {
        "rateLimits": {
            "primary": {"usedPercent": 100, "windowDurationMins": 300, "resetsAt": future},
            "secondary": {"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": future},
        },
        "rateLimitsByLimitId": {
            "not a bucket": {"primary": {"usedPercent": 80, "windowDurationMins": 300, "resetsAt": future}, "secondary": None},
            "ok.id": {"primary": {"usedPercent": 10, "windowDurationMins": 60, "resetsAt": future}, "secondary": None},
        },
        "rateLimitReachedType": "primary",
        "rateLimitResetCredits": [
            {"expiresAt": _future(timedelta(days=3))},
            {"expires_at": canonical_utc(NOW + timedelta(days=3))},
        ],
    }
    parsed = parse_rate_limits(payload, scope_ref=SCOPE, observed_at=NOW)
    assert [item["bucket_id"] for item in parsed] == ["codex", "codex", "ok.id"]
    primary = next(item for item in parsed if item["window_kind"] == "5h" and item["bucket_id"] == "codex")
    secondary = next(item for item in parsed if item["window_kind"] == "weekly")
    other = next(item for item in parsed if item["bucket_id"] == "ok.id")
    assert primary["exhausted_hint"] == "exhausted"
    assert primary["used_bp"] == 10000
    assert "exhausted_hint" not in secondary
    assert other["window_kind"] == "other"
    assert other["window_duration_s"] == 3600
    assert "exhausted_hint" not in other
    assert primary["expiring_grants"] == [{
        "count": 2,
        "expires_at": canonical_utc(NOW + timedelta(days=3)),
        "kind": "codex_full_reset",
    }]
    for item in parsed:
        normalize_observation(item)


def test_transport_reads_rate_limits_without_other_account_methods():
    from asterun.backends.codex import CODEX_CLIENT_METHODS, CodexAppServerTransport

    assert "account/rateLimits/read" in CODEX_CLIENT_METHODS
    transport = CodexAppServerTransport.__new__(CodexAppServerTransport)
    seen = {}

    def request(method, params, timeout=30.0):
        seen["args"] = (method, params, timeout)
        return {"rateLimits": {}}

    transport.request = request
    assert transport.read_rate_limits(3.5) == {"rateLimits": {}}
    assert seen["args"] == ("account/rateLimits/read", {}, 3.5)


class _Transport:
    def __init__(self, *, codex_bin, extra_env=()):
        self.codex_bin = codex_bin
        self.extra_env = extra_env
        self.methods: list[str] = []
        self.cwd = None

    def spawn(self, cwd):
        self.methods.append("spawn")
        self.cwd = cwd

    def initialize(self, timeout=20.0):
        self.methods.append("initialize")
        self.init_timeout = timeout
        return {"userAgent": "codex-cli/9.9.9", "serverInfo": {"version": "should-not-win"}}

    def read_rate_limits(self, timeout=10.0):
        self.methods.append("read_rate_limits")
        self.read_timeout = timeout
        return {
            "rateLimits": {
                "primary": {"usedPercent": 48, "windowDurationMins": 10080, "resetsAt": _future(timedelta(days=5))},
                "secondary": None,
            }
        }

    def close(self):
        self.methods.append("close")


def _executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_probe_codex_success_only_uses_rate_limit_read(monkeypatch, tmp_path):
    monkeypatch.setenv("ASTERUN_RUN_CODEX", "1")
    binary = _executable(tmp_path / "codex")
    holder = {}

    class Transport(_Transport):
        def __init__(self, *, codex_bin, extra_env=()):
            super().__init__(codex_bin=codex_bin, extra_env=extra_env)
            holder["transport"] = self

    result = probe_codex(
        SimpleNamespace(bin=str(binary), extra_env=("EXAMPLE=1",)),
        scope_ref=SCOPE,
        now=NOW,
        transport_factory=Transport,
    )
    transport = holder["transport"]
    assert transport.methods == ["spawn", "initialize", "read_rate_limits", "close"]
    assert transport.codex_bin == str(binary)
    assert transport.extra_env == ("EXAMPLE=1",)
    assert 0 < transport.init_timeout <= 10.0 + 1e-6
    assert 0 < transport.read_timeout <= transport.init_timeout + 1e-9
    assert not Path(transport.cwd).exists()
    assert result["ok"] is True
    assert result["native_version"] == "codex-cli/9.9.9"
    assert result["observations"][0]["used_bp"] == 4800
    assert result["observations"][0]["native_version"] == "codex-cli/9.9.9"
    assert "account/read" not in transport.methods


def test_probe_codex_initialize_failure_closes_and_hides_the_exception(monkeypatch, tmp_path):
    monkeypatch.setenv("ASTERUN_RUN_CODEX", "1")
    binary = _executable(tmp_path / "codex")
    holder = {}

    class Transport(_Transport):
        def __init__(self, *, codex_bin, extra_env=()):
            super().__init__(codex_bin=codex_bin, extra_env=extra_env)
            holder["transport"] = self

        def initialize(self, timeout=20.0):
            self.methods.append("initialize")
            raise RuntimeError("secret path /home/person@example.com")

    result = probe_codex(
        SimpleNamespace(bin=str(binary), extra_env=()),
        scope_ref=SCOPE,
        now=NOW,
        transport_factory=Transport,
    )
    assert result == {"ok": False, "error_code": "PROBE_FAILED"}
    assert holder["transport"].methods == ["spawn", "initialize", "close"]
    assert not Path(holder["transport"].cwd).exists()
    dumped = json.dumps(result)
    assert "secret" not in dumped
    assert "RuntimeError" not in dumped
    assert "@" not in dumped


def test_probe_codex_shares_one_deadline_across_both_rpcs(monkeypatch, tmp_path):
    monkeypatch.setenv("ASTERUN_RUN_CODEX", "1")
    binary = _executable(tmp_path / "codex")
    clock = {"now": 5_000.0}

    def monotonic():
        return clock["now"]

    monkeypatch.setattr("asterun.quota.codex_probe.time.monotonic", monotonic)
    holder = {}

    class Overrun(_Transport):
        def __init__(self, *, codex_bin, extra_env=()):
            super().__init__(codex_bin=codex_bin, extra_env=extra_env)
            holder["transport"] = self

        def initialize(self, timeout=20.0):
            self.methods.append("initialize")
            self.init_timeout = timeout
            clock["now"] += timeout + 1
            return {"userAgent": "codex-cli/9.9.9"}

        def read_rate_limits(self, timeout=10.0):
            raise AssertionError("deadline already passed")

    clock["now"] = 5_000.0
    overrun = probe_codex(
        SimpleNamespace(bin=str(binary), extra_env=()),
        scope_ref=SCOPE,
        now=NOW,
        timeout_s=10,
        transport_factory=Overrun,
    )
    assert overrun == {"ok": False, "error_code": "PROBE_TIMEOUT"}
    assert holder["transport"].methods == ["spawn", "initialize", "close"]
    assert not Path(holder["transport"].cwd).exists()

    class Partial(_Transport):
        def __init__(self, *, codex_bin, extra_env=()):
            super().__init__(codex_bin=codex_bin, extra_env=extra_env)
            holder["transport"] = self

        def initialize(self, timeout=20.0):
            self.methods.append("initialize")
            self.init_timeout = timeout
            clock["now"] += 4
            return {"userAgent": "codex-cli/9.9.9"}

        def read_rate_limits(self, timeout=10.0):
            self.read_timeout = timeout
            self.read_clock = clock["now"]
            return super().read_rate_limits(timeout)

    clock["now"] = 8_000.0
    started = clock["now"]
    partial = probe_codex(
        SimpleNamespace(bin=str(binary), extra_env=()),
        scope_ref=SCOPE,
        now=NOW,
        timeout_s=10,
        transport_factory=Partial,
    )
    transport = holder["transport"]
    assert partial["ok"] is True
    assert transport.methods == ["spawn", "initialize", "read_rate_limits", "close"]
    assert transport.read_timeout <= (started + 10) - transport.read_clock
    assert transport.read_timeout <= 6
    assert not Path(transport.cwd).exists()


def test_probe_codex_live_disabled(monkeypatch):
    monkeypatch.delenv("ASTERUN_RUN_CODEX", raising=False)
    called = []

    def factory(**kwargs):
        called.append(kwargs)
        raise AssertionError("live disabled must not construct a transport")

    result = probe_codex(
        SimpleNamespace(bin="/bin/true", extra_env=()),
        scope_ref=SCOPE,
        now=NOW,
        transport_factory=factory,
    )
    assert result == {"ok": False, "error_code": "LIVE_DISABLED"}
    assert called == []


def test_probe_codex_missing_binary(monkeypatch, tmp_path):
    monkeypatch.setenv("ASTERUN_RUN_CODEX", "1")
    monkeypatch.delenv("CODEX_BIN", raising=False)
    result = probe_codex(
        SimpleNamespace(bin=str(tmp_path / "missing-codex"), extra_env=()),
        scope_ref=SCOPE,
        now=NOW,
        transport_factory=lambda **kwargs: (_ for _ in ()).throw(AssertionError("missing binary")),
    )
    assert result == {"ok": False, "error_code": "BINARY_NOT_FOUND"}


def _weekly(scope_ref: str, now: datetime) -> dict:
    return {
        "scope_ref": scope_ref,
        "bucket_id": "codex",
        "window_kind": "weekly",
        "source_kind": "probe_codex",
        "confidence": "high",
        "paid_overage": False,
        "observed_at": canonical_utc(now),
        "valid_until": canonical_utc(now + timedelta(minutes=10)),
        "used_bp": 4800,
        "resets_at": canonical_utc(now + timedelta(days=5)),
    }


def test_quota_refresh_writes_throttles_and_records_probe_errors(app, monkeypatch, tmp_path):
    store = SqliteStore(tmp_path / "quota-state" / "asterun.sqlite")
    app.store = store
    store.bootstrap_applied_revision(app.config.revision)
    app.config.backends["codex"] = BackendConfig(name="codex", kind="codex", enabled=True, bin="/bin/true")
    seen = {"n": 0, "fail": False}

    def fake_probe(backend_config, *, scope_ref, now, timeout_s=10.0, ttl_s=600, transport_factory=None):
        seen["n"] += 1
        seen["kind"] = backend_config.kind
        seen["scope"] = scope_ref
        if seen["fail"]:
            return {"ok": False, "error_code": "PROBE_FAILED"}
        return {"ok": True, "observations": [_weekly(scope_ref, now)], "native_version": None}

    monkeypatch.setattr("asterun.quota.service.probe_codex", fake_probe)
    try:
        with pytest.raises(AsterunError):
            handle(app, "quota.refresh", {"backend": "fake"})
        assert seen["n"] == 0

        refreshed = handle(app, "quota.refresh", {"scope_ref": SCOPE})
        assert refreshed["upstream_queried"] is True
        assert refreshed["scopes"] == [{
            "scope_ref": SCOPE,
            "probe_id": PROBE_ID,
            "ok": True,
            "error_code": None,
            "written": 1,
            "skipped": None,
        }]
        assert seen["kind"] == "codex"
        assert seen["scope"] == SCOPE
        inspected = handle(app, "quota.inspect", {"backend": "codex"})
        window = inspected["scopes"][0]["windows"][0]["observation"]
        assert window["window_kind"] == "weekly"
        assert window["used_bp"] == 4800

        again = handle(app, "quota.refresh", {"backend": "codex"})
        assert again["scopes"][0]["skipped"] == "min_interval"
        assert again["upstream_queried"] is True
        assert seen["n"] == 1

        plugin_store = PluginStore(app.store._conn)
        state = plugin_store.get_probe_state(SCOPE, PROBE_ID)
        past = canonical_utc(datetime.now(timezone.utc) - timedelta(seconds=120))
        plugin_store.put_probe_state({**state, "last_attempt_at": past})
        seen["fail"] = True
        failed = handle(app, "quota.refresh", {"backend": "codex"})
        assert failed["scopes"][0]["ok"] is False
        assert failed["scopes"][0]["error_code"] == "PROBE_FAILED"
        assert failed["scopes"][0]["written"] == 0
        assert failed["scopes"][0]["skipped"] is None
        failed_state = plugin_store.get_probe_state(SCOPE, PROBE_ID)
        assert failed_state["last_error_code"] == "PROBE_FAILED"
        assert failed_state["last_ok_at"] == state["last_ok_at"]
        still = handle(app, "quota.inspect", {"backend": "codex"})
        assert len(still["scopes"][0]["windows"]) == 1
        assert still["scopes"][0]["windows"][0]["observation"]["used_bp"] == 4800
        assert seen["n"] == 2
    finally:
        store.close()


def _sqlite_app(config, conn, policy, principal):
    """第二条独立连接上的额度入口。不复用第一条连接的事务。"""

    class Store:
        def __init__(self):
            self._conn = conn

        def get_applied_revision(self):
            row = self._conn.execute("SELECT value FROM meta WHERE key='applied_config_revision'").fetchone()
            return None if row is None else int(row[0])

    class App:
        def __init__(self):
            self.config = config
            self.store = Store()
            self.policy = policy
            self.principal = principal
            self.grant = None
            self.admission = None

        def _require_grant(self, resource):
            from asterun.application import utc_now
            from asterun.policy import enforce_grant
            enforce_grant(self.grant, resource, utc_now())

        def _require_applied_config(self):
            applied = self.store.get_applied_revision()
            if applied != self.config.revision:
                raise AsterunError("REVISION_CONFLICT", "配置尚未应用到第二条连接")

    return App()


def test_refresh_lease_is_single_flight_across_connections(app, monkeypatch, tmp_path):
    path = tmp_path / "quota-flight" / "asterun.sqlite"
    store = SqliteStore(path)
    app.store = store
    store.bootstrap_applied_revision(app.config.revision)
    app.config.backends["codex"] = BackendConfig(name="codex", kind="codex", enabled=True, bin="/bin/true")
    second_conn = sqlite3.connect(path, isolation_level=None)
    PluginStore(second_conn)
    second = _sqlite_app(app.config, second_conn, app.policy, app.principal)
    calls = {"n": 0}
    seen = {}

    def fake_probe(backend_config, *, scope_ref, now, timeout_s=10.0, ttl_s=600, transport_factory=None):
        calls["n"] += 1
        if calls["n"] == 1:
            seen["during"] = handle(second, "quota.refresh", {"backend": "codex"})
        return {"ok": True, "observations": [_weekly(scope_ref, now)], "native_version": None}

    monkeypatch.setattr("asterun.quota.service.probe_codex", fake_probe)
    try:
        first = handle(app, "quota.refresh", {"backend": "codex"})
        assert calls["n"] == 1
        assert first["scopes"][0]["ok"] is True
        assert first["scopes"][0]["written"] == 1
        assert seen["during"]["scopes"][0]["skipped"] == "in_flight"
        assert seen["during"]["scopes"][0]["written"] == 0
        assert seen["during"]["scopes"][0]["ok"] is False
        state = PluginStore(store._conn).get_probe_state(SCOPE, PROBE_ID)
        assert state["lease_until"] is None
        assert len(PluginStore(second_conn).quota_snapshots(SCOPE)) == 1
    finally:
        second_conn.close()
        store.close()


def test_refresh_lease_still_in_flight_at_44_seconds(app, monkeypatch, tmp_path):
    from asterun.quota.settings import codex_quota_settings

    path = tmp_path / "quota-lifetime" / "asterun.sqlite"
    store = SqliteStore(path)
    app.store = store
    store.bootstrap_applied_revision(app.config.revision)
    app.config.backends["codex"] = BackendConfig(name="codex", kind="codex", enabled=True, bin="/bin/true")
    second_conn = sqlite3.connect(path, isolation_level=None)
    PluginStore(second_conn)
    second = _sqlite_app(app.config, second_conn, app.policy, app.principal)
    start = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
    clock = {"now": start}
    seen = {}

    def settings(config):
        value = codex_quota_settings(config)
        value["probes"]["codex_app_server"]["timeout_seconds"] = 30
        return value

    def fake_probe(backend_config, *, scope_ref, now, timeout_s=10.0, ttl_s=600, transport_factory=None):
        seen["lease"] = PluginStore(app.store._conn).get_probe_state(SCOPE, PROBE_ID)["lease_until"]
        clock["now"] = start + timedelta(seconds=44)
        seen["during"] = handle(second, "quota.refresh", {"backend": "codex"})
        return {"ok": True, "observations": [_weekly(scope_ref, now)], "native_version": None}

    monkeypatch.setattr("asterun.quota.service._now", lambda: clock["now"])
    monkeypatch.setattr("asterun.quota.service.codex_quota_settings", settings)
    monkeypatch.setattr("asterun.quota.service.probe_codex", fake_probe)
    try:
        assert SPAWN_GRACE_S == 5 and CLOSE_GRACE_S == 10
        assert probe_lifetime_s(30) == 45
        first = handle(app, "quota.refresh", {"backend": "codex"})
        assert first["scopes"][0]["ok"] is True
        assert seen["lease"] == canonical_utc(start + timedelta(seconds=45))
        assert seen["during"]["scopes"][0]["skipped"] == "in_flight"
        assert seen["during"]["scopes"][0]["ok"] is False
        assert seen["during"]["scopes"][0]["written"] == 0
        state = PluginStore(store._conn).get_probe_state(SCOPE, PROBE_ID)
        assert state["lease_until"] is None
    finally:
        second_conn.close()
        store.close()


def test_refresh_release_keeps_a_newer_holder(app, monkeypatch, tmp_path):
    path = tmp_path / "quota-holder" / "asterun.sqlite"
    store = SqliteStore(path)
    app.store = store
    store.bootstrap_applied_revision(app.config.revision)
    app.config.backends["codex"] = BackendConfig(name="codex", kind="codex", enabled=True, bin="/bin/true")
    plugin = PluginStore(app.store._conn)
    start = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
    clock = {"now": start}
    holder = {}

    def fake_probe(backend_config, *, scope_ref, now, timeout_s=10.0, ttl_s=600, transport_factory=None):
        clock["now"] = start + timedelta(seconds=61)
        current = plugin.get_probe_state(SCOPE, PROBE_ID)
        holder["original_attempt"] = current["last_attempt_at"]
        plugin.put_probe_state({
            **current,
            "last_attempt_at": canonical_utc(clock["now"]),
            "lease_until": canonical_utc(clock["now"] - timedelta(seconds=1)),
        })
        holder["stolen"] = plugin.get_probe_state(SCOPE, PROBE_ID)
        return {"ok": True, "observations": [_weekly(scope_ref, now)], "native_version": None}

    monkeypatch.setattr("asterun.quota.service._now", lambda: clock["now"])
    monkeypatch.setattr("asterun.quota.service.probe_codex", fake_probe)
    try:
        first = handle(app, "quota.refresh", {"backend": "codex"})
        stolen = holder["stolen"]
        assert stolen["last_attempt_at"] != holder["original_attempt"]
        assert stolen["lease_until"] == canonical_utc(start + timedelta(seconds=60))
        state = plugin.get_probe_state(SCOPE, PROBE_ID)
        assert state["last_attempt_at"] == stolen["last_attempt_at"]
        assert state["lease_until"] == stolen["lease_until"]
        assert state == stolen
        assert first["scopes"][0]["written"] == 1
        assert len(plugin.quota_snapshots(SCOPE)) == 1
    finally:
        store.close()


def test_refresh_interval_floor_is_60_seconds(app, monkeypatch, tmp_path):
    from asterun.quota.settings import codex_quota_settings

    path = tmp_path / "quota-floor" / "asterun.sqlite"
    store = SqliteStore(path)
    app.store = store
    store.bootstrap_applied_revision(app.config.revision)
    app.config.backends["codex"] = BackendConfig(name="codex", kind="codex", enabled=True, bin="/bin/true")
    calls = {"n": 0}

    def short(config):
        settings = codex_quota_settings(config)
        settings["probes"]["codex_app_server"]["min_interval_seconds"] = 30
        return settings

    def fake_probe(backend_config, *, scope_ref, now, timeout_s=10.0, ttl_s=600, transport_factory=None):
        calls["n"] += 1
        return {"ok": True, "observations": [_weekly(scope_ref, now)], "native_version": None}

    monkeypatch.setattr("asterun.quota.service.codex_quota_settings", short)
    monkeypatch.setattr("asterun.quota.service.probe_codex", fake_probe)
    try:
        assert handle(app, "quota.refresh", {"backend": "codex"})["scopes"][0]["ok"] is True
        plugin_store = PluginStore(app.store._conn)
        state = plugin_store.get_probe_state(SCOPE, PROBE_ID)
        moment = datetime.now(timezone.utc)
        plugin_store.put_probe_state({
            **state,
            "last_attempt_at": canonical_utc(moment - timedelta(seconds=45)),
            "lease_until": None,
        })
        skipped = handle(app, "quota.refresh", {"backend": "codex"})
        assert skipped["scopes"][0]["skipped"] == "min_interval"
        assert calls["n"] == 1
        plugin_store.put_probe_state({
            **plugin_store.get_probe_state(SCOPE, PROBE_ID),
            "last_attempt_at": canonical_utc(moment - timedelta(seconds=61)),
            "lease_until": None,
        })
        again = handle(app, "quota.refresh", {"backend": "codex"})
        assert again["scopes"][0]["skipped"] is None
        assert again["scopes"][0]["ok"] is True
        assert calls["n"] == 2
    finally:
        store.close()
