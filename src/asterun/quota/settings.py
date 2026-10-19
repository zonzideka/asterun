"""Codex 额度探针与被动采集的有效配置。v1 和缺省段使用同一组默认值。"""

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
