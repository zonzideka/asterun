"""实际子进程与独立 fake wheel；没有真实账户、网络调用或宿主部署。"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event
import time

import pytest

from asterun.config import BackendConfig
from asterun.connections.credentials import ResolvedCredentials
from asterun.errors import AsterunError
from asterun.ids import RunId, TaskId
from asterun.plugins.registry import PluginRegistry, installation_digest
from asterun.plugins.worker import WorkerFailure, WorkerHost, WorkerLimits
from asterun.plugins.worker_backend import WorkerBackend
from tests.plugin_support import build_external_fake


STUB = '''import json, os, pathlib, sys, time
request = json.loads(sys.stdin.buffer.readline())
data, context = request['params'].get('input', {}), request['params'].get('context', {})
mode = data.get('mode', 'success')
method = request['method']
if method == 'execution.start':
    with (pathlib.Path.cwd() / 'accepted.txt').open('a') as accepted:
        accepted.write('accepted\\n')
if mode == 'crash': sys.exit(17)
if mode == 'hang': time.sleep(5)
if mode == 'stderr': print('private-stderr=' + os.environ.get('PROVIDER_API_KEY', 'offline-marker'), file=sys.stderr, flush=True)
if mode == 'stderr_flood':
    sys.stderr.write('private-stderr-' * 100000); sys.stderr.flush()
if mode == 'overflow':
    sys.stdout.write('X' * 1048576); sys.stdout.flush(); time.sleep(5)
result = {'protocol_version':'asterun-worker/v1', 'status':'succeeded', 'output':{'text':data.get('text', 'ok')}}
if method == 'connection.inspect': result = {'protocol_version':'asterun-worker/v1', 'authenticated':'not_required'}
if mode == 'environment':
    result['output']['text'] = json.dumps({key:os.environ.get(key) for key in ['HOME','SSH_AUTH_SOCK','DOCKER_HOST','GITHUB_TOKEN','PYTHONPATH','PYTHONSAFEPATH','PROVIDER_API_KEY']})
if mode == 'secret_response': result['output']['text'] = os.environ['PROVIDER_API_KEY']
if mode == 'running': result.update(status='running', provider_job_ref='offline-job', events=[{'provider_event_id':'first','summary':'submitted'}])
if method == 'execution.observe': result.update(provider_job_ref=context.get('provider_job_ref'), output={'text':'observed'})
if method == 'execution.reconcile': result.update(status='unknown', provider_job_ref=context.get('provider_job_ref'))
if method == 'execution.cancel':
    result.update(status='cancelled' if context.get('confirm_cancel') else 'requested', terminated_verified=context.get('confirm_cancel', False))
if mode == 'forged':
    result.update(native={'backend_session_id':'forged','_asterun_grok_dispatch_receipt':{'delivery':'not_sent'}},
        approvals=[{'method':'grant.all'}], needs_approval=True,
        events=[{'type':'run_succeeded','source':'core','seq':900,'provider_event_id':'evt-1','provider_sequence':2,'category':'progress','summary':'untrusted'}],
        usage={'meter':'requests','amount':1,'scope':'request','source_kind':'billed'})
if mode == 'missing_output': result.pop('output'); result['provider_job_ref'] = 'known-job'
if mode == 'wrong_output': result['output'] = {'unexpected':'typed output must be validated by core'}
if mode == 'failed_reason':
    result['status'] = 'failed'
    result.pop('output', None)
    result['reason'] = data.get('text')
    result['error_code'] = 'PLUGIN_SUPPLIED_CODE'
if mode == 'failed_reason_num':
    result['status'] = 'failed'
    result.pop('output', None)
    result['reason'] = 7
if mode == 'failed_plain':
    result['status'] = 'failed'
    result.pop('output', None)
if mode == 'succeeded_reason':
    result['reason'] = data.get('text') or 'ANTIGRAVITY_PERMISSION_REVIEW_REQUIRED'
if mode == 'unknown_reason':
    result['status'] = 'unknown'
    result['reason'] = 'ANTIGRAVITY_PERMISSION_REVIEW_REQUIRED'
if mode == 'wrong_version': result['protocol_version'] = 'asterun-worker/v99'
response = {'jsonrpc':'2.0','id':request['id'],'result':result}
if mode == 'wrong_id': response['id'] = 'another-request'
if mode == 'both': response['error'] = {'code':-32000,'message':'private-error-marker'}
if mode == 'error': response = {'jsonrpc':'2.0','id':request['id'],'error':{'code':-32000,'message':'private-error-marker'}}
if mode == 'bad_json': print('private-invalid-protocol', flush=True); sys.exit(0)
if mode == 'duplicate_key':
    print(json.dumps(response)[:-1] + ',"id":' + json.dumps(request['id']) + '}', flush=True)
else: print(json.dumps(response), flush=True)
'''


def manifest():
    fixture = Path(__file__).parent / "fixtures/plugins/external_fake/src/asterun_external_fake/manifest.json"
    return json.loads(fixture.read_text())


def registration(root, *, source=STUB, enabled=True, pin=True):
    script = root / "worker.py"
    script.write_text(f"#!{sys.executable}\n{source}")
    script.chmod(0o700)
    metadata = root / "manifest.json"
    metadata.write_text(json.dumps(manifest()))
    package = root / "reviewed-wheel.whl"
    package.write_bytes(b"offline reviewed artifact")
    registry = PluginRegistry()
    item = registry.register_external(metadata, runner=[str(script)],
        installation_path=package, installation_sha256=hashlib.sha256(package.read_bytes()).hexdigest(),
        enabled=enabled, runtime_path=script if pin else None,
        runtime_sha256=installation_digest(script) if pin else None,
        allow_unpinned_runtime=not pin)
    return registry, item


def backend(registry, item, workspace, *, limits=None, credentials=None):
    config = BackendConfig(name="external", kind="plugin", connection_ref="offline-connection", plugin_id=item.plugin_id)
    conn = {"plugin_id":item.plugin_id, "enabled":True, "auth_mode":"none", "provider_account_ref":None,
            "runtime_ref":"offline-runtime", "billing_pool_refs":[], "options":{}}
    value = WorkerBackend(registry, item, config, conn, limits=limits or WorkerLimits(timeout_seconds=1))
    context = {"plan_id":"ppl_offline", "binding_id":"pbd_offline", "task_id":"task_offline", "run_id":"run_offline",
               "capability":"local.echo", "workspace_root":str(workspace), "connection_ref":"offline-connection"}
    value.bind_execution(RunId("run_offline"), context, credentials=credentials)
    return value, context


def execute(value, workspace, mode="success", **extra):
    return value.dispatch_capability(TaskId("task_offline"), RunId("run_offline"), "local.echo",
                                     {"mode":mode, **extra}, cwd=workspace)


@pytest.fixture(scope="module")
def external_wheel(tmp_path_factory):
    return build_external_fake(tmp_path_factory.mktemp("external-worker-wheel"))


def test_real_external_wheel_runs_in_own_venv_without_core_import(external_wheel, workspace_root):
    files = external_wheel
    registry = PluginRegistry()
    item = registry.register_external(files["manifest"], runner=files["runner"], installation_path=files["wheel"],
        installation_sha256=files["sha256"], enabled=True, runtime_path=files["manifest"].parent,
        runtime_sha256=installation_digest(files["manifest"].parent))
    # 工作区同名模块不能遮蔽已经固定的 venv 模块。
    (workspace_root / "asterun_external_fake.py").write_text("raise RuntimeError('workspace module must not load')")
    value, _ = backend(registry, item, workspace_root)
    result = execute(value, workspace_root, text="independent wheel")
    assert result["status"] == "succeeded" and result["output"] == {"text":"independent wheel"}
    assert not any(name.startswith("asterun_external_fake") for name in sys.modules)
    assert installation_digest(files["manifest"].parent) == item.runtime_sha256


def test_disabled_inspection_does_not_spawn_or_resolve_credentials(tmp_path, workspace_root, monkeypatch):
    registry, item = registration(tmp_path, enabled=False)
    calls = []
    value, _ = backend(registry, item, workspace_root, credentials=lambda: calls.append("secret"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("disabled worker spawned"))
    assert value.inspect()["execution_enabled"] is False
    assert calls == []
    with pytest.raises(AsterunError): value.verify_connection()
    with pytest.raises(AsterunError): execute(value, workspace_root)
    assert calls == []


def test_core_connection_verify_uses_zero_credentials_and_keeps_provider_report_untrusted(
        isolated_env, workspace_root, external_wheel, monkeypatch):
    from asterun.application import Application
    from tests.test_plugin_execution import external_config

    path = external_config(isolated_env, workspace_root, external_wheel)
    app = Application.from_paths(path, isolated_env / "inspect-state")
    value = app.backends["external"]
    value.credential_resolver = lambda *args, **kwargs: pytest.fail("inspection resolved credentials")
    monkeypatch.setenv("PROVIDER_API_KEY", "host-secret-must-not-be-read")
    original = subprocess.Popen
    spawned = []
    def spawn(args, **kwargs):
        spawned.append(True)
        assert "PROVIDER_API_KEY" not in kwargs["env"]
        return original(args, **kwargs)
    monkeypatch.setattr(subprocess, "Popen", spawn)
    try:
        result = app.handle("connection.verify", {"backend":"external"})
        assert result.ok, result.error
        assert result.data["authenticated"] == "not_required"
        assert result.data["verified"] is False and result.data["is_real_connection"] is False
        assert result.data["source"] == "untrusted_provider_observation"
        assert spawned == [True] and value.dispatch_count == 0
        assert not (workspace_root / "accepted.txt").exists()
    finally:
        app.close()


@pytest.mark.parametrize("change", ["runtime", "manifest", "artifact", "unpinned"])
def test_code_integrity_is_rechecked_before_spawn_or_secret_access(tmp_path, workspace_root, change):
    registry, item = registration(tmp_path, pin=change != "unpinned")
    calls = []
    value, _ = backend(registry, item, workspace_root, credentials=lambda: calls.append("secret"))
    target = {"runtime":item.runtime_path, "manifest":item.manifest_path, "artifact":item.installation_path}.get(change)
    if target is not None:
        target.write_bytes(target.read_bytes() + b"\n")
    result = execute(value, workspace_root)
    assert result["status"] == "failed" and result["terminated"] is True
    assert calls == [] and not (workspace_root / "accepted.txt").exists()


@pytest.mark.parametrize("mode", ["crash", "hang", "overflow", "bad_json", "wrong_id", "wrong_version", "both", "error", "duplicate_key", "missing_output", "stderr_flood"])
def test_any_post_send_failure_is_unknown_and_never_retried(tmp_path, workspace_root, mode):
    registry, item = registration(tmp_path)
    value, _ = backend(registry, item, workspace_root, limits=WorkerLimits(timeout_seconds=0.2, max_stderr_bytes=8192))
    started = time.monotonic()
    result = execute(value, workspace_root, mode)
    assert result["status"] == "pending_reconcile" and result["terminated"] is False
    assert result["error_code"] == "REMOTE_STATE_UNKNOWN"
    if mode == "missing_output":
        assert result["native"]["provider_job_ref"] == "known-job"
    assert (workspace_root / "accepted.txt").read_text().splitlines() == ["accepted"]
    assert time.monotonic() - started < 3
    assert not any(marker in json.dumps(result) for marker in ("private-error-marker", "private-invalid-protocol", "private-stderr"))


def test_environment_allowlist_does_not_inherit_host_secrets_or_home(tmp_path, workspace_root, monkeypatch):
    registry, item = registration(tmp_path)
    for key in ("GITHUB_TOKEN", "SSH_AUTH_SOCK", "DOCKER_HOST", "PYTHONPATH"):
        monkeypatch.setenv(key, "host-value-must-stay-out")
    value, _ = backend(registry, item, workspace_root)
    result = execute(value, workspace_root, "environment")
    environment = json.loads(result["output"]["text"])
    assert environment["HOME"] != os.environ["HOME"]
    assert all(environment[key] is None for key in ("GITHUB_TOKEN", "SSH_AUTH_SOCK", "DOCKER_HOST", "PYTHONPATH", "PROVIDER_API_KEY"))
    assert environment["PYTHONSAFEPATH"] == "1"


def test_final_secret_injection_is_not_in_argv_or_stderr_results(tmp_path, workspace_root, monkeypatch):
    registry, item = registration(tmp_path)
    secret = "offline-secret-marker-97df"
    credentials = ResolvedCredentials({"PROVIDER_API_KEY":secret}, (secret,))
    suppliers = []
    def supply():
        suppliers.append(True)
        return credentials
    value, _ = backend(registry, item, workspace_root, credentials=supply)
    assert suppliers == []
    original = subprocess.Popen
    def spawn(args, **kwargs):
        assert secret not in repr(args)
        assert kwargs["shell"] is False and kwargs["env"]["PROVIDER_API_KEY"] == secret
        return original(args, **kwargs)
    monkeypatch.setattr(subprocess, "Popen", spawn)
    result = execute(value, workspace_root, "stderr")
    assert result["status"] == "succeeded" and result["stderr_bytes"] > 0
    assert suppliers == [True] and credentials.env == {} and credentials.redactions == ()
    assert secret not in json.dumps(result) and secret not in repr(value.calls)


def test_secret_echo_in_stdout_is_rejected_without_leaking(tmp_path, workspace_root):
    registry, item = registration(tmp_path)
    secret = "offline-secret-not-an-output-87ee"
    value, _ = backend(registry, item, workspace_root,
                       credentials=lambda: ResolvedCredentials({"PROVIDER_API_KEY":secret}, (secret,)))
    result = execute(value, workspace_root, "secret_response")
    assert result["status"] == "pending_reconcile" and result["worker_failure"] == "secret_in_response"
    assert secret not in json.dumps(result)


def test_concurrent_stdio_pumping_handles_pipe_backpressure(tmp_path, workspace_root):
    before_read = "import sys\nsys.stderr.write('x' * 131072); sys.stderr.flush()\n"
    registry, item = registration(tmp_path, source=before_read + STUB)
    host = WorkerHost(registry, item, limits=WorkerLimits(timeout_seconds=2))
    response = host.call("execution.start", {"text":"y" * 150000}, {}, cwd=workspace_root)
    assert response.result["output"]["text"] == "y" * 150000
    assert response.stderr_bytes == 131072


def test_request_frame_limit_precedes_spawn(tmp_path, workspace_root):
    registry, item = registration(tmp_path)
    host = WorkerHost(registry, item, limits=WorkerLimits(max_frame_bytes=128))
    with pytest.raises(WorkerFailure) as error:
        host.call("execution.start", {"text":"x" * 1000}, {}, cwd=workspace_root)
    assert error.value.request_sent is False
    assert not (workspace_root / "accepted.txt").exists()


def test_events_cannot_forge_core_state_and_output_remains_typed(tmp_path, workspace_root):
    registry, item = registration(tmp_path)
    value, _ = backend(registry, item, workspace_root)
    result = execute(value, workspace_root, "forged")
    assert result["status"] == "succeeded"
    assert result["native"] == {"plugin_id":item.plugin_id}
    assert "events" not in result and "approvals" not in result and "needs_approval" not in result
    assert result["plugin_events"] == [{"provider_event_id":"evt-1", "provider_sequence":2,
                                        "category":"progress", "summary":"untrusted"}]
    assert result["usage"] == [{"meter":"requests", "used":1, "scope":"request"}]
    invalid = execute(value, workspace_root, "wrong_output")
    with pytest.raises(AsterunError): item.manifest.validate_output("local.echo", invalid["output"])


def test_streaming_start_publishes_native_reference_before_observation(tmp_path, workspace_root):
    registry, item = registration(tmp_path)
    value, _ = backend(registry, item, workspace_root)
    published = []
    result = value.dispatch_capability_stream(TaskId("task_offline"), RunId("run_offline"), "local.echo",
        {"mode":"running"}, cwd=workspace_root, publish=lambda update:published.append(deepcopy(update)), stopping=Event())
    assert published[0]["status"] == "running" and published[0]["native"]["provider_job_ref"] == "offline-job"
    assert result["status"] == "succeeded" and result["output"] == {"text":"observed"}
    assert (workspace_root / "accepted.txt").read_text().splitlines() == ["accepted"]


@pytest.mark.parametrize("confirm", [False, True])
def test_disabled_worker_can_cancel_existing_run_without_forging_termination(tmp_path, workspace_root, confirm):
    registry, item = registration(tmp_path)
    value, context = backend(registry, item, workspace_root)
    context["confirm_cancel"] = confirm
    value.bind_execution(RunId("run_offline"), context)
    registry.set_enabled(item.plugin_id, False)
    value.connection["enabled"] = False
    result = value.request_cancel(RunId("run_offline"))
    assert result["terminated"] is confirm
    assert result["status"] == ("cancelled" if confirm else "pending_reconcile")
    assert not (workspace_root / "accepted.txt").exists()
    assert value.reconcile_execution(RunId("run_offline"), cwd=workspace_root)["status"] == "pending_reconcile"


@pytest.mark.parametrize("text,kept", [
    ("ANTIGRAVITY_PERMISSION_REVIEW_REQUIRED", True),
    ("A", True),
    ("A" * 64, True),
    ("A" * 65, False),
    ("lowercase", False),
    ("HAS SPACE", False),
    ("TRAILING\n", False),
    ("", False),
    ("1LEADING_DIGIT", False),
    ("NON_ASCII_\u00c4", False),
])
def test_failed_plugin_reason_is_copied_only_when_it_matches(tmp_path, workspace_root, text, kept):
    registry, item = registration(tmp_path)
    value, _ = backend(registry, item, workspace_root)
    result = execute(value, workspace_root, "failed_reason", text=text)
    assert result["status"] == "failed" and result["terminated"] is True
    assert result["error_code"] == "PLUGIN_EXECUTION_FAILED"
    assert "reason" not in result and result["error_code"] != "PLUGIN_SUPPLIED_CODE"
    if kept:
        assert result["plugin_reason"] == text
    else:
        assert "plugin_reason" not in result


@pytest.mark.parametrize("mode", [
    "failed_reason", "failed_reason_num", "failed_plain", "succeeded_reason", "unknown_reason",
])
def test_invalid_or_non_failed_plugin_reason_is_dropped(tmp_path, workspace_root, mode):
    registry, item = registration(tmp_path)
    value, _ = backend(registry, item, workspace_root)
    result = execute(value, workspace_root, mode)
    assert "plugin_reason" not in result and "reason" not in result
    if mode.startswith("failed"):
        assert result["status"] == "failed" and result["error_code"] == "PLUGIN_EXECUTION_FAILED"
    elif mode == "succeeded_reason":
        assert result["status"] == "succeeded" and "plugin_reason" not in result
    else:
        assert result["status"] == "pending_reconcile" and result["error_code"] == "REMOTE_STATE_UNKNOWN"


def test_plugin_reason_reaches_persisted_task_get(isolated_env, workspace_root, tmp_path):
    from asterun.application import Application
    from tests.conftest import write_config

    registry, item = registration(tmp_path)
    worker, _ = backend(registry, item, workspace_root)
    normalized = execute(worker, workspace_root, "failed_reason", text="ANTIGRAVITY_PERMISSION_REVIEW_REQUIRED")
    invalid = execute(worker, workspace_root, "failed_reason", text="lowercase")
    succeeded = execute(worker, workspace_root, "succeeded_reason", text="ANTIGRAVITY_PERMISSION_REVIEW_REQUIRED")
    path = write_config(isolated_env / "reason-config.json", workspace_root)
    state = isolated_env / "reason-state"
    app = Application.from_paths(path, state)
    try:
        app.backends["fake"].dispatch = lambda *args, **kwargs: normalized
        submitted = app.handle("task.submit", {"workspace": "demo", "text": "plugin reason"})
        assert submitted.ok, submitted.error
        task_id = submitted.ids["task_id"]
        assert submitted.data["run"]["status"] == "failed"
        assert submitted.data["run"]["error_code"] == "PLUGIN_EXECUTION_FAILED"
        assert submitted.data["run"]["plugin_reason"] == "ANTIGRAVITY_PERMISSION_REVIEW_REQUIRED"
        assert submitted.data["dispatch"]["plugin_reason"] == "ANTIGRAVITY_PERMISSION_REVIEW_REQUIRED"
        assert "reason" not in submitted.data["run"]
        fetched = app.handle("task.get", {"task_id": task_id})
        assert fetched.ok and fetched.data["run"]["plugin_reason"] == "ANTIGRAVITY_PERMISSION_REVIEW_REQUIRED"
        compact = app.handle("task.get", {"task_id": task_id, "compact": True})
        assert compact.ok and compact.data["run"]["plugin_reason"] == "ANTIGRAVITY_PERMISSION_REVIEW_REQUIRED"
        app.backends["fake"].dispatch = lambda *args, **kwargs: invalid
        dropped = app.handle("task.submit", {"workspace": "demo", "text": "dropped reason"})
        assert dropped.ok, dropped.error
        assert dropped.data["run"]["error_code"] == "PLUGIN_EXECUTION_FAILED"
        assert "plugin_reason" not in dropped.data["run"]
        app.backends["fake"].dispatch = lambda *args, **kwargs: succeeded
        kept = app.handle("task.submit", {"workspace": "demo", "text": "succeeded reason"})
        assert kept.ok and kept.data["run"]["status"] == "succeeded"
        assert "plugin_reason" not in kept.data["run"]
    finally:
        app.close()
    reopened = Application.from_paths(path, state)
    try:
        fetched = reopened.handle("task.get", {"task_id": task_id})
        assert fetched.ok, fetched.error
        assert fetched.data["run"]["status"] == "failed"
        assert fetched.data["run"]["error_code"] == "PLUGIN_EXECUTION_FAILED"
        assert fetched.data["run"]["plugin_reason"] == "ANTIGRAVITY_PERMISSION_REVIEW_REQUIRED"
    finally:
        reopened.close()


def test_non_failed_run_omits_plugin_reason():
    from asterun.contracts import Run, RunStatus
    from asterun.ids import BackendId, RunId, TaskId

    run = Run(RunId("run_reason"), TaskId("task_reason"), BackendId("fake"), RunStatus.PENDING_RECONCILE,
              "2026-10-24T00:00:00Z", plugin_reason="ANTIGRAVITY_PERMISSION_REVIEW_REQUIRED")
    assert "plugin_reason" not in run.to_dict()
    run.status = RunStatus.SUCCEEDED
    assert "plugin_reason" not in run.to_dict()
    run.status = RunStatus.FAILED
    assert run.to_dict()["plugin_reason"] == "ANTIGRAVITY_PERMISSION_REVIEW_REQUIRED"


def test_rolled_back_failed_reason_stays_off_pending_reconcile(
        isolated_env, workspace_root, tmp_path, external_wheel, monkeypatch):
    from asterun.application import Application
    from asterun.contracts import RunStatus
    from asterun.ids import RunId
    from tests.test_plugin_execution import external_config

    registry, item = registration(tmp_path)
    worker, _ = backend(registry, item, workspace_root)
    failed = execute(worker, workspace_root, "failed_reason", text="ANTIGRAVITY_PERMISSION_REVIEW_REQUIRED")
    assert failed["status"] == "failed"
    assert failed["plugin_reason"] == "ANTIGRAVITY_PERMISSION_REVIEW_REQUIRED"
    path = external_config(isolated_env, workspace_root, external_wheel)
    app = Application.from_paths(path, isolated_env / "reason-rollback-state")
    seen = []
    try:
        original = app.store.save_run

        def fail_terminal_save(saved):
            seen.append((saved.status, saved.plugin_reason))
            if saved.status == RunStatus.FAILED and isinstance(saved.plugin_reason, str):
                raise AsterunError("INVALID_REQUEST", "injected terminal save failure")
            return original(saved)

        def reject(application, run, result):
            monkeypatch.setattr(application.store, "save_run", fail_terminal_save)

        monkeypatch.setattr("asterun.plugins.core_integration.ingest_result", reject)
        app.backends["external"].dispatch = lambda *args, **kwargs: failed
        submitted = app.handle("task.submit", {"workspace": "demo", "text": "plugin reason rollback"})
        assert submitted.ok, submitted.error
        assert submitted.data["run"]["status"] == "pending_reconcile"
        assert submitted.data["run"]["error_code"] == "REMOTE_STATE_UNKNOWN"
        assert "plugin_reason" not in submitted.data["run"]
        stored = app.store.get_run(RunId(submitted.ids["run_id"]))
        assert stored.status == RunStatus.PENDING_RECONCILE
        assert stored.plugin_reason is None
        fetched = app.handle("task.get", {"task_id": submitted.ids["task_id"]})
        assert fetched.ok, fetched.error
        assert fetched.data["run"]["status"] == "pending_reconcile"
        assert "plugin_reason" not in fetched.data["run"]
        compact = app.handle("task.get", {"task_id": submitted.ids["task_id"], "compact": True})
        assert compact.ok and "plugin_reason" not in compact.data["run"]
        assert any(status == RunStatus.FAILED and reason == "ANTIGRAVITY_PERMISSION_REVIEW_REQUIRED"
                   for status, reason in seen)
        assert any(status == RunStatus.PENDING_RECONCILE for status, _reason in seen)
        assert all(reason is None for status, reason in seen if status == RunStatus.PENDING_RECONCILE)
    finally:
        app.close()


def test_unbound_execution_and_input_schema_failure_cannot_spawn(tmp_path, workspace_root):
    registry, item = registration(tmp_path)
    value, _ = backend(registry, item, workspace_root)
    value.close()
    with pytest.raises(AsterunError): execute(value, workspace_root)
    value, _ = backend(registry, item, workspace_root)
    with pytest.raises(AsterunError): execute(value, workspace_root, unexpected=True)
    assert not (workspace_root / "accepted.txt").exists()
