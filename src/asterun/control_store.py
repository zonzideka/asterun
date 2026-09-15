"""Transactional persistence for the runner-control/v1 service.

The caller supplies authenticated namespaces and validates entity schemas. All
mutations join its transaction, so an accepted operation, intent, reservation,
outbox item and events can be committed together. No remote calls belong here.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from typing import Any, Iterator

from .errors import AsterunError


_MAX_INT = 9007199254740991
_METERS = {"tokens", "turns", "tool_actions", "wall_ms", "usd_micros"}
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS control_entities (
        namespace_id TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL,
        payload TEXT NOT NULL, PRIMARY KEY(namespace_id, kind, id))""",
    """CREATE TABLE IF NOT EXISTS control_operations (
        namespace_id TEXT NOT NULL, principal_id TEXT NOT NULL,
        method TEXT NOT NULL, idempotency_key TEXT NOT NULL,
        operation_id TEXT NOT NULL, digest TEXT NOT NULL,
        PRIMARY KEY(namespace_id, principal_id, method, idempotency_key),
        UNIQUE(namespace_id, operation_id))""",
    """CREATE TABLE IF NOT EXISTS control_event_streams (
        namespace_id TEXT NOT NULL, task_id TEXT NOT NULL,
        sequence INTEGER NOT NULL DEFAULT 0, floor INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(namespace_id, task_id))""",
    """CREATE TABLE IF NOT EXISTS control_events (
        namespace_id TEXT NOT NULL, task_id TEXT NOT NULL, sequence INTEGER NOT NULL,
        id TEXT NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(namespace_id, task_id, sequence), UNIQUE(namespace_id, id))""",
    """CREATE TABLE IF NOT EXISTS control_event_ids (
        namespace_id TEXT NOT NULL, id TEXT NOT NULL,
        PRIMARY KEY(namespace_id, id))""",
    """CREATE TABLE IF NOT EXISTS control_outbox (
        namespace_id TEXT NOT NULL, action_id TEXT NOT NULL,
        payload TEXT NOT NULL, status TEXT NOT NULL,
        PRIMARY KEY(namespace_id, action_id))""",
    """CREATE TABLE IF NOT EXISTS control_leases (
        namespace_id TEXT NOT NULL, resource_id TEXT NOT NULL,
        holder_id TEXT, expires_at TEXT NOT NULL, fencing_token INTEGER NOT NULL,
        PRIMARY KEY(namespace_id, resource_id))""",
    """CREATE TABLE IF NOT EXISTS control_reservations (
        namespace_id TEXT NOT NULL, id TEXT NOT NULL, budget_id TEXT NOT NULL,
        action_id TEXT NOT NULL, PRIMARY KEY(namespace_id, id),
        UNIQUE(namespace_id, budget_id, action_id))""",
    """CREATE TABLE IF NOT EXISTS control_blobs (
        namespace_id TEXT NOT NULL, sha256 TEXT NOT NULL, content BLOB NOT NULL,
        PRIMARY KEY(namespace_id, sha256))""",
)

SCHEMA = ";\n".join(_SCHEMA) + ";\n"
TABLE_COLUMNS = {
    "control_entities": {"namespace_id", "kind", "id", "payload"},
    "control_operations": {"namespace_id", "principal_id", "method", "idempotency_key", "operation_id", "digest"},
    "control_event_streams": {"namespace_id", "task_id", "sequence", "floor"},
    "control_events": {"namespace_id", "task_id", "sequence", "id", "payload"},
    "control_event_ids": {"namespace_id", "id"},
    "control_outbox": {"namespace_id", "action_id", "payload", "status"},
    "control_leases": {"namespace_id", "resource_id", "holder_id", "expires_at", "fencing_token"},
    "control_reservations": {"namespace_id", "id", "budget_id", "action_id"},
    "control_blobs": {"namespace_id", "sha256", "content"},
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _time(value: str) -> datetime:
    try:
        if not isinstance(value, str) or not value.endswith("Z"):
            raise ValueError("UTC timestamp required")
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except (ValueError, TypeError) as exc:
        raise AsterunError("INVALID_REQUEST", "租约时间必须是带 Z 的 UTC 时间") from exc


def _amounts(values: list[dict[str, Any]], field: str = "amount") -> dict[tuple[str, str | None], int]:
    result: dict[tuple[str, str | None], int] = {}
    for value in values:
        key = (value["meter"], value["billing_pool_ref"])
        amount = value[field]
        if (key[0] not in _METERS or key in result or type(amount) is not int
                or not 0 <= amount <= _MAX_INT):
            raise AsterunError("INVALID_REQUEST", "预算计量必须唯一且使用非负安全整数")
        result[key] = amount
    return result


class ControlStore:
    def __init__(self, connection: sqlite3.Connection, *, initialize: bool = True) -> None:
        self._conn = connection
        self._savepoint = 0
        # executescript implicitly commits an existing transaction; execute each
        # DDL statement separately to preserve the caller's transaction boundary.
        if initialize:
            with self.transaction():
                for statement in _SCHEMA:
                    self._conn.execute(statement)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Acquire the write lock before reading invariants; never commit a parent."""
        nested = self._conn.in_transaction
        self._savepoint += 1
        name = f"control_sp_{self._savepoint}"
        self._conn.execute(f"SAVEPOINT {name}" if nested else "BEGIN IMMEDIATE")
        try:
            yield
            self._conn.execute(f"RELEASE SAVEPOINT {name}" if nested else "COMMIT")
        except BaseException:
            if self._conn.in_transaction:
                if nested:
                    self._conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
                    self._conn.execute(f"RELEASE SAVEPOINT {name}")
                else:
                    self._conn.execute("ROLLBACK")
            raise

    def get(self, kind: str, id: str, namespace: str) -> dict[str, Any]:
        if kind == "Event":
            row = self._conn.execute("SELECT payload FROM control_events WHERE namespace_id=? AND id=?", (namespace, id)).fetchone()
        else:
            row = self._conn.execute(
                "SELECT payload FROM control_entities WHERE namespace_id=? AND kind=? AND id=?",
                (namespace, kind, id),
            ).fetchone()
        if row is None:
            raise AsterunError("NOT_FOUND", "未找到可访问的控制记录")
        return json.loads(row[0])

    def put(self, entity: dict[str, Any]) -> dict[str, Any]:
        if entity["kind"] == "Event":
            raise AsterunError("INVALID_REQUEST", "事件只能通过 append_event 追加")
        with self.transaction():
            self._put(entity)
        return json.loads(_json(entity))

    def _put(self, entity: dict[str, Any]) -> None:
        self._conn.execute(
            """INSERT INTO control_entities(namespace_id,kind,id,payload) VALUES(?,?,?,?)
            ON CONFLICT(namespace_id,kind,id) DO UPDATE SET payload=excluded.payload""",
            (entity["namespace_id"], entity["kind"], entity["id"], _json(entity)),
        )

    def list(self, kind: str, namespace: str) -> list[dict[str, Any]]:
        if kind == "Event":
            rows = self._conn.execute("SELECT payload FROM control_events WHERE namespace_id=? ORDER BY task_id,sequence", (namespace,)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT payload FROM control_entities WHERE namespace_id=? AND kind=? ORDER BY id",
                (namespace, kind),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def put_blob(self, namespace: str, content: bytes) -> str:
        if not isinstance(content, bytes):
            raise AsterunError("INVALID_REQUEST", "产物内容必须是字节串")
        digest = hashlib.sha256(content).hexdigest()
        with self.transaction():
            self._conn.execute("INSERT OR IGNORE INTO control_blobs VALUES(?,?,?)", (namespace, digest, content))
            if self.get_blob(namespace, digest) != content:
                raise AsterunError("INVALID_STATE", "产物内容与持久摘要不一致")
        return digest

    def get_blob(self, namespace: str, sha256: str) -> bytes:
        row = self._conn.execute("SELECT content FROM control_blobs WHERE namespace_id=? AND sha256=?", (namespace, sha256)).fetchone()
        if row is None:
            raise AsterunError("NOT_FOUND", "未找到可访问的产物内容")
        content = bytes(row[0])
        if hashlib.sha256(content).hexdigest() != sha256:
            raise AsterunError("INVALID_STATE", "产物内容与持久摘要不一致")
        return content

    def find_operation(self, namespace: str, principal: str, method: str, key: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            """SELECT operation_id FROM control_operations WHERE namespace_id=?
            AND principal_id=? AND method=? AND idempotency_key=?""",
            (namespace, principal, method, key),
        ).fetchone()
        return self.get("Operation", row[0], namespace) if row else None

    def save_operation(self, operation: dict[str, Any], principal: str, key: str, digest: str) -> dict[str, Any]:
        namespace, method = operation["namespace_id"], operation["method"]
        with self.transaction():
            row = self._conn.execute(
                """SELECT operation_id,digest FROM control_operations WHERE namespace_id=?
                AND principal_id=? AND method=? AND idempotency_key=?""",
                (namespace, principal, method, key),
            ).fetchone()
            if row:
                if row[1] != digest:
                    raise AsterunError("IDEMPOTENCY_CONFLICT", "幂等键已绑定不同命令摘要")
                return self.get("Operation", row[0], namespace)
            try:
                self._conn.execute(
                    "INSERT INTO control_operations VALUES(?,?,?,?,?,?)",
                    (namespace, principal, method, key, operation["id"], digest),
                )
            except sqlite3.IntegrityError as exc:
                raise AsterunError("IDEMPOTENCY_CONFLICT", "Operation 身份已绑定其他命令") from exc
            self._put(operation)
        return json.loads(_json(operation))

    def append_event(self, event: dict[str, Any]) -> dict[str, Any]:
        event = json.loads(_json(event))
        namespace, task_id = event["namespace_id"], event["task_id"]
        with self.transaction():
            self._conn.execute(
                "INSERT OR IGNORE INTO control_event_streams(namespace_id,task_id) VALUES(?,?)",
                (namespace, task_id),
            )
            latest = self._conn.execute(
                "SELECT sequence FROM control_event_streams WHERE namespace_id=? AND task_id=?",
                (namespace, task_id),
            ).fetchone()[0]
            if latest >= _MAX_INT:
                raise AsterunError("INVALID_STATE", "事件序号已达到协议整数上限")
            event["sequence"] = latest + 1
            try:
                # Retention removes payloads, not the identity tombstone; an old
                # event ID must never acquire a different meaning for a client.
                self._conn.execute("INSERT INTO control_event_ids VALUES(?,?)", (namespace, event["id"]))
                self._conn.execute(
                    "INSERT INTO control_events VALUES(?,?,?,?,?)",
                    (namespace, task_id, event["sequence"], event["id"], _json(event)),
                )
            except sqlite3.IntegrityError as exc:
                raise AsterunError("IDEMPOTENCY_CONFLICT", "事件身份不可复用") from exc
            self._conn.execute(
                "UPDATE control_event_streams SET sequence=? WHERE namespace_id=? AND task_id=?",
                (event["sequence"], namespace, task_id),
            )
        return event

    def events(self, namespace: str, task_id: str, after: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        if type(after) is not int or not 0 <= after <= _MAX_INT or type(limit) is not int or not 1 <= limit <= 10000:
            raise AsterunError("INVALID_REQUEST", "事件游标或页大小无效")
        # A read transaction keeps retention metadata and this page consistent.
        with self.transaction():
            stream = self._conn.execute(
                "SELECT sequence,floor FROM control_event_streams WHERE namespace_id=? AND task_id=?",
                (namespace, task_id),
            ).fetchone()
            latest, floor = stream if stream else (0, 0)
            if after < floor:
                raise AsterunError("CURSOR_EXPIRED", "事件游标已超出保留窗口，请重新读取实体快照", details={"floor": floor, "latest": latest})
            if after > latest:
                raise AsterunError("INVALID_REQUEST", "事件游标不能指向未来", details={"latest": latest})
            rows = self._conn.execute(
                """SELECT payload FROM control_events WHERE namespace_id=? AND task_id=?
                AND sequence>? ORDER BY sequence LIMIT ?""", (namespace, task_id, after, limit),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def prune_events(self, namespace: str, task_id: str, through: int) -> None:
        """Internal retention operation. A removed prefix is never silently skipped."""
        with self.transaction():
            row = self._conn.execute(
                "SELECT sequence,floor FROM control_event_streams WHERE namespace_id=? AND task_id=?",
                (namespace, task_id),
            ).fetchone()
            latest, floor = row if row else (0, 0)
            if type(through) is not int or not 0 <= through <= latest:
                raise AsterunError("INVALID_REQUEST", "保留边界必须是已提交的事件位置")
            if through <= floor:
                return
            self._conn.execute("DELETE FROM control_events WHERE namespace_id=? AND task_id=? AND sequence<=?", (namespace, task_id, through))
            self._conn.execute("UPDATE control_event_streams SET floor=? WHERE namespace_id=? AND task_id=?", (through, namespace, task_id))

    def enqueue(self, action_id: str, namespace: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self.transaction():
            row = self._conn.execute("SELECT payload,status FROM control_outbox WHERE namespace_id=? AND action_id=?", (namespace, action_id)).fetchone()
            if row:
                if row[0] != _json(payload):
                    raise AsterunError("IDEMPOTENCY_CONFLICT", "待交付 Action 已绑定不同载荷")
                return self._outbox_row(namespace, action_id, row[0], row[1])
            self._conn.execute("INSERT INTO control_outbox VALUES(?,?,?,?)", (namespace, action_id, _json(payload), "PENDING"))
        return self._outbox_row(namespace, action_id, _json(payload), "PENDING")

    @staticmethod
    def _outbox_row(namespace: str, action_id: str, payload: str, status: str) -> dict[str, Any]:
        return {"namespace_id": namespace, "action_id": action_id, "payload": json.loads(payload), "status": status}

    def _deliveries(self, namespace: str, status: str) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT action_id,payload,status FROM control_outbox WHERE namespace_id=? AND status=? ORDER BY rowid", (namespace, status)).fetchall()
        return [self._outbox_row(namespace, row[0], row[1], row[2]) for row in rows]

    def pending(self, namespace: str) -> list[dict[str, Any]]:
        return self._deliveries(namespace, "PENDING")

    def uncertain_deliveries(self, namespace: str) -> list[dict[str, Any]]:
        return self._deliveries(namespace, "DELIVERING")

    def _delivery(self, action_id: str, namespace: str | None) -> dict[str, Any]:
        if namespace is None:
            rows = self._conn.execute("SELECT namespace_id,payload,status FROM control_outbox WHERE action_id=?", (action_id,)).fetchall()
            if len(rows) > 1:
                raise AsterunError("INVALID_REQUEST", "需要明确交付所属 namespace")
            row = rows[0] if rows else None
        else:
            row = self._conn.execute("SELECT namespace_id,payload,status FROM control_outbox WHERE namespace_id=? AND action_id=?", (namespace, action_id)).fetchone()
        if row is None:
            raise AsterunError("NOT_FOUND", "未找到待交付记录")
        return self._outbox_row(row[0], action_id, row[1], row[2])

    def mark_delivery(self, action_id: str, namespace: str | None = None) -> dict[str, Any]:
        """Persist the uncertainty window before issuing any external bytes."""
        with self.transaction():
            item = self._delivery(action_id, namespace)
            if item["status"] != "PENDING":
                raise AsterunError("UNKNOWN_REMOTE" if item["status"] == "DELIVERING" else "INVALID_STATE", "Action 已领取；需对账，不能再次派发")
            self._conn.execute("UPDATE control_outbox SET status='DELIVERING' WHERE namespace_id=? AND action_id=?", (item["namespace_id"], action_id))
            item["status"] = "DELIVERING"
            return item

    def finish_delivery(self, action_id: str, namespace: str | None = None) -> dict[str, Any]:
        with self.transaction():
            item = self._delivery(action_id, namespace)
            self._conn.execute("UPDATE control_outbox SET status='DONE' WHERE namespace_id=? AND action_id=?", (item["namespace_id"], action_id))
            item["status"] = "DONE"
            return item

    def claim_lease(self, namespace: str, resource_id: str, holder_id: str, expires_at: str, *, now: str | None = None) -> dict[str, Any]:
        timestamp = _time(now or _now())
        if _time(expires_at) <= timestamp:
            raise AsterunError("INVALID_REQUEST", "租约必须在将来到期")
        with self.transaction():
            row = self._conn.execute("SELECT holder_id,expires_at,fencing_token FROM control_leases WHERE namespace_id=? AND resource_id=?", (namespace, resource_id)).fetchone()
            if row and row[0] not in (None, holder_id) and _time(row[1]) > timestamp:
                raise AsterunError("LEASE_CONFLICT", "资源已有有效持有者")
            token = (row[2] if row else 0) + 1
            if token > _MAX_INT:
                raise AsterunError("INVALID_STATE", "租约 fencing token 已达到协议整数上限")
            self._conn.execute("""INSERT INTO control_leases VALUES(?,?,?,?,?)
                ON CONFLICT(namespace_id,resource_id) DO UPDATE SET holder_id=excluded.holder_id,
                expires_at=excluded.expires_at,fencing_token=excluded.fencing_token""", (namespace, resource_id, holder_id, expires_at, token))
        return {"holder_id": holder_id, "expires_at": expires_at, "fencing_token": token}

    def check_lease(self, namespace: str, resource_id: str, holder_id: str, token: int, *, now: str | None = None) -> dict[str, Any]:
        row = self._conn.execute("SELECT holder_id,expires_at,fencing_token FROM control_leases WHERE namespace_id=? AND resource_id=?", (namespace, resource_id)).fetchone()
        if type(token) is not int or row is None or row[0] != holder_id or row[2] != token or _time(row[1]) <= _time(now or _now()):
            raise AsterunError("LEASE_LOST", "租约已失效或 fencing token 过期")
        return {"holder_id": row[0], "expires_at": row[1], "fencing_token": row[2]}

    def release_lease(self, namespace: str, resource_id: str, holder_id: str, token: int) -> None:
        if type(token) is not int:
            raise AsterunError("LEASE_LOST", "租约 fencing token 无效")
        with self.transaction():
            result = self._conn.execute("UPDATE control_leases SET holder_id=NULL WHERE namespace_id=? AND resource_id=? AND holder_id=? AND fencing_token=?", (namespace, resource_id, holder_id, token))
            if result.rowcount != 1:
                raise AsterunError("LEASE_LOST", "不能释放其他持有者或新 epoch 的租约")

    def budget_totals(self, namespace: str, budget_id: str) -> list[dict[str, Any]]:
        with self.transaction():
            return self._budget_totals(namespace, budget_id)

    def _budget_totals(self, namespace: str, budget_id: str) -> list[dict[str, Any]]:
        totals: dict[tuple[str, str | None], dict[str, Any]] = {}
        rows = self._conn.execute("SELECT id FROM control_reservations WHERE namespace_id=? AND budget_id=?", (namespace, budget_id)).fetchall()
        for row in rows:
            reservation = self.get("Reservation", row[0], namespace)
            status = reservation["status"]
            amounts = reservation["actual"] if status == "SETTLED" else reservation["reserved"]
            if status == "RELEASED":
                continue
            for key, amount in _amounts(amounts).items():
                total = totals.setdefault(key, {"meter": key[0], "billing_pool_ref": key[1], "settled": 0, "held": 0})
                total["settled" if status == "SETTLED" else "held"] += amount
        return sorted(totals.values(), key=lambda value: (value["meter"], value["billing_pool_ref"] or ""))

    def reserve(self, reservation: dict[str, Any]) -> dict[str, Any]:
        namespace, budget_id = reservation["namespace_id"], reservation["budget_id"]
        amounts = _amounts(reservation["reserved"])
        if not amounts or reservation["status"] != "HELD" or reservation["actual"] is not None:
            raise AsterunError("INVALID_REQUEST", "新预留必须为 HELD 且实际用量未知")
        with self.transaction():
            rows = self._conn.execute("SELECT id FROM control_reservations WHERE namespace_id=? AND (id=? OR (budget_id=? AND action_id=?))", (namespace, reservation["id"], budget_id, reservation["action_id"])).fetchall()
            if rows:
                if len(rows) != 1:
                    raise AsterunError("IDEMPOTENCY_CONFLICT", "预留身份和 Action 已关联不同记录")
                previous = self.get("Reservation", rows[0][0], namespace)
                if (previous["budget_id"] != budget_id or previous["action_id"] != reservation["action_id"]
                        or previous["task_id"] != reservation["task_id"] or _amounts(previous["reserved"]) != amounts):
                    raise AsterunError("IDEMPOTENCY_CONFLICT", "该 Action 的预算预留已绑定不同内容")
                return previous
            budget = self.get("Budget", budget_id, namespace)
            if budget["task_id"] != reservation["task_id"]:
                raise AsterunError("BINDING_MISMATCH", "预算和预留必须属于同一 Task")
            limits = _amounts(budget["limits"], "limit")
            current = {(row["meter"], row["billing_pool_ref"]): row["settled"] + row["held"] for row in self.budget_totals(namespace, budget_id)}
            for key, amount in amounts.items():
                if key not in limits:
                    raise AsterunError("BUDGET_UNKNOWN", "计量及计费池没有可核对的预算限制")
                if current.get(key, 0) + amount > limits[key]:
                    raise AsterunError("BUDGET_EXHAUSTED", "已结算用量与在途预留超过预算限制")
            self._conn.execute("INSERT INTO control_reservations VALUES(?,?,?,?)", (namespace, reservation["id"], budget_id, reservation["action_id"]))
            self._put(reservation)
        return json.loads(_json(reservation))

    def settle(self, namespace: str, reservation_id: str, actual: list[dict[str, Any]], receipt_evidence_ids: list[str] | tuple[str, ...] = ()) -> dict[str, Any]:
        amounts = _amounts(actual)
        with self.transaction():
            reservation = self.get("Reservation", reservation_id, namespace)
            if reservation["status"] == "SETTLED":
                if _amounts(reservation["actual"]) != amounts:
                    raise AsterunError("IDEMPOTENCY_CONFLICT", "预留已经按不同实际用量结算")
                return reservation
            if reservation["status"] == "RELEASED":
                raise AsterunError("INVALID_STATE", "已释放预留不能再次结算")
            if not amounts.keys() <= _amounts(reservation["reserved"]).keys():
                raise AsterunError("INVALID_REQUEST", "结算不能加入未经预留的计量或计费池")
            reservation.update(status="SETTLED", actual=actual, receipt_evidence_ids=list(dict.fromkeys(receipt_evidence_ids)), revision=reservation["revision"] + 1, updated_at=_now())
            self._put(reservation)
        return reservation

    def mark_uncertain(self, namespace: str, reservation_id: str) -> dict[str, Any]:
        with self.transaction():
            reservation = self.get("Reservation", reservation_id, namespace)
            if reservation["status"] == "UNCERTAIN":
                return reservation
            if reservation["status"] != "HELD":
                raise AsterunError("INVALID_STATE", "只有在途预留可转为结果未知")
            reservation.update(status="UNCERTAIN", revision=reservation["revision"] + 1, updated_at=_now())
            self._put(reservation)
        return reservation
