"""核心线程上的 worker 绑定与观察落盘；外部进程没有 Store/Grant 接口。"""
from __future__ import annotations

from asterun.control_protocol import digest
from asterun.contracts import EventType
from asterun.errors import AsterunError
from asterun.plugin_api.protocol import json_copy


def bind_execution(app, run, task, *, existing=False):
    backend = app.backends[run.backend.value]
    if not hasattr(backend, "bind_execution"):
        return
    plan = app.admission.guard_existing_run(run.id.value) if existing else app.admission.guard_run(run.id.value)
    binding = app.admission.store.binding_for_run(run.id.value, app.admission.namespace, app.admission.principal)
    snapshot = plan["binding"]
    connection = snapshot["connection"]
    context = {
        "plan_id": plan["id"], "binding_id": binding["id"], "run_id": run.id.value, "task_id": task.id.value,
        "capability": snapshot.get("capability", "agent.execute"),
        "connection_ref": snapshot["connection_ref"], "provider_account_ref": snapshot["provider_account_ref"],
        "auth_mode": connection["auth_mode"], "upstream_version": connection.get("upstream_version"),
        "options": connection["options"], "workspace_root": snapshot["workspace_root"],
        "input_sha256": snapshot["input_sha256"], "billing_pool_refs": connection["billing_pool_refs"],
        **{key: run.native[key] for key in ("provider_job_ref", "provider_session_id", "provider_turn_id") if key in run.native},
    }
    if not existing and run.role == "implementation" and task.conversation_id:
        session = app.store.get_session(task.conversation_id)
        if session.backend_session_id:
            app.admission.assert_session(session)
            context["provider_session_id"] = session.backend_session_id.value
            if session.latest_turn_id:
                context["previous_provider_turn_id"] = session.latest_turn_id
    backend.bind_execution(run.id, json_copy(context))


def ingest_result(app, run, result):
    backend = app.backends[run.backend.value]
    if not hasattr(backend, "bind_execution"):
        return
    store = app.admission.store
    binding = store.binding_for_run(run.id.value, app.admission.namespace, app.admission.principal)
    if binding is None:
        raise AsterunError("BINDING_MISMATCH", "worker 结果缺少核心执行绑定")
    plan = app.admission._plan(binding)
    snapshot = plan["binding"]
    if result.get("output") is not None:
        run.output = backend.registration.manifest.validate_output(snapshot.get("capability", "agent.execute"), result["output"])
    # 观察只追加，不让插件指定计费池或把自报消耗直接改写成可信账单/退款。
    for raw in result.get("usage") or []:
        if not isinstance(raw, dict):
            raise AsterunError("INVALID_REQUEST", "worker 用量观察必须为对象")
        for pool_ref, pool in snapshot["billing_pools"].items():
            if raw.get("meter") != pool.get("meter", "requests"):
                continue
            scope = raw.get("scope", "run")
            scope_ref = run.id.value
            if scope == "session_cumulative":
                scope_ref = (result.get("native") or run.native).get("provider_session_id")
                if not scope_ref:
                    continue  # 不把未知原生 session 的计数器合并到其它会话。
            elif scope == "billing_window":
                # worker 没有全账户账单权限，不能声明跨连接的账单窗口计数器。
                continue
            observation = {"pool_ref": pool_ref, "meter": raw["meter"], "scope": scope,
                "scope_ref": scope_ref, "window_id": pool.get("window_id", "local"),
                "cursor": raw.get("cursor") or digest(raw), "source_kind": "provider_observed",
                "observed_at": raw.get("observed_at") or plan["created_at"], "used": raw.get("used"),
                **{key: raw[key] for key in ("sequence", "cumulative") if key in raw}}
            store.record_observation(observation, app.admission.namespace)
    rows = result.get("plugin_events") or []
    if not rows:
        return
    # 去重依据在现有事件表中，重启后也不会丢失；观察不具有核心状态或审批权限。
    known, cursor, greatest_sequence = {}, 0, None
    while True:
        page = app.store.list_events(run.id, cursor, 500)
        if not page:
            break
        for event in page:
            if event.payload.get("observation_key"):
                known[event.payload["observation_key"]] = event.payload.get("observation_sha256")
                sequence = event.payload.get("observation", {}).get("provider_sequence")
                if type(sequence) is int:
                    greatest_sequence = sequence if greatest_sequence is None else max(greatest_sequence, sequence)
        cursor = page[-1].seq
    for row in rows:
        sha = digest(row)
        key = row.get("provider_event_id") or sha
        if key in known:
            if known[key] != sha:
                raise AsterunError("IDEMPOTENCY_CONFLICT", "同一 worker 观察标识返回了不同内容")
            continue
        sequence = row.get("provider_sequence")
        note = "unavailable"
        if type(sequence) is int:
            note = ("first_observation" if greatest_sequence is None else
                    "out_of_order" if sequence <= greatest_sequence else
                    "gap_observed" if sequence > greatest_sequence + 1 else "contiguous")
            greatest_sequence = sequence if greatest_sequence is None else max(greatest_sequence, sequence)
        app._event(run.id, EventType.MESSAGE, "plugin.observation", {
            "observation_key": key, "observation_sha256": sha,
            "trust": "untrusted_provider_observation", "plugin_id": snapshot["plugin_id"], "observation": row,
            "provider_sequence_note": note,
        })
        known[key] = sha
