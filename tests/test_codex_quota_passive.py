from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from asterun.application import Application
from asterun.backends.codex import CodexAppServerTransport, CodexSession
from asterun.config import BackendConfig, load_config
from asterun.control_protocol import digest
from asterun.config_migration import migrate_v1_to_v2
from asterun.config_v2 import default_quota
from asterun.contracts import Run, RunStatus
from asterun.errors import AsterunError
from asterun.ids import BackendId, RunId, TaskId
from asterun.plugin_store import PluginStore
from asterun.quota.codex_probe import scrub_rate_limits
from asterun.quota.passive import persist_run_quota
from asterun.quota.service import handle
from asterun.quota.settings import codex_quota_settings
from asterun.sqlite_store import SqliteStore

OBSERVED_AT = "2026-10-19T12:00:00+00:00"
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


def _future() -> int:
    return int((NOW + timedelta(days=5)).timestamp())


def _notice(*, wrapped: bool = False) -> dict:
    payload = {
        "rateLimits": {
            "primary": {
                "usedPercent": 48,
                "windowDurationMins": 10080,
                "resetsAt": _future(),
                "planType": "pro",
            },
            "secondary": None,
        },
        "credits": {"hasCredits": False, "unlimited": False, "balance": "61639.42"},
        "planType": "pro",
        "email": "a@b.c",
        "rateLimitResetCredits": [{"expiresAt": _future(), "balance": "61639.42"}],
    }
    if wrapped:
        return {"rateLimits": payload, "email": "a@b.c", "planType": "pro"}
    return payload


def _session() -> CodexSession:
    return CodexSession(
        session_id="thread-1",
        transport=CodexAppServerTransport(),
        cwd="/tmp/project",
        model="gpt-5.5",
        started_at="2026-10-19T00:00:00+00:00",
    )


def _assert_scrubbed(value) -> None:
    dumped = json.dumps(value)
    assert "61639" not in dumped
    assert "\"pro\"" not in dumped
    assert "@" not in dumped


_FORBIDDEN = ("secret", "@", "balance", "8642097531", "Bearer")


def _assert_no_secrets(value) -> None:
    dumped = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    for needle in _FORBIDDEN:
        assert needle not in dumped


def _malicious_notice() -> dict:
    return {
        "rateLimits": {
            "primary": {
                "usedPercent": {"email": "a@b.c", "token": "secret"},
                "windowDurationMins": 10080,
                "resetsAt": "x@y",
            },
            "secondary": {
                "usedPercent": 10,
                "windowDurationMins": 300,
                "resetsAt": _future(),
            },
        },
        "credits": {"hasCredits": {"balance": 8642097531}, "unlimited": False},
        "rateLimitResetCredits": [
            {"expiresAt": {"k": "v"}},
            {"expires_at": "2026-10-25T00:00:00Z"},
        ],
        "rateLimitReachedType": "Bearer abc",
    }


def test_session_scrubs_rate_limit_notification_without_thread_id():
    session = _session()
    session.handle_message({"method": "account/rateLimits/updated", "params": _notice()})
    status = session.to_status_dict()
    captured = status["quota_rate_limits"]
    assert captured["params"]["rateLimits"]["primary"]["usedPercent"] == 48
    assert captured["observed_at"]
    _assert_scrubbed(captured)
    _assert_scrubbed(status)


def test_scrub_drops_structured_secrets_from_status_events_prompt_and_snapshot(app, tmp_path, monkeypatch):
    monkeypatch.setattr("asterun.backends.codex.time.time", lambda: 1700000000.0)
    notice = _malicious_notice()
    session = _session()
    session.handle_message({"method": "account/rateLimits/updated", "params": notice})
    status = session.to_status_dict()
    _assert_no_secrets(status)
    _assert_no_secrets(status["recent_events"])
    kept = status["quota_rate_limits"]["params"]
    assert kept["rateLimits"]["secondary"]["usedPercent"] == 10
    assert "usedPercent" not in kept["rateLimits"]["primary"]
    assert "resetsAt" not in kept["rateLimits"]["primary"]
    assert "hasCredits" not in kept["credits"]
    assert kept["rateLimitResetCredits"] == [{"expires_at": 1792886400}]
    assert "rateLimitReachedType" not in kept

    class FakePromptTransport(CodexAppServerTransport):
        def on_message(self, cb):
            self.cb = cb

        def start_thread(self, params, timeout=60.0):
            return {"thread": {"id": "thread-1"}}

        def start_turn(self, params, timeout=900.0):
            self.cb({"method": "account/rateLimits/updated", "params": notice})
            self.cb({"method": "turn/completed", "params": {"turn": {"id": "turn-1", "status": "completed"}}})
            return {"turn": {"id": "turn-1"}}

    result = FakePromptTransport().run_text_prompt(text="hello", cwd="/tmp/project", model="gpt-5.5", timeout=1)
    _assert_no_secrets(result.to_dict())
    _assert_no_secrets(result.recent_events)

    store = _prepare(app, tmp_path)
    try:
        assert persist_run_quota(app, _run({"quota_rate_limits": session.quota_rate_limits}, run_id="run-scrub")) == 1
        payloads = [row[0] for row in store._conn.execute("SELECT payload FROM plugin_quota_snapshots")]
        assert len(payloads) == 1
        _assert_no_secrets(payloads)
        _assert_no_secrets("".join(payloads))
    finally:
        store.close()


def test_session_accepts_rate_limits_wrapped_one_level():
    session = _session()
    session.handle_message({"method": "account/rateLimits/updated", "params": _notice(wrapped=True)})
    captured = session.to_status_dict()["quota_rate_limits"]
    assert captured["params"]["rateLimits"]["primary"]["windowDurationMins"] == 10080
    assert "email" not in captured["params"]
    _assert_scrubbed(captured)


def _run(native, *, status=RunStatus.SUCCEEDED, backend="codex", run_id="run-quota-1"):
    return Run(
        RunId(run_id),
        TaskId("task-quota"),
        BackendId(backend),
        status,
        "2026-10-19T00:00:00Z",
        terminated=status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED},
        native=native,
    )


def _native():
    return {
        "quota_rate_limits": {
            "params": scrub_rate_limits(_notice()),
            "observed_at": OBSERVED_AT,
        }
    }


def _prepare(app, tmp_path):
    store = SqliteStore(tmp_path / "quota-state" / "asterun.sqlite")
    app.store = store
    store.bootstrap_applied_revision(app.config.revision)
    app.config.backends["codex"] = BackendConfig(name="codex", kind="codex", enabled=True, bin="/bin/true")
    if hasattr(store, "quota_hook"):
        store.quota_hook = lambda run: persist_run_quota(app, run)
    return store


def test_persist_run_quota_writes_weekly_once(app, tmp_path):
    store = _prepare(app, tmp_path)
    try:
        run = _run(_native())
        assert persist_run_quota(app, run) == 1
        report = handle(app, "quota.inspect", {"backend": "codex"})
        windows = report["scopes"][0]["windows"]
        assert len(windows) == 1
        observation = windows[0]["observation"]
        assert observation["window_kind"] == "weekly"
        assert observation["source_kind"] == "passive_codex"
        assert observation["bucket_id"] == "codex"
        _assert_scrubbed(observation)
        assert persist_run_quota(app, run) == 0
        again = handle(app, "quota.inspect", {"backend": "codex"})
        assert len(again["scopes"][0]["windows"]) == 1
    finally:
        store.close()


def test_persist_run_quota_skips_non_terminal_and_disabled_passive(app, tmp_path, monkeypatch):
    store = _prepare(app, tmp_path)
    try:
        assert persist_run_quota(app, _run(_native(), status=RunStatus.RUNNING, run_id="run-open")) == 0
        assert PluginStore(store._conn).quota_snapshots() == []

        def disabled(config):
            settings = codex_quota_settings(config)
            settings["passive"]["codex_rate_limits_updated"] = False
            return settings

        monkeypatch.setattr("asterun.quota.passive.codex_quota_settings", disabled)
        assert persist_run_quota(app, _run(_native(), run_id="run-off")) == 0
        assert PluginStore(store._conn).quota_snapshots() == []
    finally:
        store.close()


def test_persist_run_quota_swallows_append_errors(app, tmp_path, monkeypatch):
    store = _prepare(app, tmp_path)
    try:
        def boom(self, observation, namespace="ns_local"):
            raise RuntimeError("quota store failed")

        monkeypatch.setattr(PluginStore, "append_quota_snapshot", boom)
        assert persist_run_quota(app, _run(_native(), run_id="run-boom")) == 0
    finally:
        store.close()


def _native_two():
    payload = {
        "rateLimits": {
            "primary": {"usedPercent": 48, "windowDurationMins": 10080, "resetsAt": _future()},
            "secondary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": _future()},
        }
    }
    return {
        "quota_rate_limits": {
            "params": scrub_rate_limits(payload),
            "observed_at": OBSERVED_AT,
        }
    }


def test_persist_run_quota_rolls_back_partial_batch(app, tmp_path, monkeypatch):
    store = _prepare(app, tmp_path)
    original = PluginStore.append_quota_snapshot
    state = {"n": 0, "raise": True}

    def flaky(self, observation, namespace="ns_local"):
        state["n"] += 1
        if state["raise"] and state["n"] == 2:
            raise RuntimeError("second window failed")
        return original(self, observation, namespace)

    monkeypatch.setattr(PluginStore, "append_quota_snapshot", flaky)
    run = _run(_native_two(), run_id="run-atomic")
    try:
        assert persist_run_quota(app, run) == 0
        assert store._conn.execute("SELECT payload FROM plugin_quota_snapshots").fetchall() == []
        state["raise"] = False
        assert persist_run_quota(app, run) == 2
        saved = PluginStore(store._conn).quota_snapshots()
        assert len(saved) == 2
        assert {item["window_kind"] for item in saved} == {"weekly", "5h"}
        assert len({item["raw_digest"] for item in saved}) == 1
    finally:
        store.close()


def _write(path, raw):
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def test_v2_quota_defaults_reject_unknown_and_out_of_range(config_path, isolated_env):
    raw = json.loads(config_path.read_text())
    raw["backends"]["codex"] = {"kind": "codex", "enabled": True, "bin": "/bin/true"}
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    proposed = migrate_v1_to_v2(config_path)["config"]
    path = isolated_env / "quota-v2.json"
    config = load_config(_write(path, proposed))
    assert config.quota == default_quota()
    assert config.quota_explicit is False
    projected = config.to_dict()
    assert "quota" not in projected
    assert digest(projected) == digest({key: value for key, value in projected.items() if key != "quota"})
    assert codex_quota_settings(load_config(config_path)) == default_quota()
    config.quota = None
    assert codex_quota_settings(config) == default_quota()

    unknown = json.loads(json.dumps(proposed))
    unknown["quota"] = {"probes": {"codex_app_server": {"enabled": True}}, "balance": "61639.42"}
    with pytest.raises(AsterunError):
        load_config(_write(isolated_env / "quota-unknown.json", unknown))

    for bad in (59, 3601):
        out_of_range = json.loads(json.dumps(proposed))
        out_of_range["quota"] = {"probes": {"codex_app_server": {"ttl_seconds": bad}}}
        with pytest.raises(AsterunError):
            load_config(_write(isolated_env / "quota-range.json", out_of_range))
    low_interval = json.loads(json.dumps(proposed))
    low_interval["quota"] = {"probes": {"codex_app_server": {"min_interval_seconds": 59}}}
    with pytest.raises(AsterunError):
        load_config(_write(isolated_env / "quota-interval.json", low_interval))
    high_interval = json.loads(json.dumps(proposed))
    high_interval["quota"] = {"probes": {"codex_app_server": {"min_interval_seconds": 3601}}}
    with pytest.raises(AsterunError):
        load_config(_write(isolated_env / "quota-interval-high.json", high_interval))
    accepted = json.loads(json.dumps(proposed))
    accepted["quota"] = {"probes": {"codex_app_server": {"min_interval_seconds": 60}}}
    loaded = load_config(_write(isolated_env / "quota-interval-ok.json", accepted))
    assert loaded.quota_explicit is True
    assert loaded.quota["probes"]["codex_app_server"]["min_interval_seconds"] == 60
    assert loaded.to_dict()["quota"]["probes"]["codex_app_server"]["min_interval_seconds"] == 60
    top = json.loads(json.dumps(proposed))
    top["quota"] = {"probes": {"codex_app_server": {"min_interval_seconds": 3600}}}
    assert load_config(_write(isolated_env / "quota-interval-top.json", top)).quota["probes"]["codex_app_server"]["min_interval_seconds"] == 3600
    bad_timeout = json.loads(json.dumps(proposed))
    bad_timeout["quota"] = {"probes": {"codex_app_server": {"timeout_seconds": 31}}}
    with pytest.raises(AsterunError):
        load_config(_write(isolated_env / "quota-timeout.json", bad_timeout))


def test_disabled_probe_does_not_call_codex(config_path, isolated_env, monkeypatch):
    raw = json.loads(config_path.read_text())
    raw["backends"]["codex"] = {"kind": "codex", "enabled": True, "bin": "/bin/true"}
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    proposed = migrate_v1_to_v2(config_path)["config"]
    proposed["quota"] = {"probes": {"codex_app_server": {"enabled": False}}}
    path = _write(isolated_env / "quota-disabled.json", proposed)
    calls = []
    monkeypatch.setattr(
        "asterun.quota.service.probe_codex",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"ok": True, "observations": []},
    )
    opened = Application.from_paths(path, isolated_env / "quota-disabled-state")
    try:
        refreshed = handle(opened, "quota.refresh", {"backend": "codex"})
        assert calls == []
        assert refreshed["scopes"] == [{
            "scope_ref": refreshed["scopes"][0]["scope_ref"],
            "probe_id": "codex.app_server.rate_limits",
            "ok": False,
            "error_code": "PROBE_DISABLED",
            "written": 0,
            "skipped": None,
        }]
        assert refreshed["scopes"][0]["scope_ref"].startswith("pool_")
    finally:
        opened.close()
