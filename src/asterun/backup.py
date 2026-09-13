"""使用 SQLite 快照备份；恢复前验证归档与库，不解包任意路径。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from asterun.errors import INVALID_REQUEST, PATH_INVALID, AsterunError
from asterun.instance import InstanceLock
from asterun.sqlite_store import SCHEMA_VERSION, READABLE_SCHEMA_VERSIONS

MANIFEST_NAME = "manifest.json"
DB_NAME = "asterun.sqlite"
_CONTROL_KINDS = {
    "Task", "Run", "Assignment", "Session", "ExecutionContext", "Capability", "Artifact",
    "State", "Action", "Grant", "Approval", "Budget", "Reservation", "Capsule", "Evidence",
    "BackendDescriptor", "Operation", "BotInstance",
}


def _snapshot_json(payload: str) -> Any:
    def unique(pairs):
        value = {}
        for key, child in pairs:
            if key in value:
                raise AsterunError(INVALID_REQUEST, "备份控制记录包含重复 JSON 字段")
            value[key] = child
        return value
    return json.loads(payload, object_pairs_hook=unique)


def _validate_control_snapshot(conn: sqlite3.Connection) -> None:
    from asterun.control_protocol import digest, validate

    def require(condition: bool) -> None:
        if not condition:
            raise AsterunError(INVALID_REQUEST, "备份控制账本或产物引用不一致")

    blobs = {}
    for namespace, sha, content in conn.execute("SELECT namespace_id,sha256,content FROM control_blobs"):
        validate(namespace, "Id")
        validate(sha, "Sha256")
        require(isinstance(content, bytes) and hashlib.sha256(content).hexdigest() == sha)
        require((namespace, sha) not in blobs)
        blobs[namespace, sha] = len(content)

    entities = {}
    for namespace, kind, id, payload in conn.execute("SELECT namespace_id,kind,id,payload FROM control_entities"):
        item = _snapshot_json(payload)
        require(isinstance(item, dict) and item.get("kind") == kind
                and item.get("namespace_id") == namespace and item.get("id") == id)
        require((namespace, kind, id) not in entities)
        if kind == "Upload":
            require(set(item) == {"kind", "id", "namespace_id", "task_id", "owner_id", "sha256", "size_bytes"})
            for field in ("id", "namespace_id", "task_id", "owner_id"):
                validate(item[field], "Id")
            validate(item["sha256"], "Sha256")
            require(id.startswith("upl_") and type(item["size_bytes"]) is int
                    and 0 <= item["size_bytes"] <= 512 * 1024)
        else:
            require(kind in _CONTROL_KINDS)
            validate(item, kind)
        if kind in {"Upload", "Artifact"}:
            require(blobs.get((namespace, item["sha256"])) == item["size_bytes"])
            if kind == "Artifact":
                require(item["storage_ref"] == "sqlite:sha256:" + item["sha256"])
        entities[namespace, kind, id] = item

    indexed_operations = set()
    for namespace, principal, method, key, id, sha in conn.execute(
            "SELECT namespace_id,principal_id,method,idempotency_key,operation_id,digest FROM control_operations"):
        item = entities.get((namespace, "Operation", id))
        require(item is not None)
        require((item["principal_id"], item["method"], item["idempotency_key"], item["payload_sha256"]) == (principal, method, key, sha))
        require((namespace, id) not in indexed_operations)
        indexed_operations.add((namespace, id))
    require(indexed_operations == {(ns, id) for ns, kind, id in entities if kind == "Operation"})

    indexed_reservations = set()
    for namespace, id, budget_id, action_id in conn.execute("SELECT namespace_id,id,budget_id,action_id FROM control_reservations"):
        item = entities.get((namespace, "Reservation", id))
        require(item is not None)
        require((item["budget_id"], item["action_id"]) == (budget_id, action_id))
        budget = entities.get((namespace, "Budget", budget_id))
        action = entities.get((namespace, "Action", action_id))
        require(budget is not None and action is not None)
        require(item["task_id"] == budget["task_id"] == action["task_id"])
        require((namespace, id) not in indexed_reservations)
        indexed_reservations.add((namespace, id))
    require(indexed_reservations == {(ns, id) for ns, kind, id in entities if kind == "Reservation"})

    streams = {}
    for namespace, task, sequence, floor in conn.execute("SELECT namespace_id,task_id,sequence,floor FROM control_event_streams"):
        validate(namespace, "Id")
        validate(task, "Id")
        require(type(sequence) is int and type(floor) is int and 0 <= floor <= sequence <= 9007199254740991)
        require((namespace, task) not in streams)
        streams[namespace, task] = {"latest": sequence, "floor": floor, "count": 0}
    tombstones = set()
    for namespace, id in conn.execute("SELECT namespace_id,id FROM control_event_ids"):
        validate(namespace, "Id")
        validate(id, "Id")
        require(id.startswith("evt_") and (namespace, id) not in tombstones)
        tombstones.add((namespace, id))
    for namespace, task, sequence, id, payload in conn.execute("SELECT namespace_id,task_id,sequence,id,payload FROM control_events ORDER BY namespace_id,task_id,sequence"):
        item = _snapshot_json(payload)
        validate(item, "Event")
        require((item["namespace_id"], item["task_id"], item["sequence"], item["id"]) == (namespace, task, sequence, id))
        require((namespace, id) in tombstones and (namespace, task) in streams)
        stream = streams[namespace, task]
        require(sequence == stream["floor"] + stream["count"] + 1)
        stream["count"] += 1
    require(all(value["count"] == value["latest"] - value["floor"] for value in streams.values()))

    for namespace, action_id, payload, status in conn.execute("SELECT namespace_id,action_id,payload,status FROM control_outbox"):
        require((namespace, "Action", action_id) in entities and status in {"PENDING", "DELIVERING", "DONE"})
        item = _snapshot_json(payload)
        require(isinstance(item, dict))
        digest(item)  # Reject non-JSON / unsafe-number values without logging them.
    for namespace, resource, holder, expires, token in conn.execute("SELECT namespace_id,resource_id,holder_id,expires_at,fencing_token FROM control_leases"):
        validate(namespace, "Id")
        validate(resource, "Id")
        validate(expires, "Timestamp")
        require(type(token) is int and 1 <= token <= 9007199254740991)
        if holder is not None:
            validate(holder, "Id")


def _readonly(path: Path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def _hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _validate_snapshot(path: Path) -> int:
    from asterun.contracts import ApprovalRequest, Event, Run, SessionBinding, Task

    with closing(_readonly(path)) as conn:
        if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise AsterunError(INVALID_REQUEST, "备份数据库完整性检查失败")
        version_row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if version_row is None or str(version_row[0]) not in READABLE_SCHEMA_VERSIONS:
            raise AsterunError(INVALID_REQUEST, "备份数据库版本不兼容")
        version = int(version_row[0])
        expected = {"meta", "operations", "tasks", "runs", "approvals", "events"}
        if version >= 2:
            expected.add("sessions")
        if version >= 5:
            from asterun.control_store import TABLE_COLUMNS
            expected.update(TABLE_COLUMNS)
        if version >= 6:
            from asterun.plugin_store import TABLE_COLUMNS as PLUGIN_TABLE_COLUMNS
            expected.update(PLUGIN_TABLE_COLUMNS)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not expected <= tables:
            raise AsterunError(INVALID_REQUEST, "备份数据库缺少必要表")
        columns = {
            "meta": {"key", "value"},
            "operations": {"id", "idempotency_key", "principal", "action", "target", "input_hash",
                           "status", "created_at", "task_id", "run_id"},
            "tasks": {"id", "payload"}, "runs": {"id", "payload"}, "approvals": {"id", "payload"},
            "events": {"run_id", "seq", "payload"}, "sessions": {"conversation_id", "payload"},
        }
        if version >= 5:
            columns.update(TABLE_COLUMNS)
        if version >= 6:
            columns.update(PLUGIN_TABLE_COLUMNS)
        for table in expected:
            actual = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if not columns[table] <= actual:
                raise AsterunError(INVALID_REQUEST, "备份数据库表结构不兼容")
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type IN ('trigger', 'view') LIMIT 1").fetchone():
            raise AsterunError(INVALID_REQUEST, "备份含有未支持的触发器或视图")
        revision = conn.execute("SELECT value FROM meta WHERE key='applied_config_revision'").fetchone()
        if revision is not None and int(revision[0]) < 1:
            raise AsterunError(INVALID_REQUEST, "备份配置 revision 无效")
        for table, model in (("tasks", Task), ("runs", Run), ("approvals", ApprovalRequest), ("events", Event), ("sessions", SessionBinding)):
            if table in tables:
                for (payload,) in conn.execute(f"SELECT payload FROM {table}"):
                    model.from_dict(json.loads(payload))
        if version >= 5:
            _validate_control_snapshot(conn)
        if version >= 6:
            from asterun.plugin_store import validate_snapshot
            validate_snapshot(conn)
        return version


def backup_state(state_dir: Path, dest: Path) -> dict[str, Any]:
    source = state_dir / DB_NAME
    if not source.is_file():
        raise AsterunError(PATH_INVALID, "状态库不存在")
    dest = dest.expanduser().absolute()
    if dest.is_symlink() or dest.resolve().is_relative_to(state_dir.resolve()):
        raise AsterunError(PATH_INVALID, "备份输出必须位于状态目录之外，且不能是符号链接")
    if dest.exists() and source.samefile(dest):
        raise AsterunError(PATH_INVALID, "备份输出不能覆盖状态库")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix=".asterun-backup-", dir=dest.parent) as raw_work:
            work = Path(raw_work)
            snapshot = work / DB_NAME
            with closing(_readonly(source)) as src, closing(sqlite3.connect(snapshot)) as dst:
                src.backup(dst)
            version = _validate_snapshot(snapshot)
            manifest = {
                "schema_version": version,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "includes": [DB_NAME, MANIFEST_NAME],
                "sha256": _hash(snapshot),
                "excludes": ["asterun.lock", "config.json", "credentials", "workspaces"],
            }
            (work / MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False) + "\n", encoding="utf-8")
            candidate = work / "backup.tar.gz"
            with tarfile.open(candidate, "w:gz") as archive:
                archive.add(snapshot, arcname=DB_NAME)
                archive.add(work / MANIFEST_NAME, arcname=MANIFEST_NAME)
            candidate.chmod(0o600)
            os.replace(candidate, dest)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise AsterunError(INVALID_REQUEST, "状态备份失败；原状态库未被改写") from exc
    return {"output": str(dest), "bytes": dest.stat().st_size, "schema_version": version,
            "includes": [DB_NAME, MANIFEST_NAME], "message": "状态快照已备份，未包含原生会话、配置文件或工作区。"}


def restore_state(archive_path: Path, state_dir: Path) -> dict[str, Any]:
    state_dir.mkdir(parents=True, exist_ok=True)
    lock = InstanceLock(state_dir / "asterun.lock")
    lock.acquire()
    try:
        return _restore_unlocked(archive_path, state_dir)
    finally:
        lock.release()


def _restore_unlocked(archive_path: Path, state_dir: Path) -> dict[str, Any]:
    archive_path = archive_path.expanduser()
    if not archive_path.is_file():
        raise AsterunError(PATH_INVALID, "备份文件不存在")
    try:
        with tempfile.TemporaryDirectory(prefix=".asterun-restore-", dir=state_dir) as raw_work:
            work = Path(raw_work)
            with tarfile.open(archive_path, "r:gz") as archive:
                members = archive.getmembers()
                if len(members) != 2 or {m.name for m in members} != {DB_NAME, MANIFEST_NAME}:
                    raise AsterunError(INVALID_REQUEST, "备份必须且只能包含一份数据库和清单")
                for member in members:
                    if not member.isfile() or member.size < 0:
                        raise AsterunError(INVALID_REQUEST, "备份成员必须是普通文件")
                    if member.name == MANIFEST_NAME and member.size > 65536:
                        raise AsterunError(INVALID_REQUEST, "备份清单过大")
                    with archive.extractfile(member) as src, (work / member.name).open("wb") as dst:
                        shutil.copyfileobj(src, dst)
            manifest = json.loads((work / MANIFEST_NAME).read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise AsterunError(INVALID_REQUEST, "备份清单必须是对象")
            snapshot = work / DB_NAME
            if "sha256" in manifest and manifest["sha256"] != _hash(snapshot):
                raise AsterunError(INVALID_REQUEST, "备份数据库哈希不匹配")
            version = _validate_snapshot(snapshot)
            if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != version:
                raise AsterunError(INVALID_REQUEST, "清单与备份数据库版本不一致")
            # 完成全部预检后才打开目标写连接；backup API 在事务内替换库内容。
            target = state_dir / DB_NAME
            unreadable = False
            if target.exists():
                try:
                    with closing(_readonly(target)) as current:
                        unreadable = current.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
                except sqlite3.DatabaseError:
                    unreadable = True
            if unreadable:
                # 不可读的旧库无法接受 backup API；执行锁下保留旧文件以便替换失败时回退。
                moved = []
                try:
                    for suffix in ("", "-wal", "-shm"):
                        old = state_dir / (DB_NAME + suffix)
                        if old.exists():
                            saved = work / ("previous" + suffix)
                            os.replace(old, saved)
                            moved.append((old, saved))
                    snapshot.chmod(0o600)
                    os.replace(snapshot, target)
                except BaseException:
                    for old, saved in reversed(moved):
                        os.replace(saved, old)
                    raise
            else:
                with closing(_readonly(snapshot)) as src, closing(sqlite3.connect(target)) as dst:
                    src.backup(dst)
    except (OSError, sqlite3.Error, tarfile.TarError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise AsterunError(INVALID_REQUEST, "备份无效或恢复失败，无法确认恢复完成") from exc
    return {"state_dir": str(state_dir), "schema_version": version, "restored": [DB_NAME],
            "untouched": ["user workspaces", "native sessions", "shared accounts", "config.json"],
            "message": "状态库已通过校验并恢复；用户工作区与原生会话未改写。"}
