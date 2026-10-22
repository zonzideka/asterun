"""路由决策的持久记录。写入加入调用方事务，不提交。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
import sqlite3
from uuid import uuid4

_CANDIDATE_KEYS = ("alias", "tier", "S", "S_A", "excluded", "reason_codes")
_LIMIT = 1000
_RETENTION = timedelta(days=30)


def _stamp(moment: datetime | None = None) -> str:
    current = moment or datetime.now(timezone.utc)
    return current.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _candidate(item: dict) -> dict:
    stored = {key: item.get(key) for key in _CANDIDATE_KEYS}
    codes = stored["reason_codes"]
    if isinstance(codes, list):
        stored["reason_codes"] = list(codes)
    return stored


def _payload(result: dict) -> dict:
    candidates = []
    for item in result.get("candidates") or []:
        if isinstance(item, dict):
            candidates.append(_candidate(item))
    return {"candidates": candidates, "would_choose": result.get("chosen")}


def _prune(conn: sqlite3.Connection) -> None:
    """全表最多保留 1000 行，并删除 30 天前的记录。"""
    cutoff = _stamp(datetime.now(timezone.utc) - _RETENTION)
    conn.execute("DELETE FROM plugin_routing_decisions WHERE evaluated_at < ?", (cutoff,))
    extra = conn.execute(
        """SELECT id FROM plugin_routing_decisions
           ORDER BY evaluated_at DESC, rowid DESC
           LIMIT -1 OFFSET ?""",
        (_LIMIT,),
    ).fetchall()
    if extra:
        conn.executemany("DELETE FROM plugin_routing_decisions WHERE id=?", extra)


def record_decision(conn, *, namespace, task_id, run_id, role, group, result, mode) -> dict:
    evaluated_at = _stamp()
    decision_id = "rtd_" + uuid4().hex
    body = _payload(result)
    encoded = json.dumps(body, sort_keys=True)
    chosen = result.get("chosen")
    primary = result.get("primary")
    reason = result.get("reason_code")
    conn.execute(
        """INSERT INTO plugin_routing_decisions(
               namespace_id, id, task_id, run_id, role, group_name, chosen,
               primary_alias, mode, reason_code, payload, evaluated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (namespace, decision_id, task_id, run_id, role, group, chosen,
         primary, mode, reason, encoded, evaluated_at),
    )
    _prune(conn)
    return {
        "id": decision_id,
        "namespace_id": namespace,
        "task_id": task_id,
        "run_id": run_id,
        "role": role,
        "group_name": group,
        "chosen": chosen,
        "primary_alias": primary,
        "mode": mode,
        "reason_code": reason,
        "payload": body,
        "evaluated_at": evaluated_at,
    }


def last_chosen(conn, namespace, group) -> str | None:
    row = conn.execute(
        """SELECT chosen FROM plugin_routing_decisions
           WHERE namespace_id=? AND group_name=?
           ORDER BY evaluated_at DESC, rowid DESC
           LIMIT 1""",
        (namespace, group),
    ).fetchone()
    if row is None:
        return None
    return row[0]


def latest_for_task(conn, namespace, task_id) -> dict | None:
    row = conn.execute(
        """SELECT id, task_id, run_id, role, group_name, chosen, primary_alias,
                  mode, reason_code, payload, evaluated_at
           FROM plugin_routing_decisions
           WHERE namespace_id=? AND task_id=?
           ORDER BY evaluated_at DESC, rowid DESC
           LIMIT 1""",
        (namespace, task_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "namespace_id": namespace,
        "task_id": row[1],
        "run_id": row[2],
        "role": row[3],
        "group_name": row[4],
        "chosen": row[5],
        "primary_alias": row[6],
        "mode": row[7],
        "reason_code": row[8],
        "payload": json.loads(row[9]),
        "evaluated_at": row[10],
    }
