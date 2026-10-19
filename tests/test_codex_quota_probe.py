from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from asterun.config import BackendConfig
from asterun.errors import AsterunError
from asterun.plugin_store import PluginStore
from asterun.quota.codex_probe import parse_rate_limits, probe_codex
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
    assert transport.init_timeout == 10.0
    assert transport.read_timeout == 10.0
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
