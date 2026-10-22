"""把续段胶囊写入 context_capsules，并按内容寻址保存正文。

observe 调用时 trigger 为 observe_only，ready 一律写 0，不派发。
裁剪只删行，不删 blob。读取方不得建表。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from asterun.capsule.assemble import assemble_capsule, segment_index_for, verify_capsule
from asterun.capsule.model import CapsuleTooLarge, canonical

_TABLE = "context_capsules"
_NAMESPACE = "ns_local"
_LIMIT = 2000
_RETENTION = timedelta(days=30)
_COLUMNS = (
    "namespace_id", "capsule_id", "task_id", "run_id", "segment_index", "trigger",
    "body_sha256", "blob_sha256", "size_bytes", "truncated", "ready", "issues",
    "error_code", "created_at",
)


def record_capsule(app, task, run, *, trigger) -> dict | None:
    """组装、校验、保存正文并按 (run, trigger) upsert。

    ready 只有在校验没有问题、且 trigger 不是 observe_only 时才为 1。
    observe 不派发，所以 observe_only 一律 ready=0。
    CapsuleTooLarge 只写行，error_code 为 CAPSULE_TOO_LARGE，不写 blob。
    """
    from asterun.plugin_store import PluginStore

    store = PluginStore(app.store._conn)
    segment_index = segment_index_for(app, task, run)
    try:
        capsule = assemble_capsule(
            app, task, run, trigger=trigger, segment_index=segment_index,
        )
    except CapsuleTooLarge:
        row = _base_row(task, run, trigger, segment_index)
        row["error_code"] = "CAPSULE_TOO_LARGE"
        _write(app, store, row, None)
        return row
    issues = list(verify_capsule(app, task, capsule))
    integrity = capsule.get("integrity") if isinstance(capsule.get("integrity"), dict) else {}
    row = _base_row(task, run, trigger, capsule.get("segment_index", segment_index))
    row.update({
        "capsule_id": capsule.get("capsule_id"),
        "body_sha256": integrity.get("body_sha256"),
        "size_bytes": integrity.get("size_bytes"),
        "truncated": 1 if integrity.get("truncated") else 0,
        "ready": 0 if trigger == "observe_only" or issues else 1,
        "issues": issues,
        "error_code": None,
        "created_at": capsule.get("created_at") or row["created_at"],
    })
    _write(app, store, row, canonical(capsule))
    return row


def list_capsules(conn, *, task_id=None, limit=20) -> list[dict]:
    """只读胶囊行。表不存在时返回空列表，不建表。"""
    if not _table_exists(conn):
        return []
    if task_id is None:
        rows = conn.execute(
            f"""SELECT {", ".join(_COLUMNS)}
                FROM {_TABLE}
                WHERE namespace_id=?
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?""",
            (_NAMESPACE, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""SELECT {", ".join(_COLUMNS)}
                FROM {_TABLE}
                WHERE namespace_id=? AND task_id=?
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?""",
            (_NAMESPACE, _task_text(task_id), limit),
        ).fetchall()
    return [_row_dict(row) for row in rows]


def latest_with_blob(conn, task_id) -> dict | None:
    """只读该任务最新一条有正文的胶囊。表不存在时返回 None，不建表。"""
    if not _table_exists(conn):
        return None
    row = conn.execute(
        f"""SELECT {", ".join(_COLUMNS)}
            FROM {_TABLE}
            WHERE namespace_id=? AND task_id=?
              AND blob_sha256 IS NOT NULL AND blob_sha256 != ''
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1""",
        (_NAMESPACE, _task_text(task_id)),
    ).fetchone()
    if row is None:
        return None
    return _row_dict(row)


def load_capsule(app, blob_sha) -> dict:
    """按 checkpoint blob 的 sha256 读回胶囊 JSON。"""
    return json.loads(app.store.get_checkpoint_blob(blob_sha))


def _base_row(task, run, trigger, segment_index) -> dict:
    return {
        "namespace_id": _NAMESPACE,
        "capsule_id": None,
        "task_id": _task_text(getattr(task, "id", task)),
        "run_id": _task_text(getattr(run, "id", run)),
        "segment_index": segment_index,
        "trigger": trigger,
        "body_sha256": None,
        "blob_sha256": None,
        "size_bytes": None,
        "truncated": 0,
        "ready": 0,
        "issues": [],
        "error_code": None,
        "created_at": str(getattr(run, "created_at", "") or _stamp()),
    }


def _write(app, store, row: dict, blob: bytes | None) -> None:
    issues = row.get("issues")
    if not isinstance(issues, str):
        issues = json.dumps(issues if issues is not None else [], ensure_ascii=False, separators=(",", ":"))
    with store.transaction():
        blob_sha = None
        if blob is not None:
            blob_sha = app.store.put_checkpoint_blob(blob)
            row["blob_sha256"] = blob_sha
        store._conn.execute(
            f"""INSERT INTO {_TABLE}(
                    namespace_id, capsule_id, task_id, run_id, segment_index, trigger,
                    body_sha256, blob_sha256, size_bytes, truncated, ready, issues,
                    error_code, created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(namespace_id, run_id, trigger) DO UPDATE SET
                    capsule_id=excluded.capsule_id,
                    task_id=excluded.task_id,
                    segment_index=excluded.segment_index,
                    body_sha256=excluded.body_sha256,
                    blob_sha256=excluded.blob_sha256,
                    size_bytes=excluded.size_bytes,
                    truncated=excluded.truncated,
                    ready=excluded.ready,
                    issues=excluded.issues,
                    error_code=excluded.error_code,
                    created_at=excluded.created_at""",
            (
                row["namespace_id"], row["capsule_id"], row["task_id"], row["run_id"],
                row["segment_index"], row["trigger"], row["body_sha256"], row["blob_sha256"],
                row["size_bytes"], row["truncated"], int(row["ready"]), issues,
                row["error_code"], row["created_at"],
            ),
        )
        _prune(store._conn)


def _prune(conn: sqlite3.Connection) -> None:
    """全表最多保留 2000 行，并删除 30 天前的记录。只删行，不删 blob。"""
    cutoff = _stamp(datetime.now(timezone.utc) - _RETENTION)
    conn.execute(f"DELETE FROM {_TABLE} WHERE created_at < ?", (cutoff,))
    extra = conn.execute(
        f"""SELECT namespace_id, run_id, trigger FROM {_TABLE}
            ORDER BY created_at DESC, rowid DESC
            LIMIT -1 OFFSET ?""",
        (_LIMIT,),
    ).fetchall()
    if extra:
        conn.executemany(
            f"DELETE FROM {_TABLE} WHERE namespace_id=? AND run_id=? AND trigger=?",
            [(row[0], row[1], row[2]) for row in extra],
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


def _task_text(value) -> str:
    raw = getattr(value, "value", value)
    return raw if isinstance(raw, str) else str(raw)


def _row_dict(row) -> dict:
    item = {column: row[index] for index, column in enumerate(_COLUMNS)}
    raw = item.get("issues")
    if raw is None or raw == "":
        item["issues"] = []
    else:
        item["issues"] = json.loads(raw)
    return item
