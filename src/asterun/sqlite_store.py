from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from asterun.contracts import (
    ApprovalRequest,
    Event,
    Operation,
    OperationStatus,
    Run,
    SessionBinding,
    Task,
)
from asterun.errors import (
    IDEMPOTENCY_CONFLICT,
    INTENT_PERSIST_FAILED,
    NOT_FOUND,
    REVISION_CONFLICT,
    AsterunError,
)
from asterun.ids import ApprovalId, ConversationId, OperationId, RunId, TaskId

SCHEMA_VERSION = 6
READABLE_SCHEMA_VERSIONS = {"1", "2", "3", "4", "5", "6"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operations (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    principal TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    task_id TEXT,
    run_id TEXT,
    UNIQUE (principal, action, target, idempotency_key)
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS sessions (
    conversation_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
"""


class SqliteStore:
    """A1-03 事务存储。等待后端时不持有写事务。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.fail_next_intent = False
        self.intent_hook = None
        self.run_hook = None
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        try:
            self._initialize()
        except BaseException:
            self._conn.close()
            raise

    def _initialize(self) -> None:
        meta = self._conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
        current = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() if meta else None
        if current is not None and str(current["value"]) not in READABLE_SCHEMA_VERSIONS:
            raise AsterunError(
                INTENT_PERSIST_FAILED, "不支持的状态库版本",
                next_action="不要把旧二进制指向不兼容的新库；先备份再迁移",
            )
        self.migration_backup = None
        if current is not None and str(current["value"]) != str(SCHEMA_VERSION):
            # 新审批状态和原生引用不能交给旧二进制读写；升级前保留一致快照。
            with tempfile.NamedTemporaryFile(prefix=f"{self.path.stem}.schema-{current['value']}.",
                                             suffix=".sqlite", dir=self.path.parent, delete=False) as temporary:
                backup_path = Path(temporary.name)
            try:
                target = sqlite3.connect(backup_path)
                try:
                    self._conn.backup(target)
                finally:
                    target.close()
            except BaseException:
                backup_path.unlink()
                raise
            self.migration_backup = backup_path
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        from asterun.control_store import ControlStore

        ControlStore(self._conn)
        from asterun.plugin_store import PluginStore
        PluginStore(self._conn)
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(SCHEMA_VERSION),),
        )

    def close(self) -> None:
        self._conn.close()

    def save_task(self, task: Task) -> None:
        self._upsert("tasks", task.id.value, task.to_dict())

    def get_task(self, task_id: TaskId) -> Task:
        return Task.from_dict(self._get("tasks", task_id.value, f"找不到任务 {task_id}"))

    def save_run(self, run: Run) -> None:
        from asterun.control_store import ControlStore
        with ControlStore(self._conn).transaction():
            self._upsert("runs", run.id.value, run.to_dict())
            if self.run_hook is not None:
                self.run_hook(run)

    def get_run(self, run_id: RunId) -> Run:
        return Run.from_dict(self._get("runs", run_id.value, f"找不到运行 {run_id}"))

    def save_approval(self, approval: ApprovalRequest) -> None:
        self._upsert("approvals", approval.id.value, approval.to_dict())

    def get_approval(self, approval_id: ApprovalId) -> ApprovalRequest:
        return ApprovalRequest.from_dict(
            self._get("approvals", approval_id.value, f"找不到审批 {approval_id}")
        )

    def find_pending_approval(self, run_id: RunId) -> ApprovalRequest | None:
        rows = self._conn.execute("SELECT payload FROM approvals").fetchall()
        for row in rows:
            item = ApprovalRequest.from_dict(_json_load(row["payload"]))
            if item.run_id.value == run_id.value and item.state.value == "pending":
                return item
        return None

    def append_event(self, run_id: RunId, event: Event) -> Event:
        latest = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS seq FROM events WHERE run_id=?",
            (run_id.value,),
        ).fetchone()
        event.seq = int(latest["seq"]) + 1
        self._conn.execute(
            "INSERT INTO events(run_id, seq, payload) VALUES (?, ?, ?)",
            (run_id.value, event.seq, _json_dump(event.to_dict())),
        )
        return event

    def list_events(self, run_id: RunId, cursor: int = 0, page_size: int = 100) -> list[Event]:
        rows = self._conn.execute(
            "SELECT payload FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
            (run_id.value, cursor, page_size),
        ).fetchall()
        return [Event.from_dict(_json_load(row["payload"])) for row in rows]

    def commit_intent(self, operation: Operation, task: Task, run: Run, *, session: SessionBinding | None = None) -> tuple[Operation, Task, Run, bool]:
        if self.fail_next_intent:
            self.fail_next_intent = False
            raise AsterunError(
                INTENT_PERSIST_FAILED,
                "操作意图写盘失败，未派发后端",
                next_action="检查状态目录权限后重试；本次没有后端调用",
            )
        from asterun.control_store import ControlStore
        try:
            with ControlStore(self._conn).transaction():
                existing = self._conn.execute(
                    """
                    SELECT id, input_hash, task_id, run_id
                    FROM operations
                    WHERE principal=? AND action=? AND target=? AND idempotency_key=?
                    """,
                    (operation.principal, operation.action, operation.target, operation.idempotency_key),
                ).fetchone()
                if existing is not None:
                    if existing["input_hash"] != operation.input_hash:
                        raise AsterunError(
                            IDEMPOTENCY_CONFLICT,
                            "同一幂等键对应不同输入",
                            next_action="更换幂等键，或使用与首次提交相同的输入",
                            details={"idempotency_key": operation.idempotency_key},
                        )
                    found = self.get_operation(OperationId(existing["id"]))
                    if found.task_id is None or found.run_id is None:
                        raise AsterunError(NOT_FOUND, "已有意图缺少任务引用")
                    return found, self.get_task(found.task_id), self.get_run(found.run_id), True
                operation.task_id = task.id
                operation.run_id = run.id
                operation.status = OperationStatus.INTENDED
                self._conn.execute(
                    """
                    INSERT INTO operations(
                        id, idempotency_key, principal, action, target, input_hash,
                        status, created_at, task_id, run_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        operation.id.value,
                        operation.idempotency_key,
                        operation.principal,
                        operation.action,
                        operation.target,
                        operation.input_hash,
                        str(operation.status),
                        operation.created_at,
                        task.id.value,
                        run.id.value,
                    ),
                )
                self._conn.execute(
                    "INSERT INTO tasks(id, payload) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                    (task.id.value, _json_dump(task.to_dict())),
                )
                self._conn.execute(
                    "INSERT INTO runs(id, payload) VALUES (?, ?)",
                    (run.id.value, _json_dump(run.to_dict())),
                )
                if session is not None:
                    self.save_session(session)
                if self.intent_hook is not None:
                    self.intent_hook(operation, task, run, session)
        except AsterunError:
            raise
        except sqlite3.Error as exc:
            raise AsterunError(
                INTENT_PERSIST_FAILED,
                f"操作意图写盘失败：{exc}",
                next_action="检查状态目录后重试；本次没有后端调用",
            ) from exc
        return operation, task, run, False

    def save_operation(self, operation: Operation) -> None:
        self._conn.execute(
            """
            UPDATE operations SET status=?, task_id=?, run_id=? WHERE id=?
            """,
            (
                str(operation.status),
                None if operation.task_id is None else operation.task_id.value,
                None if operation.run_id is None else operation.run_id.value,
                operation.id.value,
            ),
        )

    def get_applied_revision(self) -> int | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key='applied_config_revision'").fetchone()
        if row is None:
            return None
        return int(row["value"])

    def bootstrap_applied_revision(self, revision: int) -> int:
        current = self.get_applied_revision()
        if current is None:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES ('applied_config_revision', ?)",
                (str(revision),),
            )
            return revision
        return current

    def cas_apply_revision(self, expected: int, new_revision: int) -> int:
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            current = self.get_applied_revision()
            if current is None:
                self._conn.execute("ROLLBACK")
                raise AsterunError(REVISION_CONFLICT, "状态库还没有已应用的配置 revision")
            if current != expected:
                self._conn.execute("ROLLBACK")
                raise AsterunError(
                    REVISION_CONFLICT,
                    f"配置 revision 不匹配：期望已应用 {expected}，实际 {current}",
                    next_action="重新读取 config.validate 后再 apply，不要覆盖别人刚写入的 revision",
                    details={"expected": expected, "applied": current, "requested": new_revision},
                )
            self._conn.execute(
                "UPDATE meta SET value=? WHERE key='applied_config_revision'",
                (str(new_revision),),
            )
            self._conn.execute("COMMIT")
        except AsterunError:
            raise
        except sqlite3.Error as exc:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise AsterunError(
                INTENT_PERSIST_FAILED,
                f"配置 revision 写入失败：{exc}",
                next_action="检查状态目录后重试；本次没有覆盖已应用 revision",
            ) from exc
        return new_revision

    def save_session(self, session: SessionBinding) -> None:
        self._conn.execute(
            """
            INSERT INTO sessions(conversation_id, payload) VALUES (?, ?)
            ON CONFLICT(conversation_id) DO UPDATE SET payload=excluded.payload
            """,
            (session.conversation_id.value, _json_dump(session.to_dict())),
        )

    def get_session(self, conversation_id: ConversationId) -> SessionBinding:
        row = self._conn.execute(
            "SELECT payload FROM sessions WHERE conversation_id=?",
            (conversation_id.value,),
        ).fetchone()
        if row is None:
            raise AsterunError(
                NOT_FOUND,
                f"找不到会话 {conversation_id}",
                details={"conversation_id": conversation_id.value},
            )
        return SessionBinding.from_dict(_json_load(row["payload"]))

    def find_session_by_native(self, backend: str, backend_session_id: str) -> SessionBinding | None:
        rows = self._conn.execute("SELECT payload FROM sessions").fetchall()
        for row in rows:
            item = SessionBinding.from_dict(_json_load(row["payload"]))
            native = None if item.backend_session_id is None else item.backend_session_id.value
            if item.backend.value == backend and native == backend_session_id:
                return item
        return None

    def list_task_ids(self) -> list[TaskId]:
        rows = self._conn.execute("SELECT id FROM tasks").fetchall()
        return [TaskId(row["id"]) for row in rows]

    def find_operation_for_run(self, run_id: RunId) -> Operation | None:
        row = self._conn.execute("SELECT id FROM operations WHERE run_id=?", (run_id.value,)).fetchone()
        if row is None:
            return None
        return self.get_operation(OperationId(row["id"]))

    def find_idempotent_operation(self, operation: Operation) -> Operation | None:
        row = self._conn.execute(
            """
            SELECT id FROM operations
            WHERE principal=? AND action=? AND target=? AND idempotency_key=?
            """,
            (operation.principal, operation.action, operation.target, operation.idempotency_key),
        ).fetchone()
        if row is None:
            return None
        return self.get_operation(OperationId(row["id"]))

    def get_operation(self, operation_id: OperationId) -> Operation:
        row = self._conn.execute("SELECT * FROM operations WHERE id=?", (operation_id.value,)).fetchone()
        if row is None:
            raise AsterunError(NOT_FOUND, f"找不到操作 {operation_id}")
        return Operation(
            id=OperationId(row["id"]),
            idempotency_key=row["idempotency_key"],
            principal=row["principal"],
            action=row["action"],
            target=row["target"],
            input_hash=row["input_hash"],
            status=OperationStatus(row["status"]),
            created_at=row["created_at"],
            task_id=None if not row["task_id"] else TaskId(row["task_id"]),
            run_id=None if not row["run_id"] else RunId(row["run_id"]),
        )

    def _upsert(self, table: str, key: str, payload: dict) -> None:
        self._conn.execute(
            f"INSERT INTO {table}(id, payload) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
            (key, _json_dump(payload)),
        )

    def _get(self, table: str, key: str, message: str) -> dict:
        row = self._conn.execute(f"SELECT payload FROM {table} WHERE id=?", (key,)).fetchone()
        if row is None:
            raise AsterunError(NOT_FOUND, message, details={"id": key})
        return _json_load(row["payload"])


def _json_dump(payload: dict) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _json_load(raw: str) -> dict:
    import json

    return json.loads(raw)
