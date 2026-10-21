"""终态运行的上下文预算观测。失败不得影响 save_run。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from asterun.config_v2 import context_budget_settings
from asterun.contracts import TERMINAL_RUN_STATUSES
from asterun.usage.breakdown import normalize_breakdown

_TABLE = "context_budget_observations"
_NAMESPACE = "ns_local"
_LIMIT = 10000
_RETENTION = timedelta(days=30)
_COLUMNS = (
    "namespace_id", "run_id", "task_id", "profile_key", "backend", "role",
    "run_processed", "run_new", "task_processed", "task_new", "unknown_runs",
    "flags", "policy_sha256", "observed_at",
)
_FLAG_KEYS = (
    "segment_soft_exceeded", "task_soft_exceeded", "task_hard_exceeded",
    "max_segments_exceeded", "long_task", "would_segment", "capsule_candidate",
)


def observe_run(app, run) -> int:
    """observe 模式下为终态 run 写一行幂等观测。关闭、非终态或任何异常都返回 0。"""
    try:
        return _observe_run(app, run)
    except Exception:
        return 0


def latest_for_task(conn, task_id) -> dict | None:
    """只读最近一条。表不存在时返回 None，不建表。"""
    if not _table_exists(conn):
        return None
    row = conn.execute(
        f"""SELECT {", ".join(_COLUMNS)}
            FROM {_TABLE}
            WHERE namespace_id=? AND task_id=?
            ORDER BY observed_at DESC, rowid DESC
            LIMIT 1""",
        (_NAMESPACE, _task_text(task_id)),
    ).fetchone()
    if row is None:
        return None
    return _row_dict(row)


def list_observations(conn, *, task_id=None, limit=20) -> list[dict]:
    """只读观测。表不存在时返回空列表，不建表。"""
    if not _table_exists(conn):
        return []
    if task_id is None:
        rows = conn.execute(
            f"""SELECT {", ".join(_COLUMNS)}
                FROM {_TABLE}
                WHERE namespace_id=?
                ORDER BY observed_at DESC, rowid DESC
                LIMIT ?""",
            (_NAMESPACE, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""SELECT {", ".join(_COLUMNS)}
                FROM {_TABLE}
                WHERE namespace_id=? AND task_id=?
                ORDER BY observed_at DESC, rowid DESC
                LIMIT ?""",
            (_NAMESPACE, _task_text(task_id), limit),
        ).fetchall()
    return [_row_dict(row) for row in rows]


def _observe_run(app, run) -> int:
    settings = context_budget_settings(getattr(app, "config", None))
    if settings.get("mode") != "observe":
        return 0
    if getattr(run, "status", None) not in TERMINAL_RUN_STATUSES:
        return 0
    role = getattr(run, "role", None) or "implementation"
    backend = run.backend.value
    profile_key = f"{backend}/{role}"
    profiles = settings.get("profiles") if isinstance(settings.get("profiles"), dict) else {}
    profile = profiles.get(profile_key) or {}
    if not isinstance(profile, dict):
        profile = {}
    segmentation = settings.get("segmentation") if isinstance(settings.get("segmentation"), dict) else {}
    native = run.native if isinstance(getattr(run, "native", None), dict) else {}
    current = normalize_breakdown(native)
    task_processed, task_new, unknown_runs, run_count = _task_totals(app, run, current)
    flags = _flags(
        profile, segmentation, current, task_processed, run_count, run, native, backend, role,
    )
    observed_at = _stamp()
    policy_sha256 = hashlib.sha256(
        json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    from asterun.plugin_store import PluginStore

    store = PluginStore(app.store._conn)
    with store.transaction():
        store._conn.execute(
            f"""INSERT INTO {_TABLE}(
                    namespace_id, run_id, task_id, profile_key, backend, role,
                    run_processed, run_new, task_processed, task_new, unknown_runs,
                    flags, policy_sha256, observed_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(namespace_id, run_id) DO UPDATE SET
                    task_id=excluded.task_id,
                    profile_key=excluded.profile_key,
                    backend=excluded.backend,
                    role=excluded.role,
                    run_processed=excluded.run_processed,
                    run_new=excluded.run_new,
                    task_processed=excluded.task_processed,
                    task_new=excluded.task_new,
                    unknown_runs=excluded.unknown_runs,
                    flags=excluded.flags,
                    policy_sha256=excluded.policy_sha256,
                    observed_at=excluded.observed_at""",
            (
                _NAMESPACE, run.id.value, run.task_id.value, profile_key, backend, role,
                current.get("processed"), current.get("new"), task_processed, task_new, unknown_runs,
                json.dumps(flags, sort_keys=True, separators=(",", ":")), policy_sha256, observed_at,
            ),
        )
        _prune(store._conn)
    return 1


def _task_totals(app, run, current: dict) -> tuple[int, int, int, int]:
    """终态 run 里，可归因且 processed 非空的分量相加，其余计 unknown。"""
    considered = []
    seen = False
    run_id = run.id.value
    for item in app.store.list_runs(run.task_id):
        if item.id.value == run_id:
            considered.append(run)
            seen = True
        else:
            considered.append(item)
    if not seen:
        considered.append(run)
    task_processed = 0
    task_new = 0
    unknown = 0
    for item in considered:
        if item.status not in TERMINAL_RUN_STATUSES:
            continue
        picture = current if item is run else normalize_breakdown(getattr(item, "native", None))
        if picture.get("attributable") and picture.get("processed") is not None:
            task_processed += picture["processed"]
            if picture.get("new") is not None:
                task_new += picture["new"]
        else:
            unknown += 1
    return task_processed, task_new, unknown, len(considered)


def _exceeded(total, profile: dict, key: str) -> bool:
    if key not in profile or total is None:
        return False
    return total > profile[key]


def _flags(profile, segmentation, current, task_processed, run_count, run, native, backend, role) -> dict:
    minimum = segmentation.get("min_long_task_processed", 600_000)
    capsule_min = segmentation.get("observe_capsule_min_processed", 600_000)
    backends = segmentation.get("backends") or []
    roles = segmentation.get("roles") if "roles" in segmentation else ["implementation", "repair"]
    long_task = task_processed >= minimum
    stop = native.get("stop_reason") if isinstance(native, dict) else None
    max_turn = isinstance(stop, str) and "max_turn" in stop
    incomplete = getattr(run, "error_code", None) == "GROK_RUN_INCOMPLETE"
    would_segment = (
        backend in backends and role in roles and long_task and (incomplete or max_turn)
    )
    flags = {
        "segment_soft_exceeded": _exceeded(current.get("processed"), profile, "segment_soft"),
        "task_soft_exceeded": _exceeded(task_processed, profile, "task_soft"),
        "task_hard_exceeded": _exceeded(task_processed, profile, "task_hard"),
        "max_segments_exceeded": "max_segments" in profile and run_count > profile["max_segments"],
        "long_task": long_task,
        "would_segment": would_segment,
        "capsule_candidate": task_processed >= capsule_min,
    }
    return {key: bool(flags[key]) for key in _FLAG_KEYS}


def _prune(conn: sqlite3.Connection) -> None:
    """全表最多保留 10000 行，并删除 30 天前的记录。"""
    cutoff = _stamp(datetime.now(timezone.utc) - _RETENTION)
    conn.execute(f"DELETE FROM {_TABLE} WHERE observed_at < ?", (cutoff,))
    extra = conn.execute(
        f"""SELECT namespace_id, run_id FROM {_TABLE}
            ORDER BY observed_at DESC, rowid DESC
            LIMIT -1 OFFSET ?""",
        (_LIMIT,),
    ).fetchall()
    if extra:
        conn.executemany(
            f"DELETE FROM {_TABLE} WHERE namespace_id=? AND run_id=?",
            [(row[0], row[1]) for row in extra],
        )


def _stamp(moment: datetime | None = None) -> str:
    current = moment or datetime.now(timezone.utc)
    return current.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _table_exists(conn) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (_TABLE,),
    ).fetchone()
    return row is not None


def _task_text(task_id) -> str:
    value = getattr(task_id, "value", task_id)
    return value if isinstance(value, str) else str(task_id)


def _row_dict(row) -> dict:
    item = {column: row[index] for index, column in enumerate(_COLUMNS)}
    item["flags"] = json.loads(item["flags"])
    return item
