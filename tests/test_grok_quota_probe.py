from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from asterun.application import Application
from asterun.config import BackendConfig, load_config
from asterun.config_migration import migrate_v1_to_v2
from asterun.config_v2 import default_quota
from asterun.errors import INVALID_CONFIG, UNKNOWN_FIELD, AsterunError
from asterun.grok_profile import build_environment, prepare_home
from asterun.quota.grok_probe import GROK_BILLING_METHOD, parse_billing, probe_grok
from asterun.quota.models import canonical_utc, normalize_observation
from asterun.quota.service import GROK_PROBE_ID, handle
from asterun.quota.settings import grok_quota_settings
from asterun.transport import JsonRpcError

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
SCOPE = "pool_grok"
PERIOD_START = "2026-10-01T00:00:00Z"
PERIOD_END = "2026-11-01T00:00:00Z"
PERIOD_SECONDS = 31 * 24 * 60 * 60


def _sample() -> dict:
    return {
        "creditUsagePercent": 37.5,
        "billingPeriodStart": PERIOD_START,
        "billingPeriodEnd": PERIOD_END,
        "monthlyLimit": 123456,
        "onDemandEnabled": True,
        "email": "x@y.z",
        "userId": "u-secret",
    }


def _assert_hidden(value) -> None:
    dumped = json.dumps(value, ensure_ascii=False)
    assert "123456" not in dumped
    assert "x@y.z" not in dumped
    assert "u-secret" not in dumped
    assert "@" not in dumped


def _write(path: Path, raw: dict) -> Path:
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def _proposed(config_path: Path, isolated_env: Path) -> dict:
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["backends"]["grok"] = {"kind": "grok", "enabled": True, "home": str(isolated_env / "unused-grok-home")}
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    return migrate_v1_to_v2(config_path)["config"]


def _open(path: Path, state: Path) -> Application:
    return Application.from_paths(path, state)


def _disabled(scope_ref: str) -> dict:
    return {
        "scope_ref": scope_ref,
        "probe_id": GROK_PROBE_ID,
        "ok": False,
        "error_code": "PROBE_DISABLED",
        "written": 0,
        "skipped": None,
    }


def test_refresh_grok_stays_disabled_until_both_flags(config_path, isolated_env, monkeypatch):
    calls = []

    def fake_probe(*args, **kwargs):
        calls.append((args, kwargs))
        return {"ok": False, "error_code": "PROBE_FAILED"}

    monkeypatch.setattr("asterun.quota.grok_probe.probe_grok", fake_probe)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["backends"]["grok"] = {"kind": "grok", "enabled": True, "home": str(isolated_env / "unused-grok-home")}
    v1 = _open(_write(isolated_env / "v1-grok.json", raw), isolated_env / "v1-state")
    try:
        v1.config.quota = {
            "probes": {"grok_acp_billing": {"enabled": True, "acknowledge_undocumented": True}},
        }
        assert grok_quota_settings(v1.config)["probes"]["grok_acp_billing"]["enabled"] is False
        refreshed = handle(v1, "quota.refresh", {"backend": "grok"})
        assert calls == []
        assert refreshed["scopes"] == [_disabled(f"backend:grok")]
    finally:
        v1.close()

    proposed = _proposed(config_path, isolated_env)
    assert "quota" not in proposed
    baseline = _open(_write(isolated_env / "v2-default.json", proposed), isolated_env / "v2-default-state")
    try:
        assert baseline.config.quota_explicit is False
        assert "quota" not in baseline.config.to_dict()
        assert grok_quota_settings(baseline.config)["probes"]["grok_acp_billing"]["enabled"] is False
        refreshed = handle(baseline, "quota.refresh", {"backend": "grok"})
        assert calls == []
        assert refreshed["scopes"] == [_disabled(refreshed["scopes"][0]["scope_ref"])]
    finally:
        baseline.close()

    enabled_only = json.loads(json.dumps(proposed))
    enabled_only["quota"] = {"probes": {"grok_acp_billing": {"enabled": True}}}
    only = _open(_write(isolated_env / "v2-enabled.json", enabled_only), isolated_env / "v2-enabled-state")
    try:
        assert only.config.quota["probes"]["grok_acp_billing"]["acknowledge_undocumented"] is False
        refreshed = handle(only, "quota.refresh", {"backend": "grok"})
        assert calls == []
        assert refreshed["scopes"][0]["error_code"] == "PROBE_DISABLED"
        assert refreshed["scopes"][0]["probe_id"] == GROK_PROBE_ID
    finally:
        only.close()

    both = json.loads(json.dumps(proposed))
    both["quota"] = {"probes": {"grok_acp_billing": {"enabled": True, "acknowledge_undocumented": True}}}
    opened = _open(_write(isolated_env / "v2-both.json", both), isolated_env / "v2-both-state")
    try:
        monkeypatch.delenv("ASTERUN_RUN_GROK", raising=False)
        blocked = handle(opened, "quota.refresh", {"backend": "grok"})
        assert calls == []
        assert blocked["scopes"][0]["probe_id"] == GROK_PROBE_ID
        assert blocked["scopes"][0]["ok"] is False
        assert blocked["scopes"][0]["error_code"] is None
        assert blocked["scopes"][0]["written"] == 0
        assert blocked["scopes"][0]["skipped"] == "disabled"
        monkeypatch.setenv("ASTERUN_RUN_GROK", "1")
        refreshed = handle(opened, "quota.refresh", {"backend": "grok"})
        assert len(calls) == 1
        assert calls[0][0][0].kind == "grok"
        assert calls[0][1]["timeout_s"] == 10
        assert calls[0][1]["ttl_s"] == 900
        assert refreshed["scopes"][0]["probe_id"] == GROK_PROBE_ID
        assert refreshed["scopes"][0]["error_code"] == "PROBE_FAILED"
        assert refreshed["upstream_queried"] is True
    finally:
        opened.close()


def test_parse_billing_keeps_ratio_period_and_on_demand():
    parsed = parse_billing(_sample(), scope_ref=SCOPE, observed_at=NOW, ttl_s=900)
    assert [item["bucket_id"] for item in parsed] == ["grok", "grok-on-demand"]
    main, overage = parsed
    assert main["used_bp"] == 3750
    assert main["window_kind"] == "billing_period"
    assert main["confidence"] == "medium"
    assert main["source_kind"] == "probe_grok"
    assert main["paid_overage"] is False
    assert main["resets_at"] == PERIOD_END
    assert main["window_duration_s"] == PERIOD_SECONDS
    assert main["valid_until"] == "2026-10-19T12:15:00Z"
    assert overage["window_kind"] == "other"
    assert overage["paid_overage"] is True
    assert overage["confidence"] == "medium"
    assert "used_bp" not in overage
    assert overage["resets_at"] == PERIOD_END
    _assert_hidden(parsed)
    for item in parsed:
        normalize_observation(item)
    clamped = parse_billing(_sample(), scope_ref=SCOPE, observed_at=NOW, ttl_s=10**7)
    assert clamped[0]["valid_until"] == PERIOD_END
    assert clamped[0]["confidence"] == "medium"


def test_parse_billing_accepts_milliseconds_wrapper_and_usage_percent():
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    end = datetime(2026, 11, 1, tzinfo=timezone.utc)
    payload = {
        "creditUsagePercent": 37.5,
        "billingPeriodStart": int(start.timestamp()),
        "billingPeriodEnd": int(end.timestamp() * 1000),
        "monthlyLimit": 123456,
        "onDemandUsagePercent": 2,
        "email": "x@y.z",
    }
    wrapped = parse_billing({"billing": payload, "userId": "u-secret"}, scope_ref=SCOPE, observed_at=NOW, ttl_s=900)
    direct = parse_billing(payload, scope_ref=SCOPE, observed_at=NOW, ttl_s=900)
    usage = parse_billing({"usage": payload}, scope_ref=SCOPE, observed_at=NOW, ttl_s=900)
    assert wrapped == direct == usage
    assert [item["bucket_id"] for item in wrapped] == ["grok"]
    assert wrapped[0]["resets_at"] == PERIOD_END
    assert wrapped[0]["used_bp"] == 3750
    assert wrapped[0]["window_duration_s"] == PERIOD_SECONDS
    _assert_hidden(wrapped)
    quiet = parse_billing(
        {"creditUsagePercent": 1, "onDemandEnabled": False, "onDemandUsagePercent": 0},
        scope_ref=SCOPE, observed_at=NOW, ttl_s=900,
    )
    assert [item["bucket_id"] for item in quiet] == ["grok"]


def test_on_demand_reads_only_enabled_boolean():
    base = {"creditUsagePercent": 10, "billingPeriodEnd": PERIOD_END}
    enabled = parse_billing(
        {**base, "onDemandEnabled": True, "onDemandCap": 0, "onDemandUsed": 0},
        scope_ref=SCOPE, observed_at=NOW, ttl_s=900,
    )
    assert [item["bucket_id"] for item in enabled] == ["grok", "grok-on-demand"]
    assert enabled[1]["paid_overage"] is True
    assert "used_bp" not in enabled[1]
    _assert_hidden(enabled)
    rejected = [
        {"onDemandEnabled": False, "onDemandCap": 50, "onDemandUsed": 10, "onDemandUsagePercent": 80},
        {"onDemandUsagePercent": 2},
        {"onDemandCap": 100},
        {"onDemandUsed": 1},
        {"onDemandLimit": 5},
        {"onDemandSomething": True},
        {"onDemandEnabled": 1},
        {"onDemandEnabled": "true"},
        {"onDemandEnabled": "false"},
    ]
    for extra in rejected:
        parsed = parse_billing({**base, **extra}, scope_ref=SCOPE, observed_at=NOW, ttl_s=900)
        assert [item["bucket_id"] for item in parsed] == ["grok"], extra
        dumped = json.dumps(parsed)
        assert "50" not in dumped
        assert "80" not in dumped
        _assert_hidden(parsed)


def test_expired_or_illegal_billing_period_end_produces_no_observation(monkeypatch, tmp_path):
    monkeypatch.setenv("ASTERUN_RUN_GROK", "1")
    monkeypatch.delenv("GROK_BIN", raising=False)
    config = _backend(tmp_path)
    expired = "2026-10-01T00:00:00Z"
    equal = "2026-10-19T12:00:00Z"
    illegal = (
        "yesterday",
        "2026-11-01",
        "2026-11-01T00:00:00",
        "2026-11-01T00:00:00+08:00",
        "",
        None,
        True,
        False,
        -1,
        0,
        "not-a-date",
    )

    def via_probe(payload):
        class Transport(_Transport):
            def read_billing(self, timeout):
                self.methods.append("read_billing")
                return payload

        return probe_grok(
            config, scope_ref=SCOPE, now=NOW, timeout_s=10, ttl_s=900, transport_factory=Transport,
        )

    for end in (expired, equal, *illegal):
        payload = {**_sample(), "billingPeriodEnd": end}
        assert parse_billing(payload, scope_ref=SCOPE, observed_at=NOW, ttl_s=900) == []
        assert parse_billing({"billing": payload}, scope_ref=SCOPE, observed_at=NOW, ttl_s=900) == []
        result = via_probe(payload)
        assert result == {"ok": False, "error_code": "SCHEMA_MISMATCH"}
        assert "observations" not in result
        _assert_hidden(result)

    absent = _sample()
    absent.pop("billingPeriodEnd")
    parsed = parse_billing(absent, scope_ref=SCOPE, observed_at=NOW, ttl_s=900)
    assert [item["bucket_id"] for item in parsed] == ["grok", "grok-on-demand"]
    assert "resets_at" not in parsed[0]
    probed = via_probe(absent)
    assert probed["ok"] is True
    assert "resets_at" not in probed["observations"][0]


def test_parse_billing_rejects_missing_percent_and_ignores_unknown_fields():
    missing = {
        "billingPeriodStart": PERIOD_START,
        "billingPeriodEnd": PERIOD_END,
        "monthlyLimit": 123456,
        "email": "x@y.z",
        "userId": "u-secret",
    }
    assert parse_billing(missing, scope_ref=SCOPE, observed_at=NOW, ttl_s=900) == []
    assert parse_billing({"billing": missing}, scope_ref=SCOPE, observed_at=NOW, ttl_s=900) == []
    for bad in (None, "37.5", True, False, float("nan"), 1001, -1, {"creditUsagePercent": "37.5"}):
        assert parse_billing({"creditUsagePercent": bad}, scope_ref=SCOPE, observed_at=NOW, ttl_s=900) == []
    parsed = parse_billing(
        {
            "creditUsagePercent": 1,
            "mystery": "ignore-me",
            "monthlyLimit": 123456,
            "email": "x@y.z",
            "userId": "u-secret",
            "onDemandLimit": "u-secret",
        },
        scope_ref=SCOPE, observed_at=NOW, ttl_s=900,
    )
    assert len(parsed) == 1
    assert parsed[0]["used_bp"] == 100
    assert parsed[0]["confidence"] == "medium"
    assert "resets_at" not in parsed[0]
    assert parsed[0]["valid_until"] == canonical_utc(NOW + timedelta(seconds=900))
    _assert_hidden(parsed)
    assert "ignore-me" not in json.dumps(parsed)


def test_read_billing_stays_outside_the_dispatch_whitelist():
    from asterun.backends.grok import GROK_ACP_METHODS, GROK_QUOTA_METHODS, GrokAcpTransport

    assert GROK_BILLING_METHOD == "_x.ai/billing"
    assert GROK_BILLING_METHOD not in GROK_ACP_METHODS
    assert GROK_QUOTA_METHODS == ("initialize", GROK_BILLING_METHOD)
    transport = GrokAcpTransport.__new__(GrokAcpTransport)
    seen = {}

    def request(method, params, timeout=30.0):
        seen["args"] = (method, params, timeout)
        return {"creditUsagePercent": 1}

    transport.request = request
    assert transport.read_billing(4) == {"creditUsagePercent": 1}
    assert seen["args"] == (GROK_BILLING_METHOD, {}, 4)


def _executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


class _Transport:
    def __init__(self, *, grok_bin, env=None, **kwargs):
        self.grok_bin = grok_bin
        self.env = env
        self.kwargs = kwargs
        self.methods: list[str] = []
        self.cwd = None

    def spawn(self, cwd):
        self.methods.append("spawn")
        self.cwd = cwd

    def initialize(self, timeout=20.0):
        self.methods.append("initialize")
        self.init_timeout = timeout
        return {"protocolVersion": 1}

    def read_billing(self, timeout):
        self.methods.append("read_billing")
        self.read_timeout = timeout
        return _sample()

    def close(self):
        self.methods.append("close")


def _backend(tmp_path: Path) -> BackendConfig:
    binary = _executable(tmp_path / "grok-bin")
    home = tmp_path / "grok-home"
    prepare_home(home)
    return BackendConfig(name="grok", kind="grok", enabled=True, bin=str(binary), home=str(home))


def test_probe_grok_success_only_initialize_and_billing(monkeypatch, tmp_path):
    from asterun.backends.grok import GrokBackend

    monkeypatch.setenv("ASTERUN_RUN_GROK", "1")
    monkeypatch.delenv("GROK_BIN", raising=False)
    holder = {}

    class Transport(_Transport):
        def __init__(self, *, grok_bin, env=None, **kwargs):
            super().__init__(grok_bin=grok_bin, env=env, **kwargs)
            holder["transport"] = self

    config = _backend(tmp_path)
    result = probe_grok(config, scope_ref=SCOPE, now=NOW, timeout_s=10, ttl_s=900, transport_factory=Transport)
    transport = holder["transport"]
    backend = GrokBackend(config)
    assert transport.methods == ["spawn", "initialize", "read_billing", "close"]
    assert "session/new" not in transport.methods
    assert "session/prompt" not in transport.methods
    assert transport.grok_bin == str(backend.discover_bin())
    assert transport.env == build_environment(backend._prepared_home())
    assert result["ok"] is True
    assert result["observations"][0]["used_bp"] == 3750
    assert result["observations"][0]["confidence"] == "medium"
    assert not Path(transport.cwd).exists()
    _assert_hidden(result)


def test_probe_grok_maps_auth_timeout_and_schema_without_leaking_text(monkeypatch, tmp_path):
    monkeypatch.setenv("ASTERUN_RUN_GROK", "1")
    monkeypatch.delenv("GROK_BIN", raising=False)
    config = _backend(tmp_path)

    def run(read_billing):
        holder = {}

        class Transport(_Transport):
            def __init__(self, *, grok_bin, env=None, **kwargs):
                super().__init__(grok_bin=grok_bin, env=env, **kwargs)
                holder["transport"] = self

            def read_billing(self, timeout):
                self.methods.append("read_billing")
                return read_billing(self, timeout)

        result = probe_grok(
            config, scope_ref=SCOPE, now=NOW, timeout_s=10, ttl_s=900, transport_factory=Transport,
        )
        return result, holder["transport"]

    auth, transport = run(lambda self, timeout: (_ for _ in ()).throw(JsonRpcError(-32000, "token=super-secret-value")))
    assert auth == {"ok": False, "error_code": "AUTH_REQUIRED"}
    assert transport.methods == ["spawn", "initialize", "read_billing", "close"]
    assert "super-secret-value" not in json.dumps(auth)

    login, _transport = run(lambda self, timeout: (_ for _ in ()).throw(JsonRpcError(-32603, "Please LOGIN again")))
    assert login == {"ok": False, "error_code": "AUTH_REQUIRED"}
    assert "LOGIN" not in json.dumps(login)

    timed, transport = run(lambda self, timeout: (_ for _ in ()).throw(TimeoutError("please login within 1s")))
    assert timed == {"ok": False, "error_code": "PROBE_TIMEOUT"}
    assert "login" not in json.dumps(timed)
    assert transport.methods[-1] == "close"

    mismatch, transport = run(lambda self, timeout: {"monthlyLimit": 123456, "email": "x@y.z", "userId": "u-secret"})
    assert mismatch == {"ok": False, "error_code": "SCHEMA_MISMATCH"}
    assert transport.methods == ["spawn", "initialize", "read_billing", "close"]
    _assert_hidden(mismatch)


def test_probe_grok_live_disabled(monkeypatch):
    monkeypatch.delenv("ASTERUN_RUN_GROK", raising=False)
    called = []

    def factory(**kwargs):
        called.append(kwargs)
        raise AssertionError("live disabled must not construct a transport")

    result = probe_grok(
        BackendConfig(name="grok", kind="grok"),
        scope_ref=SCOPE,
        now=NOW,
        transport_factory=factory,
    )
    assert result == {"ok": False, "skipped": "disabled"}
    assert called == []


def test_probe_grok_connection_and_missing_binary_are_schema_mismatch(monkeypatch, tmp_path):
    monkeypatch.setenv("ASTERUN_RUN_GROK", "1")
    monkeypatch.delenv("GROK_BIN", raising=False)
    config = _backend(tmp_path)

    def run(mutate):
        holder = {}

        class Transport(_Transport):
            def __init__(self, *, grok_bin, env=None, **kwargs):
                super().__init__(grok_bin=grok_bin, env=env, **kwargs)
                holder["transport"] = self

            def spawn(self, cwd):
                self.methods.append("spawn")
                self.cwd = cwd
                mutate(self, "spawn")

            def read_billing(self, timeout):
                self.methods.append("read_billing")
                mutate(self, "read_billing")
                return _sample()

        result = probe_grok(
            config, scope_ref=SCOPE, now=NOW, timeout_s=10, ttl_s=900, transport_factory=Transport,
        )
        return result, holder["transport"]

    def close_read(self, method):
        if method == "read_billing":
            raise ConnectionError("connection closed token=super-secret-value")

    closed, transport = run(close_read)
    assert closed == {"ok": False, "error_code": "SCHEMA_MISMATCH"}
    assert transport.methods[-1] == "close"
    assert "super-secret-value" not in json.dumps(closed)
    assert "connection" not in json.dumps(closed)

    def reset_spawn(self, method):
        if method == "spawn":
            raise ConnectionResetError("connection reset")

    reset, transport = run(reset_spawn)
    assert reset == {"ok": False, "error_code": "SCHEMA_MISMATCH"}
    assert transport.methods == ["spawn", "close"]
    assert "reset" not in json.dumps(reset)

    def missing_process(self, method):
        if method == "spawn":
            raise FileNotFoundError("/tmp/grok-missing")

    missing_proc, transport = run(missing_process)
    assert missing_proc == {"ok": False, "error_code": "SCHEMA_MISMATCH"}
    assert "grok-missing" not in json.dumps(missing_proc)
    assert transport.methods[-1] == "close"

    constructed = []

    def factory(**kwargs):
        constructed.append(kwargs)
        raise AssertionError("missing binary must not construct a transport")

    monkeypatch.setattr("asterun.quota.grok_probe.GrokBackend.discover_bin", lambda self: None)
    absent = probe_grok(
        config, scope_ref=SCOPE, now=NOW, timeout_s=10, ttl_s=900, transport_factory=factory,
    )
    assert absent == {"ok": False, "error_code": "SCHEMA_MISMATCH"}
    assert constructed == []


def test_grok_acp_billing_defaults_reject_unknown_and_short_interval(config_path, isolated_env):
    proposed = _proposed(config_path, isolated_env)
    loaded = load_config(_write(isolated_env / "grok-default.json", proposed))
    assert loaded.quota_explicit is False
    assert "quota" not in loaded.to_dict()
    assert loaded.quota["probes"]["grok_acp_billing"] == default_quota()["probes"]["grok_acp_billing"]
    assert loaded.quota["probes"]["grok_acp_billing"] == {
        "enabled": False,
        "acknowledge_undocumented": False,
        "ttl_seconds": 900,
        "min_interval_seconds": 300,
        "timeout_seconds": 10,
    }
    v1 = load_config(config_path)
    v1.quota = {"probes": {"grok_acp_billing": {"enabled": True, "acknowledge_undocumented": True}}}
    assert grok_quota_settings(v1)["probes"]["grok_acp_billing"]["enabled"] is False
    assert grok_quota_settings(v1)["probes"]["grok_acp_billing"]["acknowledge_undocumented"] is False

    unknown = json.loads(json.dumps(proposed))
    unknown["quota"] = {"probes": {"grok_acp_billing": {"enabled": False, "balance": 1}}}
    with pytest.raises(AsterunError) as caught:
        load_config(_write(isolated_env / "grok-unknown.json", unknown))
    assert caught.value.code == UNKNOWN_FIELD

    unknown_probe = json.loads(json.dumps(proposed))
    unknown_probe["quota"] = {"probes": {"grok_billing": {"enabled": True}}}
    with pytest.raises(AsterunError) as caught:
        load_config(_write(isolated_env / "grok-unknown-probe.json", unknown_probe))
    assert caught.value.code == UNKNOWN_FIELD

    low_interval = json.loads(json.dumps(proposed))
    low_interval["quota"] = {"probes": {"grok_acp_billing": {"min_interval_seconds": 59}}}
    with pytest.raises(AsterunError) as caught:
        load_config(_write(isolated_env / "grok-interval.json", low_interval))
    assert caught.value.code == INVALID_CONFIG

    accepted = json.loads(json.dumps(proposed))
    accepted["quota"] = {"probes": {"grok_acp_billing": {"min_interval_seconds": 60}}}
    loaded = load_config(_write(isolated_env / "grok-interval-ok.json", accepted))
    assert loaded.quota_explicit is True
    assert loaded.quota["probes"]["grok_acp_billing"]["min_interval_seconds"] == 60
    assert loaded.quota["probes"]["grok_acp_billing"]["enabled"] is False
    assert "quota" in loaded.to_dict()
