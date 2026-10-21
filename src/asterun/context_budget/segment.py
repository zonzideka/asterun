"""把一次派发计划成有限回合的段。未启用时返回 None，不访问数据库。"""

from __future__ import annotations

from asterun.backends.grok import DEFAULT_CODE_MAX_TURNS
from asterun.config_v2 import context_budget_settings
from asterun.context_budget.observe import _matched_profile

SEGMENT_KINDS = frozenset({"grok", "fake"})  # 本 PR 只实现 grok；fake 用于离线测试


def plan(app, task, run) -> dict | None:
    """enforce 且后端在 segmentation.backends 白名单、角色在 segmentation.roles、kind 属于 SEGMENT_KINDS、
    匹配 profile 有 segment_turns 且 profile.get("segment") 不是 False 时，返回段计划；否则 None（不访问数据库）。"""
    del task
    settings = context_budget_settings(getattr(app, "config", None))
    if not isinstance(settings, dict) or settings.get("mode") != "enforce":
        return None
    segmentation = settings.get("segmentation") if isinstance(settings.get("segmentation"), dict) else {}
    backends = segmentation.get("backends")
    if not isinstance(backends, list) or not backends:
        return None
    role = getattr(run, "role", None) or "implementation"
    if role == "review":
        return None
    roles = segmentation.get("roles", ["implementation", "repair"])
    if not isinstance(roles, list) or role not in roles:
        return None
    backend_ref = getattr(run, "backend", None)
    backend_name = backend_ref.value if hasattr(backend_ref, "value") else backend_ref
    if not isinstance(backend_name, str) or backend_name not in backends:
        return None
    backend = app.config.get_backend(backend_name)
    kind = backend.kind
    if kind not in SEGMENT_KINDS:
        return None
    profiles = settings.get("profiles") if isinstance(settings.get("profiles"), dict) else {}
    profile_key, _role = _matched_profile(app, backend_name, role, profiles)
    profile = profiles.get(profile_key)
    if not isinstance(profile, dict) or "segment_turns" not in profile or profile.get("segment") is False:
        return None
    native = getattr(run, "native", None)
    if isinstance(native, dict) and native.get("segment") is not None:
        return native["segment"]
    segment_turns = profile["segment_turns"]
    if kind == "fake":
        max_turns = segment_turns
    else:
        max_turns = min(segment_turns, backend.max_turns or DEFAULT_CODE_MAX_TURNS)
    return {
        "index": 0,
        "max_turns": max_turns,
        "profile_key": profile_key,
        "max_segments": profile.get("max_segments"),
    }
