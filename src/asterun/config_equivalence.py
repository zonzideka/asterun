"""Which config changes leave existing tasks executable (execution-equivalent)."""
from __future__ import annotations

import hashlib
import json
from typing import Any

# Pure scheduling capacity. max_runs_per_task is NOT here: it changes task semantics.
NON_EXECUTION_SCHEDULER_KEYS = frozenset({"max_queue", "per_backend_concurrency", "max_concurrency", "local_check_concurrency"})


def execution_digest(config: Any) -> str:
    """sha256 over the config projection minus revision, source_path and scheduler capacity keys."""
    data = dict(config.to_dict())
    data.pop("revision", None)
    data.pop("source_path", None)
    scheduler = data.get("scheduler")
    if isinstance(scheduler, dict):
        data["scheduler"] = {k: v for k, v in scheduler.items() if k not in NON_EXECUTION_SCHEDULER_KEYS}
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
