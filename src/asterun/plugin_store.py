"""与现有执行意图共用 SQLite 的插件计划、共享池账本与用量观察。

这里没有网络、凭据解析、后台重试或派发入口。授权和当前计划校验由核心
AdmissionService 负责；所有写入加入调用方事务，绝不提交调用方事务。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sqlite3
from uuid import uuid4

from asterun.connections.models import PreparedPlan
from asterun.control_protocol import digest
from asterun.errors import AsterunError
from asterun.plugin_api.protocol import json_copy
from asterun.usage.models import BillingPool, UsageObservation, integer, mapping, reservation_amounts, text, timestamp

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS plugin_plans (
        namespace_id TEXT NOT NULL, id TEXT NOT NULL, principal_id TEXT NOT NULL,
        binding_sha256 TEXT NOT NULL, payload_sha256 TEXT NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(namespace_id,id))""",
    """CREATE TABLE IF NOT EXISTS plugin_execution_bindings (
        namespace_id TEXT NOT NULL, id TEXT NOT NULL, plan_id TEXT NOT NULL, principal_id TEXT NOT NULL,
        legacy_run_id TEXT, control_action_id TEXT, payload TEXT NOT NULL,
        PRIMARY KEY(namespace_id,id), UNIQUE(namespace_id,plan_id),
        UNIQUE(namespace_id,legacy_run_id), UNIQUE(namespace_id,control_action_id))""",
    """CREATE TABLE IF NOT EXISTS plugin_pool_reservations (
        namespace_id TEXT NOT NULL, id TEXT NOT NULL, binding_id TEXT NOT NULL,
        pool_ref TEXT NOT NULL, meter TEXT NOT NULL, window_id TEXT NOT NULL,
        status TEXT NOT NULL, reserved INTEGER NOT NULL, actual INTEGER, payload TEXT NOT NULL,
        PRIMARY KEY(namespace_id,id), UNIQUE(namespace_id,binding_id,pool_ref,meter,window_id))""",
    """CREATE TABLE IF NOT EXISTS plugin_reservation_changes (
        namespace_id TEXT NOT NULL, reservation_id TEXT NOT NULL, sequence INTEGER NOT NULL,
        payload TEXT NOT NULL, PRIMARY KEY(namespace_id,reservation_id,sequence))""",
    """CREATE TABLE IF NOT EXISTS plugin_usage_observations (
        namespace_id TEXT NOT NULL, id TEXT NOT NULL, pool_ref TEXT NOT NULL, meter TEXT NOT NULL,
        scope TEXT NOT NULL, scope_ref TEXT NOT NULL, window_id TEXT NOT NULL,
        source_kind TEXT NOT NULL, cursor TEXT NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(namespace_id,id),
        UNIQUE(namespace_id,pool_ref,meter,scope,scope_ref,window_id,source_kind,cursor))""",
    """CREATE TABLE IF NOT EXISTS plugin_session_locks (
        namespace_id TEXT NOT NULL, conversation_id TEXT NOT NULL, principal_id TEXT NOT NULL,
        lock_sha256 TEXT NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(namespace_id,conversation_id))""",
)
SCHEMA = ";\n".join(_SCHEMA) + ";\n"
TABLE_COLUMNS = {
    "plugin_plans": {"namespace_id", "id", "principal_id", "binding_sha256", "payload_sha256", "payload"},
    "plugin_execution_bindings": {"namespace_id", "id", "plan_id", "principal_id", "legacy_run_id", "control_action_id", "payload"},
    "plugin_pool_reservations": {"namespace_id", "id", "binding_id", "pool_ref", "meter", "window_id", "status", "reserved", "actual", "payload"},
    "plugin_reservation_changes": {"namespace_id", "reservation_id", "sequence", "payload"},
    "plugin_usage_observations": {"namespace_id", "id", "pool_ref", "meter", "scope", "scope_ref", "window_id", "source_kind", "cursor", "payload"},
    "plugin_session_locks": {"namespace_id", "conversation_id", "principal_id", "lock_sha256", "payload"},
}
_STATUSES = {"held", "committed", "released", "uncertain"}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _id(prefix):
    return prefix + "_" + uuid4().hex


def _dump(value):
    value = json_copy(value)
    digest(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _load(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise AsterunError("INVALID_STATE", "插件持久记录包含重复 JSON 字段")
            result[key] = value
        return result
    try:
        result = json.loads(raw, object_pairs_hook=unique)
        _dump(result)
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise AsterunError("INVALID_STATE", "插件持久记录不是有效 JSON") from exc


def _conflict(message="插件执行绑定已关联不同内容"):
    raise AsterunError("BINDING_MISMATCH", message)


def _evidence(values):
    if isinstance(values, dict):
        values = [values]
    if not isinstance(values, (list, tuple)):
        raise AsterunError("INVALID_REQUEST", "证据必须为引用数组或核心证据对象")
    result, known = [], set()
    for value in values:
        if not isinstance(value, dict):
            text(value, "evidence")
        sha = digest(value)
        if sha not in known:
            known.add(sha)
            result.append(json_copy(value))
    if len(result) > 256 or len(_dump(result).encode()) > 65536:
        raise AsterunError("INVALID_REQUEST", "持久证据摘要超出大小限制")
    return result


def _observation_key(observation):
    return tuple(observation[key] for key in ("pool_ref", "meter", "scope", "scope_ref", "window_id", "source_kind"))


def _derive_observation(observation, previous):
    """首个/重置计数只建立基线；没有证据时不把未知历史算作本次消耗。"""
    if observation["used"] is None:
        return None, "unknown", False
    if not observation["cumulative"]:
        return observation["used"], "reported", False
    if previous is None:
        return None, "baseline", True
    previous = previous["observation"]
    ordered = (observation.get("sequence") is not None and previous.get("sequence") is not None)
    stale = (observation["sequence"] <= previous["sequence"] if ordered else
             timestamp(observation["observed_at"]) <= timestamp(previous["observed_at"]))
    if stale:
        return None, "out_of_order", False
    if observation["used"] < previous["used"]:
        return None, "counter_reset", True
    return observation["used"] - previous["used"], "difference", True


class PluginStore:
    def __init__(self, connection: sqlite3.Connection):
        self._conn = connection
        self._savepoint = 0
        with self.transaction():
            for statement in _SCHEMA:
                self._conn.execute(statement)

    @contextmanager
    def transaction(self):
        nested = self._conn.in_transaction
        self._savepoint += 1
        name = f"plugin_sp_{self._savepoint}"
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

    def put_plan(self, plan):
        plan = PreparedPlan.from_dict(plan.to_dict() if isinstance(plan, PreparedPlan) else plan).to_dict()
        sha = digest(plan)
        with self.transaction():
            row = self._conn.execute("SELECT payload_sha256,payload FROM plugin_plans WHERE namespace_id=? AND id=?",
                                     (plan["namespace_id"], plan["id"])).fetchone()
            if row:
                if row[0] != sha or digest(_load(row[1])) != sha:
                    _conflict("PreparedPlan ID 不可重绑定或改写")
                return _load(row[1])
            self._conn.execute("INSERT INTO plugin_plans VALUES(?,?,?,?,?,?)", (
                plan["namespace_id"], plan["id"], plan["principal_id"], plan["binding_sha256"], sha, _dump(plan)))
        return json_copy(plan)

    def get_plan(self, plan_id, principal=None, namespace="ns_local"):
        row = self._conn.execute("SELECT principal_id,payload_sha256,payload FROM plugin_plans WHERE namespace_id=? AND id=?",
                                 (namespace, plan_id)).fetchone()
        if row is None or principal is not None and row[0] != principal:
            raise AsterunError("NOT_FOUND", "未找到可访问的 PreparedPlan")
        plan = PreparedPlan.from_dict(_load(row[2])).to_dict()
        if digest(plan) != row[1]:
            _conflict("PreparedPlan 持久摘要不一致")
        return plan

    def _binding(self, namespace, binding_id, principal=None):
        row = self._conn.execute("SELECT principal_id,payload FROM plugin_execution_bindings WHERE namespace_id=? AND id=?",
                                 (namespace, binding_id)).fetchone()
        if row is None or principal is not None and row[0] != principal:
            raise AsterunError("NOT_FOUND", "未找到可访问的插件执行绑定")
        result = _load(row[1])
        result["reservations"] = [_load(row[0]) for row in self._conn.execute(
            "SELECT payload FROM plugin_pool_reservations WHERE namespace_id=? AND binding_id=? ORDER BY rowid",
            (namespace, binding_id))]
        statuses = {item["status"] for item in result["reservations"]}
        result["status"] = ("uncertain" if "uncertain" in statuses else "held" if "held" in statuses else
                            "released" if statuses == {"released"} else "committed")
        return result

    def get_binding(self, binding_id, namespace="ns_local", principal=None):
        return self._binding(namespace, binding_id, principal)

    def binding_for_run(self, run_id, namespace="ns_local", principal=None):
        row = self._conn.execute("SELECT id FROM plugin_execution_bindings WHERE namespace_id=? AND legacy_run_id=?",
                                 (namespace, run_id)).fetchone()
        return None if row is None else self._binding(namespace, row[0], principal)

    def binding_for_control(self, action_id, namespace="ns_local", principal=None):
        row = self._conn.execute("SELECT id FROM plugin_execution_bindings WHERE namespace_id=? AND control_action_id=?",
                                 (namespace, action_id)).fetchone()
        return None if row is None else self._binding(namespace, row[0], principal)

    def pool_totals(self, pool_ref, namespace="ns_local", *, meter=None, window_id=None):
        """并发按池跨窗口统计；余额只统计所选窗口，未知预留永不自动释放。"""
        with self.transaction():
            rows = self._conn.execute(
                "SELECT binding_id,meter,window_id,status,reserved,actual FROM plugin_pool_reservations WHERE namespace_id=? AND pool_ref=?",
                (namespace, pool_ref)).fetchall()
            available_meters = {row[1] for row in rows if window_id is None or row[2] == window_id}
            if meter is None and len(available_meters) > 1:
                raise AsterunError("INVALID_REQUEST", "不同计量单位不能相加，请指定 meter")
            active = {row[0] for row in rows if row[3] in {"held", "uncertain"}}
            totals = {"held": 0, "committed": 0, "uncertain": 0, "released": 0}
            actual_known = 0
            for _, row_meter, window, status, reserved, actual in rows:
                if (meter is not None and meter != row_meter) or (window_id is not None and window_id != window):
                    continue
                amount = actual if status == "committed" and actual is not None else reserved
                totals[status] += amount
                if status == "committed" and actual is not None:
                    actual_known += actual
            return {"pool_ref": pool_ref, "meter": meter, "window_id": window_id, **totals,
                    "used": totals["held"] + totals["uncertain"] + totals["committed"],
                    "active": len(active), "actual_known": actual_known,
                    "enforcement": "local_admission_only"}

    def admit(self, plan, pools, *, legacy_run_id=None, control_action_id=None):
        plan = PreparedPlan.from_dict(plan.to_dict() if isinstance(plan, PreparedPlan) else plan).to_dict()
        namespace, principal = plan["namespace_id"], plan["principal_id"]
        if not any(item["amount"] > 0 for item in plan["reservations"]):
            raise AsterunError("BUDGET_UNKNOWN", "执行必须至少预留一个非零有界计量，零估价不能成为无限调用")
        if legacy_run_id is None and control_action_id is None:
            raise AsterunError("INVALID_REQUEST", "预留必须绑定执行意图或控制 Action")
        for value in (legacy_run_id, control_action_id):
            text(value, "执行引用", nullable=True)
        mapping(pools)
        normalized = {}
        for item in plan["reservations"]:
            ref = item["pool_ref"]
            if ref not in pools:
                raise AsterunError("BUDGET_UNKNOWN", "计划引用未知计费池")
            pool = BillingPool.from_dict(ref, pools[ref]).to_dict()
            if not {"local_limit", "max_concurrency", "meter", "window_id"} <= pool.keys():
                raise AsterunError("BUDGET_UNKNOWN", "计费池缺少有界本地限制、并发、计量或窗口")
            if pool["meter"] != item["meter"] or pool["window_id"] != item["window_id"]:
                _conflict("计划计量或窗口与计费池不匹配")
            normalized[ref] = pool
        with self.transaction():
            self.put_plan(plan)
            row = self._conn.execute("SELECT id FROM plugin_execution_bindings WHERE namespace_id=? AND plan_id=?", (namespace, plan["id"])).fetchone()
            if row:
                old = self._binding(namespace, row[0], principal)
                if ((legacy_run_id is not None and old["legacy_run_id"] != legacy_run_id)
                        or (control_action_id is not None and old["control_action_id"] != control_action_id)):
                    _conflict("一个确认计划不能创建第二个执行")
                return old
            for item in plan["reservations"]:
                pool = normalized[item["pool_ref"]]
                totals = self.pool_totals(item["pool_ref"], namespace, meter=item["meter"], window_id=item["window_id"])
                if totals["used"] + item["amount"] > pool["local_limit"]:
                    raise AsterunError("BUDGET_EXHAUSTED", "共享池已用与未决预留超过本地限额")
                if totals["active"] >= pool["max_concurrency"]:
                    raise AsterunError("BUDGET_EXHAUSTED", "共享池已达到并发上限；未决远端仍占用预留")
                if pool.get("remaining") is not None:
                    # 已观察余额不是可不断重用的新钱包。所有在途项及观察之后
                    # 开始的已结算项仍需扣除；余额刷新不删除旧窗口或未决项。
                    observed = timestamp(pool["observed_at"]) if pool.get("observed_at") else None
                    consumed = 0
                    for (raw,) in self._conn.execute(
                            "SELECT payload FROM plugin_pool_reservations WHERE namespace_id=? AND pool_ref=? AND meter=? AND window_id=?",
                            (namespace, item["pool_ref"], item["meter"], item["window_id"])):
                        prior = _load(raw)
                        if prior["status"] in {"held", "uncertain"}:
                            consumed += prior["reserved"]
                        elif prior["status"] == "committed" and (observed is None or timestamp(prior["created_at"]) >= observed):
                            consumed += prior["actual"] if prior["actual"] is not None else prior["reserved"]
                    if consumed + item["amount"] > pool["remaining"]:
                        raise AsterunError("BUDGET_EXHAUSTED", "共享池观察余额不足以覆盖新动作与已有未决消耗")
            now = _now()
            binding = {"id": _id("pbd"), "namespace_id": namespace, "principal_id": principal,
                       "plan_id": plan["id"], "legacy_run_id": legacy_run_id,
                       "control_action_id": control_action_id, "created_at": now}
            try:
                self._conn.execute("INSERT INTO plugin_execution_bindings VALUES(?,?,?,?,?,?,?)", (
                    namespace, binding["id"], plan["id"], principal, legacy_run_id, control_action_id, _dump(binding)))
            except sqlite3.IntegrityError as exc:
                raise AsterunError("BINDING_MISMATCH", "执行意图已绑定其他计划") from exc
            for item in plan["reservations"]:
                reservation = {"id": _id("prs"), "namespace_id": namespace, "binding_id": binding["id"],
                               "pool_ref": item["pool_ref"], "meter": item["meter"], "window_id": item["window_id"],
                               "status": "held", "reserved": item["amount"], "actual": None,
                               "estimate_source": "local_estimated", "actual_source": None,
                               "policy": normalized[item["pool_ref"]], "evidence": [],
                               "created_at": now, "updated_at": now}
                self._save_reservation(reservation)
                self._change(reservation, None, "admitted")
            return self._binding(namespace, binding["id"], principal)

    def bind_legacy(self, control_action_id, run_id, namespace="ns_local"):
        text(run_id, "legacy_run_id")
        with self.transaction():
            old = self.binding_for_control(control_action_id, namespace)
            if old is None:
                raise AsterunError("NOT_FOUND", "控制 Action 没有插件执行计划")
            if old["legacy_run_id"] not in {None, run_id}:
                _conflict("控制 Action 已绑定另一执行轮次")
            if old["legacy_run_id"] == run_id:
                return old
            value = {key: value for key, value in old.items() if key not in {"reservations", "status"}}
            value["legacy_run_id"] = run_id
            try:
                self._conn.execute("UPDATE plugin_execution_bindings SET legacy_run_id=?,payload=? WHERE namespace_id=? AND id=?",
                                   (run_id, _dump(value), namespace, old["id"]))
            except sqlite3.IntegrityError as exc:
                raise AsterunError("BINDING_MISMATCH", "执行轮次已绑定另一控制计划") from exc
            return self._binding(namespace, old["id"])

    def _namespace_for_binding(self, binding_id, namespace):
        if namespace is not None:
            return namespace
        rows = self._conn.execute("SELECT namespace_id FROM plugin_execution_bindings WHERE id=?", (binding_id,)).fetchall()
        if len(rows) != 1:
            raise AsterunError("NOT_FOUND", "执行绑定不存在或命名空间不明确")
        return rows[0][0]

    def _save_reservation(self, value):
        self._conn.execute("""INSERT INTO plugin_pool_reservations VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(namespace_id,id) DO UPDATE SET status=excluded.status,actual=excluded.actual,payload=excluded.payload""",
            (value["namespace_id"], value["id"], value["binding_id"], value["pool_ref"], value["meter"], value["window_id"],
             value["status"], value["reserved"], value["actual"], _dump(value)))

    def _change(self, value, previous, kind):
        namespace, id = value["namespace_id"], value["id"]
        sequence = self._conn.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM plugin_reservation_changes WHERE namespace_id=? AND reservation_id=?", (namespace, id)).fetchone()[0]
        change = {"sequence": sequence, "kind": kind, "recorded_at": value["updated_at"],
                  "previous_status": None if previous is None else previous["status"],
                  "previous_actual": None if previous is None else previous["actual"],
                  "previous_actual_source": None if previous is None else previous["actual_source"],
                  "actual_source": value["actual_source"],
                  "status": value["status"], "actual": value["actual"], "reserved": value["reserved"],
                  "evidence": value["evidence"]}
        self._conn.execute("INSERT INTO plugin_reservation_changes VALUES(?,?,?,?)", (namespace, id, sequence, _dump(change)))

    def reservation_history(self, reservation_id, namespace="ns_local"):
        return [_load(row[0]) for row in self._conn.execute(
            "SELECT payload FROM plugin_reservation_changes WHERE namespace_id=? AND reservation_id=? ORDER BY sequence", (namespace, reservation_id))]

    def mark_uncertain(self, binding_id, namespace=None, *, evidence=()):
        evidence = _evidence(evidence)
        with self.transaction():
            namespace = self._namespace_for_binding(binding_id, namespace)
            binding = self._binding(namespace, binding_id)
            for previous in binding["reservations"]:
                if previous["status"] == "uncertain":
                    continue
                if previous["status"] != "held":
                    raise AsterunError("INVALID_STATE", "只有在途预留可标为未知远端")
                value = {**previous, "status": "uncertain", "updated_at": _now(),
                         "evidence": _evidence(previous["evidence"] + evidence)}
                self._save_reservation(value)
                self._change(value, previous, "remote_unknown")
            return self._binding(namespace, binding_id)

    def settle(self, binding_id, actual=None, *, not_applied=False, evidence=(), namespace=None,
               source_kind="provider_observed"):
        """actual=None 保留预估；后续实际账单追加校正，绝不覆盖原预估。"""
        if type(not_applied) is not bool:
            raise AsterunError("INVALID_REQUEST", "not_applied 必须为布尔值")
        if source_kind not in ("provider_observed", "billed"):
            raise AsterunError("INVALID_REQUEST", "实际用量需明确区分供应商观察与最终账单")
        evidence = _evidence(evidence)
        with self.transaction():
            namespace = self._namespace_for_binding(binding_id, namespace)
            binding = self._binding(namespace, binding_id)
            estimates = {(r["pool_ref"], r["meter"], r["window_id"]): r for r in binding["reservations"]}
            amounts = {}
            if isinstance(actual, dict):
                if any(sum(key[0] == ref for key in estimates) != 1 for ref in actual):
                    raise AsterunError("INVALID_REQUEST", "结算池不属于执行计划或计量不明确")
                for key in estimates:
                    if key[0] in actual:
                        integer(actual[key[0]])
                        amounts[key] = actual[key[0]]
            elif actual is not None:
                amounts = reservation_amounts(actual)
            if not amounts.keys() <= estimates.keys() or not_applied and any(amounts.values()):
                raise AsterunError("INVALID_REQUEST", "结算不能添加未经预留计量；未发生不能有实际消耗")
            for key, previous in estimates.items():
                observed = 0 if not_applied else amounts.get(key, previous["actual"])
                status = "released" if not_applied else "committed"
                actual_source = "not_applied" if not_applied else source_kind if key in amounts else previous["actual_source"]
                if previous["status"] == "released":
                    if not not_applied and actual is None:
                        continue  # 重复保存同一终态没有新账单证据，不重写否定回执。
                    if status != "released" or observed != 0:
                        raise AsterunError("INVALID_STATE", "已释放的未发生预留不能转换为发生")
                if previous["status"] == "committed" and not_applied:
                    if previous["actual"] not in {None, 0} or not evidence:
                        raise AsterunError("INVALID_STATE", "已结算预留只能凭否定证据校正尚未确认的消耗")
                if previous["actual_source"] == "billed" and key in amounts and source_kind != "billed":
                    raise AsterunError("INVALID_STATE", "最终账单不能被较弱的用量观察覆盖")
                if previous["status"] == status and previous["actual"] == observed and previous["actual_source"] == actual_source:
                    continue
                value = {**previous, "status": status, "actual": observed,
                         "actual_source": actual_source,
                         "updated_at": _now(), "evidence": _evidence(previous["evidence"] + evidence)}
                self._save_reservation(value)
                self._change(value, previous, "not_applied" if not_applied else "correction" if previous["status"] == "committed" else "settled")
            return self._binding(namespace, binding_id)

    def bind_session(self, conversation_id, lock, namespace="ns_local", *, principal):
        text(conversation_id, "conversation_id")
        text(namespace, "namespace_id")
        text(principal, "principal_id")
        lock = json_copy(lock)
        mapping(lock)
        if not lock:
            _conflict("会话插件锁不能为空")
        sha = digest(lock)
        with self.transaction():
            old = self.get_session_lock(conversation_id, namespace)
            if old:
                if old["principal_id"] != principal or old["lock_sha256"] != sha:
                    _conflict("原生会话已绑定不同供应商、模型、环境或政策")
                return old
            self._conn.execute("INSERT INTO plugin_session_locks VALUES(?,?,?,?,?)",
                               (namespace, conversation_id, principal, sha, _dump(lock)))
            return self.get_session_lock(conversation_id, namespace, principal)

    def get_session_lock(self, conversation_id, namespace="ns_local", principal=None):
        row = self._conn.execute("SELECT principal_id,lock_sha256,payload FROM plugin_session_locks WHERE namespace_id=? AND conversation_id=?",
                                 (namespace, conversation_id)).fetchone()
        if row is None:
            return None
        if principal is not None and row[0] != principal:
            raise AsterunError("NOT_FOUND", "未找到可访问的会话插件锁")
        lock = _load(row[2])
        if digest(lock) != row[1]:
            _conflict("会话插件锁持久摘要不一致")
        return {"namespace_id": namespace, "conversation_id": conversation_id, "principal_id": row[0],
                "lock_sha256": row[1], "lock": lock}

    def record_observation(self, observation, namespace="ns_local"):
        observation = UsageObservation.from_dict(observation).to_dict()
        text(namespace, "namespace_id")
        key = _observation_key(observation)
        with self.transaction():
            row = self._conn.execute("""SELECT payload FROM plugin_usage_observations WHERE namespace_id=?
                AND pool_ref=? AND meter=? AND scope=? AND scope_ref=? AND window_id=? AND source_kind=? AND cursor=?""",
                (namespace, *key, observation["cursor"])).fetchone()
            if row:
                previous = _load(row[0])
                if previous["observation"] != observation:
                    raise AsterunError("IDEMPOTENCY_CONFLICT", "观察游标已绑定不同用量证据")
                return previous
            prior = self._conn.execute("""SELECT payload FROM plugin_usage_observations WHERE namespace_id=?
                AND pool_ref=? AND meter=? AND scope=? AND scope_ref=? AND window_id=? AND source_kind=? ORDER BY rowid DESC""",
                (namespace, *key))
            previous = next((item for row in prior if (item := _load(row[0]))["baseline_advanced"]), None)
            delta, derivation, advanced = _derive_observation(observation, previous)
            value = {"id": _id("puo"), "namespace_id": namespace, "created_at": _now(), "observation": observation,
                     "delta": delta, "derivation": derivation, "baseline_advanced": advanced}
            self._conn.execute("INSERT INTO plugin_usage_observations VALUES(?,?,?,?,?,?,?,?,?,?)",
                               (namespace, value["id"], *key, observation["cursor"], _dump(value)))
            return value

    def observations(self, pool_ref, namespace="ns_local"):
        return [_load(row[0]) for row in self._conn.execute(
            "SELECT payload FROM plugin_usage_observations WHERE namespace_id=? AND pool_ref=? ORDER BY rowid", (namespace, pool_ref))]

    @staticmethod
    def validate_snapshot(conn):
        validate_snapshot(conn)


def validate_snapshot(conn):
    """只读校验扩展账本、固定计划、会话锁与父执行引用。"""
    def require(condition):
        if not condition:
            raise AsterunError("INVALID_REQUEST", "备份插件计划、会话锁或共享池账本不一致")

    try:
        plans, bindings, reservations = {}, {}, {}
        seen_plan_bindings, seen_runs, seen_actions, seen_reservations = set(), set(), set(), set()
        for ns, id, principal, binding_sha, payload_sha, raw in conn.execute("SELECT namespace_id,id,principal_id,binding_sha256,payload_sha256,payload FROM plugin_plans"):
            value = PreparedPlan.from_dict(_load(raw)).to_dict()
            require((value["namespace_id"], value["id"], value["principal_id"], value["binding_sha256"]) == (ns, id, principal, binding_sha))
            require(digest(value) == payload_sha and (ns, id) not in plans)
            plans[ns, id] = value
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        run_ids = {row[0] for row in conn.execute("SELECT id FROM runs")} if "runs" in tables else None
        actions = {(row[0], row[1]) for row in conn.execute("SELECT namespace_id,id FROM control_entities WHERE kind='Action'")} if "control_entities" in tables else None
        for ns, id, plan_id, principal, run_id, action_id, raw in conn.execute("SELECT namespace_id,id,plan_id,principal_id,legacy_run_id,control_action_id,payload FROM plugin_execution_bindings"):
            value = _load(raw)
            mapping(value, {"id", "namespace_id", "plan_id", "principal_id", "legacy_run_id", "control_action_id", "created_at"}, {"id", "namespace_id", "plan_id", "principal_id", "legacy_run_id", "control_action_id", "created_at"})
            require((value["namespace_id"], value["id"], value["plan_id"], value["principal_id"], value["legacy_run_id"], value["control_action_id"]) == (ns, id, plan_id, principal, run_id, action_id))
            require((ns, id) not in bindings and (ns, plan_id) in plans and plans[ns, plan_id]["principal_id"] == principal)
            require((ns, plan_id) not in seen_plan_bindings)
            require(run_id is None or (ns, run_id) not in seen_runs)
            require(action_id is None or (ns, action_id) not in seen_actions)
            seen_plan_bindings.add((ns, plan_id))
            seen_runs.add((ns, run_id))
            seen_actions.add((ns, action_id))
            require(run_id is not None or action_id is not None)
            require(run_id is None or run_ids is None or run_id in run_ids)
            require(action_id is None or actions is None or (ns, action_id) in actions)
            timestamp(value["created_at"])
            bindings[ns, id] = value
        for ns, id, binding_id, pool, meter, window, status, reserved, actual, raw in conn.execute("SELECT namespace_id,id,binding_id,pool_ref,meter,window_id,status,reserved,actual,payload FROM plugin_pool_reservations"):
            value = _load(raw)
            fields = {"id", "namespace_id", "binding_id", "pool_ref", "meter", "window_id", "status", "reserved", "actual",
                      "estimate_source", "actual_source", "policy", "evidence", "created_at", "updated_at"}
            mapping(value, fields, fields)
            require((value["namespace_id"], value["id"], value["binding_id"], value["pool_ref"], value["meter"], value["window_id"], value["status"], value["reserved"], value["actual"]) == (ns, id, binding_id, pool, meter, window, status, reserved, actual))
            require((ns, id) not in reservations and (ns, binding_id) in bindings and status in _STATUSES)
            reservation_key = (ns, binding_id, pool, meter, window)
            require(reservation_key not in seen_reservations)
            seen_reservations.add(reservation_key)
            integer(reserved)
            integer(actual, nullable=True)
            require(status not in {"held", "uncertain"} or actual is None)
            require(status != "released" or actual == 0)
            require(value["estimate_source"] == "local_estimated")
            require(value["actual_source"] in {None, "provider_observed", "billed", "not_applied"})
            require((actual is None) == (value["actual_source"] is None))
            _evidence(value["evidence"])
            timestamp(value["created_at"])
            timestamp(value["updated_at"])
            policy = dict(value["policy"])
            require(policy.pop("ref") == pool)
            BillingPool.from_dict(pool, policy)
            require(policy["meter"] == meter and policy["window_id"] == window)
            plan = plans[ns, bindings[ns, binding_id]["plan_id"]]
            require(reservation_amounts(plan["reservations"]).get((pool, meter, window)) == reserved)
            reservations[ns, id] = value
        for key, binding in bindings.items():
            expected = reservation_amounts(plans[key[0], binding["plan_id"]]["reservations"])
            actual = {(r["pool_ref"], r["meter"], r["window_id"]): r["reserved"] for (ns, _), r in reservations.items() if ns == key[0] and r["binding_id"] == key[1]}
            require(expected == actual)
        histories = {}
        for ns, id, sequence, raw in conn.execute("SELECT namespace_id,reservation_id,sequence,payload FROM plugin_reservation_changes ORDER BY namespace_id,reservation_id,sequence"):
            require((ns, id) in reservations)
            history = histories.setdefault((ns, id), [])
            value = _load(raw)
            fields = {"sequence", "kind", "recorded_at", "previous_status", "previous_actual", "previous_actual_source",
                      "actual_source", "status", "actual", "reserved", "evidence"}
            mapping(value, fields, fields)
            require(value["sequence"] == sequence == len(history) + 1)
            require(value["reserved"] == reservations[ns, id]["reserved"] and value["status"] in _STATUSES)
            require(value["previous_status"] == (history[-1]["status"] if history else None))
            require(value["previous_actual"] == (history[-1]["actual"] if history else None))
            require(value["previous_actual_source"] == (history[-1]["actual_source"] if history else None))
            timestamp(value["recorded_at"])
            integer(value["actual"], nullable=True)
            _evidence(value["evidence"])
            history.append(value)
        require(histories.keys() == reservations.keys())
        for key, history in histories.items():
            require(history[0]["status"] == "held" and history[0]["actual"] is None)
            require((history[-1]["status"], history[-1]["actual"], history[-1]["actual_source"], history[-1]["evidence"]) == (reservations[key]["status"], reservations[key]["actual"], reservations[key]["actual_source"], reservations[key]["evidence"]))
        previous_observations, observation_ids, observation_cursors = {}, set(), set()
        for ns, id, pool, meter, scope, scope_ref, window, source, cursor, raw in conn.execute("SELECT namespace_id,id,pool_ref,meter,scope,scope_ref,window_id,source_kind,cursor,payload FROM plugin_usage_observations ORDER BY rowid"):
            value = _load(raw)
            fields = {"id", "namespace_id", "created_at", "observation", "delta", "derivation", "baseline_advanced"}
            mapping(value, fields, fields)
            observation = UsageObservation.from_dict(value["observation"]).to_dict()
            require((value["namespace_id"], value["id"]) == (ns, id))
            key = _observation_key(observation)
            require((ns, id) not in observation_ids and (ns, *key, cursor) not in observation_cursors)
            observation_ids.add((ns, id))
            observation_cursors.add((ns, *key, cursor))
            require((*key, observation["cursor"]) == (pool, meter, scope, scope_ref, window, source, cursor))
            derived = _derive_observation(observation, previous_observations.get((ns, *key)))
            require((value["delta"], value["derivation"], value["baseline_advanced"]) == derived)
            timestamp(value["created_at"])
            if value["baseline_advanced"]:
                previous_observations[ns, *key] = value
        sessions = {row[0] for row in conn.execute("SELECT conversation_id FROM sessions")} if "sessions" in tables else None
        session_keys = set()
        for ns, conversation_id, principal, sha, raw in conn.execute("SELECT namespace_id,conversation_id,principal_id,lock_sha256,payload FROM plugin_session_locks"):
            require((ns, conversation_id) not in session_keys)
            session_keys.add((ns, conversation_id))
            for field in (ns, conversation_id, principal):
                text(field)
            value = _load(raw)
            mapping(value)
            require(bool(value) and digest(value) == sha)
            require(sessions is None or conversation_id in sessions)
    except AsterunError as exc:
        raise AsterunError("INVALID_REQUEST", "备份插件计划、会话锁或共享池账本无效") from exc
    except (ValueError, TypeError, KeyError, AttributeError, sqlite3.Error, OverflowError) as exc:
        raise AsterunError("INVALID_REQUEST", "备份插件持久结构无效") from exc
