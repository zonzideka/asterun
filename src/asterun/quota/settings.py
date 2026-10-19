"""Codex / Grok 额度探针与被动采集的有效配置。v1 和缺省段使用同一组默认值。"""

from __future__ import annotations

from copy import deepcopy

from asterun.config_v2 import default_quota


def codex_quota_settings(config) -> dict:
    settings = default_quota()
    if getattr(config, "schema_version", 1) != 2:
        return settings
    raw = getattr(config, "quota", None)
    # 未写 quota 段时配置对象上可以是 None；调用方仍拿到同一组默认值。
    if raw is None or not isinstance(raw, dict) or not raw:
        return settings
    probe_in = raw.get("probes") if isinstance(raw.get("probes"), dict) else {}
    probe_in = probe_in.get("codex_app_server") if isinstance(probe_in.get("codex_app_server"), dict) else {}
    probe = settings["probes"]["codex_app_server"]
    for key in ("enabled", "ttl_seconds", "min_interval_seconds", "timeout_seconds"):
        if key in probe_in:
            probe[key] = probe_in[key]
    passive_in = raw.get("passive") if isinstance(raw.get("passive"), dict) else {}
    if "codex_rate_limits_updated" in passive_in:
        settings["passive"]["codex_rate_limits_updated"] = passive_in["codex_rate_limits_updated"]
    return deepcopy(settings)


def grok_quota_settings(config) -> dict:
    """Grok `_x.ai/billing` 探针配置。v1 固定关闭，不读取 quota 段。"""
    settings = default_quota()
    probe = settings["probes"]["grok_acp_billing"]
    if getattr(config, "schema_version", 1) != 2:
        probe["enabled"] = False
        probe["acknowledge_undocumented"] = False
        return settings
    raw = getattr(config, "quota", None)
    if raw is None or not isinstance(raw, dict) or not raw:
        return settings
    probes = raw.get("probes") if isinstance(raw.get("probes"), dict) else {}
    probe_in = probes.get("grok_acp_billing") if isinstance(probes.get("grok_acp_billing"), dict) else {}
    for key in ("enabled", "acknowledge_undocumented", "ttl_seconds", "min_interval_seconds", "timeout_seconds"):
        if key in probe_in:
            probe[key] = probe_in[key]
    return deepcopy(settings)


def quota_preference_mode(config) -> str:
    """只读。v1、未配置 quota_preference、未写 mode、enabled=false 都是 off。不写回配置。"""
    if getattr(config, "schema_version", 1) != 2:
        return "off"
    routing = getattr(config, "routing", None)
    if not isinstance(routing, dict):
        return "off"
    pref = routing.get("quota_preference")
    if not isinstance(pref, dict) or pref.get("enabled") is False:
        return "off"
    mode = pref.get("mode", "off")
    if mode not in {"off", "dry_run", "on"}:
        return "off"
    return mode
