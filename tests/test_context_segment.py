"""段计划与 grok 段回合上限。build_grok_code_command 的参数形状保持不变。"""
from __future__ import annotations

from types import SimpleNamespace

from asterun.application import _segment_session_native
from asterun.backends.grok import DEFAULT_CODE_MAX_TURNS, _segment_turns
from asterun.context_budget import segment as segment_mod
from asterun.context_budget.segment import MARKER, plan, resume_decision, segment_end
from asterun.contracts import Event, EventType, RunStatus
from asterun.grok_profile import CODE_PROFILE, PROFILE
from asterun.usage.breakdown import normalize_breakdown


def _settings(*, mode="enforce", backends=None, roles=None, profiles=None, segment_turns=5, segment=None, max_segments=4):
    if backends is None:
        backends = ["fake"]
    if roles is None:
        roles = ["implementation", "repair"]
    if profiles is None:
        profile = {"segment_turns": segment_turns, "max_segments": max_segments}
        if segment is not None:
            profile["segment"] = segment
        profiles = {"fake/implementation": profile}
    return {
        "mode": mode,
        "segmentation": {"backends": backends, "roles": roles},
        "profiles": profiles,
    }


def _bind(monkeypatch, settings, matched=None):
    monkeypatch.setattr(segment_mod, "context_budget_settings", lambda config: settings)

    def match(app, backend, role, profiles):
        if matched is not None:
            return matched
        return (f"{backend}/{role}", role)

    monkeypatch.setattr(segment_mod, "_matched_profile", match)


def _app(kind="fake", max_turns=None, execution_profile=None):
    if execution_profile is None and kind == "grok":
        execution_profile = CODE_PROFILE
    backend = SimpleNamespace(kind=kind, max_turns=max_turns, execution_profile=execution_profile)
    return SimpleNamespace(
        config=SimpleNamespace(get_backend=lambda name: backend),
        store=SimpleNamespace(
            list_runs=lambda task_id: [],
            find_operation_for_run=lambda run_id: None,
        ),
    )


def _task():
    return SimpleNamespace(id=SimpleNamespace(value="tsk_segment"))


def _run(role="implementation", backend="fake", native=None):
    return SimpleNamespace(
        id=SimpleNamespace(value="run_segment"),
        role=role,
        backend=SimpleNamespace(value=backend),
        native={} if native is None else native,
    )


def test_plan_off_and_observe_return_none(monkeypatch):
    for mode in ("off", "observe"):
        _bind(monkeypatch, _settings(mode=mode))
        assert plan(_app(), _task(), _run()) is None


def test_plan_enforce_empty_backends_returns_none(monkeypatch):
    _bind(monkeypatch, _settings(backends=[]))
    assert plan(_app(), _task(), _run()) is None


def test_plan_fake_profile_returns_segment(monkeypatch):
    _bind(monkeypatch, _settings(segment_turns=5, max_segments=4))
    assert plan(_app(), _task(), _run()) == {
        "index": 0,
        "max_turns": 5,
        "profile_key": "fake/implementation",
        "max_segments": 4,
        "trigger": "turn_limit",
    }


def test_plan_segment_false_returns_none(monkeypatch):
    _bind(monkeypatch, _settings(segment=False))
    assert plan(_app(), _task(), _run()) is None


def test_plan_review_role_returns_none(monkeypatch):
    _bind(monkeypatch, _settings(roles=["implementation", "repair", "review"]))
    assert plan(_app(), _task(), _run(role="review")) is None


def test_plan_existing_segment_returned_as_is(monkeypatch):
    existing = {"index": 2, "max_turns": 9, "profile_key": "fake/implementation", "max_segments": 1}
    _bind(monkeypatch, _settings(segment_turns=5))
    run = _run(native={"segment": existing})
    assert plan(_app(), _task(), run) is existing


def test_plan_existing_segment_ignored_when_inactive(monkeypatch):
    existing = {"index": 1, "max_turns": 3, "profile_key": "fake/implementation", "max_segments": 1}
    _bind(monkeypatch, _settings(mode="off"))
    assert plan(_app(), _task(), _run(native={"segment": existing})) is None
    _bind(monkeypatch, _settings(backends=[]))
    assert plan(_app(), _task(), _run(native={"segment": existing})) is None


def test_plan_fake_ignores_backend_max_turns(monkeypatch):
    _bind(monkeypatch, _settings(segment_turns=5))
    result = plan(_app(kind="fake", max_turns=2), _task(), _run())
    assert result["max_turns"] == 5


def test_plan_text_only_grok_returns_none(monkeypatch):
    settings = _settings(
        backends=["grok"],
        profiles={"grok/implementation": {"segment_turns": 5, "max_segments": 2}},
    )
    _bind(monkeypatch, settings)
    run = _run(backend="grok")
    assert plan(_app(kind="grok", execution_profile=PROFILE), _task(), run) is None
    configured = _app(kind="grok", execution_profile=CODE_PROFILE)
    configured.backends = {"grok": SimpleNamespace(execution_profile=PROFILE)}
    assert plan(configured, _task(), run) is None
    configured.backends = {"grok": SimpleNamespace(execution_profile=CODE_PROFILE)}
    assert plan(configured, _task(), run)["max_turns"] == 5


def test_plan_grok_caps_at_backend_or_default(monkeypatch):
    capped = _settings(backends=["grok"], profiles={"grok/implementation": {"segment_turns": 99, "max_segments": 2}})
    _bind(monkeypatch, capped)
    capped_result = plan(_app(kind="grok", max_turns=40), _task(), _run(backend="grok"))
    assert capped_result == {
        "index": 0,
        "max_turns": 40,
        "profile_key": "grok/implementation",
        "max_segments": 2,
        "trigger": "turn_limit",
    }
    defaulted = _settings(
        backends=["grok"],
        profiles={"grok/implementation": {"segment_turns": 100, "max_segments": None}},
    )
    _bind(monkeypatch, defaulted)
    assert plan(_app(kind="grok", max_turns=None), _task(), _run(backend="grok"))["max_turns"] == DEFAULT_CODE_MAX_TURNS
    shorter = _settings(backends=["grok"], profiles={"grok/implementation": {"segment_turns": 5}})
    _bind(monkeypatch, shorter)
    assert plan(_app(kind="grok", max_turns=40), _task(), _run(backend="grok"))["max_turns"] == 5


def test_plan_skips_other_kinds_and_missing_turns(monkeypatch):
    _bind(monkeypatch, _settings())
    assert plan(_app(kind="codex"), _task(), _run()) is None
    _bind(monkeypatch, _settings(profiles={"fake/implementation": {"task_hard": 10}}))
    assert plan(_app(), _task(), _run()) is None
    _bind(monkeypatch, _settings(backends=["grok"]))
    assert plan(_app(), _task(), _run()) is None


def test_segment_turns_respects_limit_and_rejects_non_positive_ints():
    assert _segment_turns(None, 40) == 40
    assert _segment_turns({}, 40) == 40
    assert _segment_turns({"segment_max_turns": None}, 40) == 40
    assert _segment_turns({"segment_max_turns": 3}, 40) == 3
    assert _segment_turns({"segment_max_turns": 99}, 40) == 40
    assert _segment_turns({"segment_max_turns": True}, 40) == 40
    assert _segment_turns({"segment_max_turns": False}, 40) == 40
    assert _segment_turns({"segment_max_turns": 0}, 40) == 40
    assert _segment_turns({"segment_max_turns": "3"}, 40) == 40
    assert _segment_turns({"segment_max_turns": 3}, 40) == 3


def test_plan_inactive_does_not_query_store(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("plan queried the store")

    app = _app()
    app.store.list_runs = boom
    app.store.find_operation_for_run = boom
    _bind(monkeypatch, _settings(mode="off"))
    assert plan(app, _task(), _run()) is None
    _bind(monkeypatch, _settings(mode="observe"))
    assert plan(app, _task(), _run()) is None
    _bind(monkeypatch, _settings(backends=[]))
    assert plan(app, _task(), _run()) is None


def test_plan_rcp_operation_returns_none(monkeypatch):
    _bind(monkeypatch, _settings())
    app = _app()
    monkeypatch.setattr(app.store, "find_operation_for_run", lambda run_id: SimpleNamespace(idempotency_key="rcp_x"))
    assert plan(app, _task(), _run()) is None


def test_plan_quality_control_operation_returns_none(monkeypatch):
    _bind(monkeypatch, _settings())
    app = _app()
    monkeypatch.setattr(
        app.store,
        "find_operation_for_run",
        lambda run_id: SimpleNamespace(idempotency_key="idem_plain"),
    )
    app.capabilities = SimpleNamespace(quality_control_for_operation=lambda operation: {"desired_state": "RUNNING"})
    assert plan(app, _task(), _run()) is None


def test_plan_operation_lookup_error_returns_none(monkeypatch):
    _bind(monkeypatch, _settings())
    app = _app()

    def find(_run_id):
        raise RuntimeError("store down")

    monkeypatch.setattr(app.store, "find_operation_for_run", find)
    assert plan(app, _task(), _run()) is None


def _grok_usage_native(*, session_id="sess-1"):
    return {
        "session_id": session_id,
        "usage_source": "native_headless_report",
        "usage": {
            "input_tokens": 10,
            "cache_read_input_tokens": 20,
            "cache_creation_input_tokens": 0,
            "output_tokens": 5,
        },
    }


def _stored_usage(input_tokens, cache_read, cache_write, *, message_id="m1"):
    usage = {}
    if input_tokens is not None:
        usage["input_tokens"] = input_tokens
    if cache_read is not None:
        usage["cache_read_input_tokens"] = cache_read
    if cache_write is not None:
        usage["cache_creation_input_tokens"] = cache_write
    return Event(
        seq=1,
        type=EventType.MESSAGE,
        source="backend.grok",
        timestamp="2026-01-01T00:00:00Z",
        schema_version=1,
        payload={"kind": "usage", "usage": usage, "message_id": message_id},
    )


def test_resume_decision_known_low_peak_resumes(monkeypatch):
    _bind(monkeypatch, _settings())
    monkeypatch.setattr(segment_mod, "_events", lambda store, run_id: [_stored_usage(100, 200, 300)])
    decision = resume_decision(
        _app(kind="grok"), _task(), _run(backend="grok", native=_grok_usage_native()), {},
    )
    assert decision == "resume"


def test_resume_decision_high_or_partial_peak_is_new_session(monkeypatch):
    _bind(monkeypatch, _settings())
    app = _app(kind="grok")
    run = _run(backend="grok", native=_grok_usage_native())
    monkeypatch.setattr(segment_mod, "_events", lambda store, run_id: [_stored_usage(90_000, 20_000, 0)])
    assert resume_decision(app, _task(), run, {}) == "new_session"
    monkeypatch.setattr(segment_mod, "_events", lambda store, run_id: [_stored_usage(100, 200, None)])
    assert resume_decision(app, _task(), run, {}) == "new_session"


def test_resume_decision_without_events_is_new_session(monkeypatch):
    _bind(monkeypatch, _settings())
    monkeypatch.setattr(segment_mod, "_events", lambda store, run_id: [])
    decision = resume_decision(
        _app(kind="grok"), _task(), _run(backend="grok", native=_grok_usage_native()), {},
    )
    assert decision == "new_session"


def test_resume_decision_non_grok_is_new_session(monkeypatch):
    _bind(monkeypatch, _settings())

    def boom(store, run_id):
        raise AssertionError("non-grok resume_decision read events")

    monkeypatch.setattr(segment_mod, "_events", boom)
    decision = resume_decision(_app(kind="fake"), _task(), _run(native=_grok_usage_native()), {})
    assert decision == "new_session"


def test_segment_session_native_copies_source_session_id():
    def decide(source_native):
        source = SimpleNamespace(native=source_native)
        run = SimpleNamespace(native={"segment_resume": "resume", "segment_source_run_id": "run_source"})
        return _segment_session_native(SimpleNamespace(get_run=lambda run_id: source), run)

    assert decide({"session_id": "sess-grok"}) == {"backend_session_id": "sess-grok", "turn_id": None}
    assert decide({"backend_session_id": "backend-sess", "session_id": "sess-grok"}) == {
        "backend_session_id": "backend-sess",
        "turn_id": None,
    }
    assert decide({"backend_session_id": "  ", "session_id": "sess-grok"})["backend_session_id"] == "sess-grok"
    assert decide({}) == {"backend_session_id": None, "turn_id": None}
    assert decide({"session_id": ""}) == {"backend_session_id": None, "turn_id": None}


def _marker_profiles(kind, *, segment_soft=80, segment=None, max_segments=4):
    profile = {"segment_soft": segment_soft, "max_segments": max_segments}
    if segment is not None:
        profile["segment"] = segment
    return {f"{kind}/implementation": profile, f"{kind}/repair": dict(profile), f"{kind}/review": dict(profile)}


def test_plan_codex_marker_only_for_implementation(monkeypatch):
    _bind(monkeypatch, _settings(
        backends=["codex"],
        roles=["implementation", "repair", "review"],
        profiles=_marker_profiles("codex"),
    ))
    app = _app(kind="codex")
    assert plan(app, _task(), _run(backend="codex")) == {
        "index": 0,
        "max_turns": None,
        "profile_key": "codex/implementation",
        "max_segments": 4,
        "trigger": "marker",
        "segment_soft": 80,
    }
    assert plan(app, _task(), _run(backend="codex", role="repair")) is None
    assert plan(app, _task(), _run(backend="codex", role="review")) is None
    reviewed = _task()
    reviewed.external_require_review = True
    assert plan(app, reviewed, _run(backend="codex")) is None
    _bind(monkeypatch, _settings(
        backends=["codex"],
        profiles={"codex/implementation": {"segment_turns": 5, "max_segments": 4}},
    ))
    assert plan(app, _task(), _run(backend="codex")) is None
    _bind(monkeypatch, _settings(
        backends=["codex"],
        profiles={"codex/implementation": {"segment_soft": 80, "segment": False}},
    ))
    assert plan(app, _task(), _run(backend="codex")) is None


def test_plan_antigravity_requires_explicit_segment(monkeypatch):
    app = _app(kind="antigravity")
    run = _run(backend="antigravity")
    _bind(monkeypatch, _settings(
        backends=["antigravity"],
        profiles=_marker_profiles("antigravity", segment_soft=50, max_segments=2),
    ))
    assert plan(app, _task(), run) is None
    _bind(monkeypatch, _settings(
        backends=["antigravity"],
        profiles=_marker_profiles("antigravity", segment_soft=50, segment=True, max_segments=2),
    ))
    assert plan(app, _task(), run) == {
        "index": 0,
        "max_turns": None,
        "profile_key": "antigravity/implementation",
        "max_segments": 2,
        "trigger": "marker",
        "segment_soft": 50,
    }
    reviewed = _task()
    reviewed.external_require_review = True
    assert plan(app, reviewed, run) is None


def test_plan_external_antigravity_plugin_kind_segments(monkeypatch):
    app = _app(kind="google.antigravity-cli")
    run = _run(backend="google.antigravity-cli")
    _bind(monkeypatch, _settings(
        backends=["google.antigravity-cli"],
        profiles=_marker_profiles("google.antigravity-cli", segment_soft=50, max_segments=2),
    ))
    assert plan(app, _task(), run) is None
    _bind(monkeypatch, _settings(
        backends=["google.antigravity-cli"],
        profiles=_marker_profiles("google.antigravity-cli", segment_soft=50, segment=True, max_segments=2),
    ))
    assert plan(app, _task(), run) == {
        "index": 0,
        "max_turns": None,
        "profile_key": "google.antigravity-cli/implementation",
        "max_segments": 2,
        "trigger": "marker",
        "segment_soft": 50,
    }


def _antigravity_usage_native(**extra):
    native = {
        "asterun.antigravity_dispatch_v1": {"stage": "result"},
        "usage_scope": "session",
        "usage": {
            "input_tokens": 100,
            "output_tokens": 10,
            "cache_read_tokens": 0,
            "total_tokens": 110,
        },
        "segment": {
            "index": 0,
            "max_turns": None,
            "trigger": "marker",
            "segment_soft": 80,
        },
    }
    native.update(extra)
    return native


def test_segment_end_antigravity_missing_subagent_count_is_false():
    native = _antigravity_usage_native(plugin_id="google.antigravity-cli")
    native.pop("asterun.antigravity_dispatch_v1")
    assert "subagent_count" not in native
    picture = normalize_breakdown(native)
    assert picture["convention"] != "antigravity"
    run = SimpleNamespace(status=RunStatus.SUCCEEDED, summary="本段到此。\n" + MARKER, native=native)
    assert segment_end(run) is False
    shaped = _antigravity_usage_native()
    assert "subagent_count" not in shaped
    assert normalize_breakdown(shaped)["convention"] == "antigravity"
    assert segment_end(SimpleNamespace(status=RunStatus.SUCCEEDED, summary="本段到此。\n" + MARKER, native=shaped)) is False


def test_segment_end_antigravity_zero_subagents_continues():
    native = _antigravity_usage_native(subagent_count=0)
    assert normalize_breakdown(native)["convention"] == "antigravity"
    run = SimpleNamespace(status=RunStatus.SUCCEEDED, summary="本段到此。\n" + MARKER, native=native)
    assert segment_end(run) is True
    delegated = _antigravity_usage_native(subagent_count=1)
    assert segment_end(SimpleNamespace(status=RunStatus.SUCCEEDED, summary="本段到此。\n" + MARKER, native=delegated)) is False


def _processed_native(processed, **extra):
    native = {
        "usage_source": "native_headless_report",
        "usage": {
            "input_tokens": processed,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": processed,
        },
    }
    native.update(extra)
    return native


def _marker_end_run(*, status=RunStatus.SUCCEEDED, summary=None, processed=100, soft=80, subagent_count=0):
    native = _processed_native(processed, segment={
        "index": 0,
        "max_turns": None,
        "trigger": "marker",
        "segment_soft": soft,
    })
    if subagent_count:
        native["subagent_count"] = subagent_count
    if summary is None:
        summary = "本段到此。\n" + MARKER
    return SimpleNamespace(status=status, summary=summary, native=native)


def test_segment_end_marker_when_processed_reaches_soft():
    assert segment_end(_marker_end_run(processed=100, soft=80)) is True
    assert segment_end(_marker_end_run(processed=80, soft=80)) is True


def test_segment_end_marker_rejects_incomplete_runs():
    assert segment_end(_marker_end_run(summary="做完了，没有标记")) is False
    assert segment_end(_marker_end_run(processed=10, soft=80)) is False
    assert segment_end(_marker_end_run(subagent_count=2)) is False
    assert segment_end(_marker_end_run(status=RunStatus.FAILED, summary="失败\n" + MARKER)) is False


def test_segment_end_grok_turn_limit_unchanged():
    native = {
        "segment": {"index": 0, "max_turns": 5, "trigger": "turn_limit"},
        "provider_failure": {"kind": "turn_limit"},
    }
    assert segment_end(SimpleNamespace(status=RunStatus.FAILED, summary="turn limit", native=native)) is True
    legacy = {
        "segment": {"index": 0, "max_turns": 5},
        "provider_failure": {"kind": "turn_limit"},
    }
    assert segment_end(SimpleNamespace(status=RunStatus.FAILED, summary="turn limit", native=legacy)) is True
    rejected = {
        "segment": {"index": 0, "max_turns": 5, "trigger": "turn_limit"},
        "provider_failure": {"kind": "rejected"},
    }
    assert segment_end(SimpleNamespace(status=RunStatus.FAILED, summary="no", native=rejected)) is False
    assert segment_end(SimpleNamespace(status=RunStatus.SUCCEEDED, summary=MARKER, native=native)) is False
    assert segment_end(SimpleNamespace(status=RunStatus.FAILED, summary="", native={})) is False


def test_plan_external_require_review_returns_none_for_grok_and_fake(monkeypatch):
    reviewed = _task()
    reviewed.external_require_review = True
    _bind(monkeypatch, _settings())
    app = _app(kind="fake")
    assert plan(app, _task(), _run()) is not None
    assert plan(app, reviewed, _run()) is None
    _bind(monkeypatch, _settings(
        backends=["grok"],
        profiles={"grok/implementation": {"segment_turns": 5, "max_segments": 2}},
    ))
    app = _app(kind="grok")
    run = _run(backend="grok")
    assert plan(app, _task(), run) is not None
    assert plan(app, reviewed, run) is None


def _codex_thread_cumulative_native(*, fresh=False):
    native = {
        "token_usage": {
            "tokenUsage": {
                "total": {
                    "inputTokens": 800_000,
                    "cachedInputTokens": 100_000,
                    "outputTokens": 50_000,
                    "reasoningOutputTokens": 1_000,
                    "totalTokens": 850_000,
                },
                "last": {
                    "inputTokens": 20,
                    "cachedInputTokens": 10,
                    "outputTokens": 5,
                    "reasoningOutputTokens": 0,
                    "totalTokens": 25,
                },
            },
        },
        "segment": {
            "index": 0,
            "max_turns": None,
            "trigger": "marker",
            "segment_soft": 80,
        },
    }
    if fresh:
        native["segment_fresh_thread"] = True
    return native


def test_segment_end_marker_rejects_unattributable_codex_thread_cumulative():
    """total 是线程累计且很大，last 很小；没有新开线程标记时即使回复带 MARKER 也不续段。"""
    native = _codex_thread_cumulative_native()
    picture = normalize_breakdown(native)
    assert picture["convention"] == "codex_app_server"
    assert picture["scope"] == "thread_cumulative"
    assert picture["attributable"] is False
    assert picture["processed"] >= 80
    assert "segment_fresh_thread" not in native
    run = SimpleNamespace(status=RunStatus.SUCCEEDED, summary="本段到此。\n" + MARKER, native=native)
    assert segment_end(run) is False


def test_segment_end_marker_accepts_fresh_codex_thread_cumulative():
    """本 run 新开线程时 thread_cumulative 等于本段用量，MARKER 且 processed≥soft 则续段。"""
    native = _codex_thread_cumulative_native(fresh=True)
    picture = normalize_breakdown(native)
    assert picture["convention"] == "codex_app_server"
    assert picture["scope"] == "thread_cumulative"
    assert picture["attributable"] is False
    assert picture["processed"] >= 80
    assert native["segment_fresh_thread"] is True
    run = SimpleNamespace(status=RunStatus.SUCCEEDED, summary="本段到此。\n" + MARKER, native=native)
    assert segment_end(run) is True
