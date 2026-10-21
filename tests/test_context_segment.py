"""段计划与 grok 段回合上限。build_grok_code_command 的参数形状保持不变。"""
from __future__ import annotations

from types import SimpleNamespace

from asterun.backends.grok import DEFAULT_CODE_MAX_TURNS, _segment_turns
from asterun.context_budget import segment as segment_mod
from asterun.context_budget.segment import plan


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


def _app(kind="fake", max_turns=None):
    return SimpleNamespace(
        config=SimpleNamespace(get_backend=lambda name: SimpleNamespace(kind=kind, max_turns=max_turns)),
    )


def _run(role="implementation", backend="fake", native=None):
    return SimpleNamespace(role=role, backend=SimpleNamespace(value=backend), native={} if native is None else native)


def test_plan_off_and_observe_return_none(monkeypatch):
    for mode in ("off", "observe"):
        _bind(monkeypatch, _settings(mode=mode))
        assert plan(_app(), SimpleNamespace(), _run()) is None


def test_plan_enforce_empty_backends_returns_none(monkeypatch):
    _bind(monkeypatch, _settings(backends=[]))
    assert plan(_app(), SimpleNamespace(), _run()) is None


def test_plan_fake_profile_returns_segment(monkeypatch):
    _bind(monkeypatch, _settings(segment_turns=5, max_segments=4))
    assert plan(_app(), SimpleNamespace(), _run()) == {
        "index": 0,
        "max_turns": 5,
        "profile_key": "fake/implementation",
        "max_segments": 4,
    }


def test_plan_segment_false_returns_none(monkeypatch):
    _bind(monkeypatch, _settings(segment=False))
    assert plan(_app(), SimpleNamespace(), _run()) is None


def test_plan_review_role_returns_none(monkeypatch):
    _bind(monkeypatch, _settings(roles=["implementation", "repair", "review"]))
    assert plan(_app(), SimpleNamespace(), _run(role="review")) is None


def test_plan_existing_segment_returned_as_is(monkeypatch):
    existing = {"index": 2, "max_turns": 9, "profile_key": "fake/implementation", "max_segments": 1}
    _bind(monkeypatch, _settings(segment_turns=5))
    run = _run(native={"segment": existing})
    assert plan(_app(), SimpleNamespace(), run) is existing


def test_plan_existing_segment_ignored_when_inactive(monkeypatch):
    existing = {"index": 1, "max_turns": 3, "profile_key": "fake/implementation", "max_segments": 1}
    _bind(monkeypatch, _settings(mode="off"))
    assert plan(_app(), SimpleNamespace(), _run(native={"segment": existing})) is None
    _bind(monkeypatch, _settings(backends=[]))
    assert plan(_app(), SimpleNamespace(), _run(native={"segment": existing})) is None


def test_plan_fake_ignores_backend_max_turns(monkeypatch):
    _bind(monkeypatch, _settings(segment_turns=5))
    result = plan(_app(kind="fake", max_turns=2), SimpleNamespace(), _run())
    assert result["max_turns"] == 5


def test_plan_grok_caps_at_backend_or_default(monkeypatch):
    capped = _settings(backends=["grok"], profiles={"grok/implementation": {"segment_turns": 99, "max_segments": 2}})
    _bind(monkeypatch, capped)
    capped_result = plan(_app(kind="grok", max_turns=40), SimpleNamespace(), _run(backend="grok"))
    assert capped_result == {
        "index": 0,
        "max_turns": 40,
        "profile_key": "grok/implementation",
        "max_segments": 2,
    }
    defaulted = _settings(
        backends=["grok"],
        profiles={"grok/implementation": {"segment_turns": 100, "max_segments": None}},
    )
    _bind(monkeypatch, defaulted)
    assert plan(_app(kind="grok", max_turns=None), SimpleNamespace(), _run(backend="grok"))["max_turns"] == DEFAULT_CODE_MAX_TURNS
    shorter = _settings(backends=["grok"], profiles={"grok/implementation": {"segment_turns": 5}})
    _bind(monkeypatch, shorter)
    assert plan(_app(kind="grok", max_turns=40), SimpleNamespace(), _run(backend="grok"))["max_turns"] == 5


def test_plan_skips_other_kinds_and_missing_turns(monkeypatch):
    _bind(monkeypatch, _settings())
    assert plan(_app(kind="codex"), SimpleNamespace(), _run()) is None
    _bind(monkeypatch, _settings(profiles={"fake/implementation": {"task_hard": 10}}))
    assert plan(_app(), SimpleNamespace(), _run()) is None
    _bind(monkeypatch, _settings(backends=["grok"]))
    assert plan(_app(), SimpleNamespace(), _run()) is None


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
