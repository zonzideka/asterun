"""P05 独立 wheel、空 native HOME、真实本地 fixture 进程的离线验收。"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

from asterun.application import Application
from asterun.plugin_api import PluginManifest
from asterun.plugins.registry import PluginRegistry, installation_digest
from asterun.plugins.worker import WorkerFailure, WorkerHost, WorkerLimits
from tests.test_plugin_execution import external_config

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "packages/asterun-plugin-antigravity"


@pytest.fixture(scope="module")
def antigravity_installation(tmp_path_factory):
    root = tmp_path_factory.mktemp("antigravity-plugin-install").resolve()
    env = {"PATH": os.pathsep.join([str(Path(sys.executable).parent), "/usr/bin", "/bin"]),
           "HOME": str(root / "build-home"), "PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
           "PYTHONDONTWRITEBYTECODE": "1"}
    Path(env["HOME"]).mkdir(mode=0o700)
    def run(args):
        result = subprocess.run(args, env=env, cwd=root, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout
    proof = json.loads(run([sys.executable, str(PACKAGE / "scripts/verify-source.py")]))
    assert proof["original_sources_unchanged"] and proof["verified_files"] == 3
    source = root / "source"
    shutil.copytree(PACKAGE, source)
    run([sys.executable, "-m", "pip", "wheel", "--no-build-isolation", "--no-deps", "-w", str(root / "wheels"), str(source)])
    wheel = next((root / "wheels").glob("*.whl"))
    run([sys.executable, "-m", "venv", str(root / "venv")])
    python = root / "venv/bin/python"
    run([str(python), "-m", "pip", "install", "--no-deps", str(wheel)])
    info = json.loads(run([str(python), "-c", "import importlib.util,json,sysconfig; print(json.dumps({'core': importlib.util.find_spec('asterun') is None, 'library': sysconfig.get_path('purelib')}))"]))
    assert info["core"] is True
    library = Path(info["library"])
    return {"wheel": wheel, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
            "manifest": library / "asterun_plugin_antigravity/manifest.json",
            "runner": [str(python), "-m", "asterun_plugin_antigravity.worker"],
            "runtime_path": library, "python": python, "env": env}


def setup_native(root, workspace, installation, mode="happy"):
    root, workspace = root.resolve(), workspace.resolve()
    home = root / "native-home"
    profile = [str(installation["python"]), "-m", "asterun_plugin_antigravity._vendor.profile"]
    for args in (["prepare", "--home", str(home)], ["bind-workspace", "--home", str(home), "--workspace", str(workspace)]):
        result = subprocess.run([*profile, *args], env=installation["env"], cwd=root, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout)["authentication_verified"] is False
    assert not list(home.rglob("auth.json"))
    marker = root / "native-marker.txt"
    binary = root / "native-fixture.py"
    wrapper = (REPO / "tests/fixtures/antigravity_plugin_cli.py").read_text()
    for old, new in (("__FIXTURE_PATH__", str(REPO / "tests/fixtures/antigravity_cli.py")),
                     ("__MODE__", mode), ("__MARKER_PATH__", str(marker))):
        wrapper = wrapper.replace(old, new)
    binary.write_text(f'#!{installation["python"]}\n' + wrapper)
    binary.chmod(0o700)
    options = {"bin": str(binary), "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
               "home": str(home), "model": "fixture-model", "execution_enabled": True, "timeout_seconds": 2}
    context = {"plan_id": "plan-fixture", "binding_id": "binding-fixture", "task_id": "task-fixture",
        "run_id": "run-fixture", "capability": "agent.execute", "provider_account_ref": "provider-fixture",
        "auth_mode": "native_account", "upstream_version": "1.2.0", "options": options,
        "workspace_root": str(workspace)}
    return {"home": home, "binary": binary, "marker": marker, "options": options, "context": context, "workspace": workspace}


def host_for(installation, *, timeout=10):
    registry = PluginRegistry()
    registration = registry.register_external(installation["manifest"], runner=installation["runner"],
        installation_path=installation["wheel"], installation_sha256=installation["sha256"], enabled=True,
        runtime_path=installation["runtime_path"], runtime_sha256=installation_digest(installation["runtime_path"]))
    return WorkerHost(registry, registration, limits=WorkerLimits(timeout_seconds=timeout))


def call_native(installation, native, *, method="execution.start", context=None, timeout=10):
    return host_for(installation, timeout=timeout).call(method, {"text": "offline prompt"}, context or native["context"],
        cwd=native["workspace"], environment={"HOME": str(native["home"])}).result


def plugin_config(root, workspace, installation, native):
    path = external_config(root, workspace, installation)
    raw = json.loads(path.read_text())
    raw["plugins"]["google.antigravity-cli"] = raw["plugins"].pop("example.external-fake")
    connection = raw["connections"]["external"]
    connection.update(plugin_id="google.antigravity-cli", auth_mode="native_account", upstream_version="1.2.0", options=native["options"])
    provider = raw["provider_accounts"][connection["provider_account_ref"]]
    provider.update(provider="google", credential_ref="native-home:" + str(native["home"]))
    pool = raw["billing_pools"][connection["billing_pool_refs"][0]]
    pool.update(billing_mode="subscription", remaining=None, overage_verified=True,
                allow_unknown_subscription=True, meter="tokens", local_limit=100, estimate_per_action=30)
    path.write_text(json.dumps(raw))
    return path


def submit(app):
    result = app.handle("task.submit", {"workspace": "demo", "text": "offline prompt"})
    assert result.ok, result.error
    return result


def test_standalone_manifest_describe_and_no_native_discovery(antigravity_installation, tmp_path):
    manifest = PluginManifest.from_path(antigravity_installation["manifest"])
    assert manifest.plugin_id == "google.antigravity-cli"
    assert manifest.capability("agent.resume")["support"] == "unsupported"
    host = host_for(antigravity_installation)
    reply = host.call("plugin.describe", {}, {}, cwd=tmp_path)
    assert reply.result["manifest"] == manifest.to_dict()
    inspect = host.call("connection.inspect", {}, {}, cwd=tmp_path).result
    assert inspect["authenticated"] == "not_verified" and inspect["billing_verified"] is False
    quota = host.call("usage.observe", {}, {}, cwd=tmp_path).result
    assert quota["remaining"] is None and quota["quota"] == "unsupported"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("mode,status,category", [
    ("happy", "succeeded", "native_terminal"), ("soft_deny", "failed", "tool_permission_denied"),
    ("split_soft_deny", "failed", "tool_permission_denied"), ("tool_error", "failed", "native_tool_failed"),
    ("tool_state_error_no_info", "failed", "native_tool_failed"), ("success_error", "failed", "native_run_failed"),
    ("quota", "failed", "native_run_failed"), ("error", "failed", "native_run_failed"),
    ("success_nonzero", "pending_reconcile", "native_result_unknown"),
    ("result_only", "pending_reconcile", "native_result_unknown"), ("eof", "pending_reconcile", "native_result_unknown"),
    ("malformed", "pending_reconcile", "native_result_unknown"), ("unsafe_permission", "pending_reconcile", "native_result_unknown"),
    ("id_mismatch", "pending_reconcile", "native_result_unknown"), ("active_tool", "pending_reconcile", "native_result_unknown"),
    ("model_mismatch", "pending_reconcile", "native_result_unknown"), ("CANCELED", "cancelled", "native_terminal"),
])
def test_native_protocol_terminals_and_failure_categories(antigravity_installation, isolated_env, workspace_root, mode, status, category):
    native = setup_native(isolated_env, workspace_root, antigravity_installation, mode)
    result = call_native(antigravity_installation, native)
    assert result["status"] == status, result
    assert result["events"][0]["category"] == "antigravity." + category
    assert "provider_turn_id" not in result
    assert "private diagnostic" not in json.dumps(result) and "private reasoning" not in json.dumps(result)
    if mode == "happy":
        assert result["output"] == {"text": "fixture response"}
        assert result["provider_session_id"]
        assert result["usage"][0]["scope_ref"] == result["provider_session_id"]
        assert result["usage"][0]["used"] == 10
    assert native["marker"].read_text() == "version\ndispatch\n"


@pytest.mark.parametrize("field,value,reason", [
    ("execution_enabled", False, "execution_not_enabled"), ("model", "--unsafe", "permission_profile_rejected"),
    ("binary_sha256", "0" * 64, "binary_digest_mismatch"), ("home", "/not-the-injected-home", "native_home_mismatch"),
    ("timeout_seconds", 21, "invalid_timeout"), ("environment", {}, "invalid_options"),
])
def test_pinned_options_fail_before_native_spawn(antigravity_installation, isolated_env, workspace_root, field, value, reason):
    native = setup_native(isolated_env, workspace_root, antigravity_installation)
    native["context"]["options"][field] = value
    result = call_native(antigravity_installation, native)
    assert result["status"] == "failed" and result["reason"] == reason
    assert not native["marker"].exists()


@pytest.mark.parametrize("field,value,reason", [("auth_mode", "api_key", "auth_mode_mismatch"),
    ("upstream_version", "1.2.1", "upstream_version_mismatch"), ("provider_session_id", "exact-native-session", "native_resume_unsupported"),
    ("capability", "agent.resume", "capability_unsupported")])
def test_context_mode_and_explicit_resume_never_fallback(antigravity_installation, isolated_env, workspace_root, field, value, reason):
    native = setup_native(isolated_env, workspace_root, antigravity_installation)
    native["context"][field] = value
    result = call_native(antigravity_installation, native)
    assert result["reason"] == reason and not native["marker"].exists()


@pytest.mark.parametrize("mutation", ["credits", "write", "permission", "unbound", "workspace_rules", "home_mcp"])
def test_effective_profile_rejects_weaker_native_permissions(antigravity_installation, isolated_env, workspace_root, mutation):
    native = setup_native(isolated_env, workspace_root, antigravity_installation)
    settings_path = native["home"] / ".gemini/antigravity-cli/settings.json"
    settings = json.loads(settings_path.read_text())
    if mutation == "credits":
        settings["useG1Credits"] = True
    elif mutation == "write":
        settings["permissions"]["deny"].remove("write_file(*)")
    elif mutation == "permission":
        settings["toolPermission"] = "always-proceed"
    elif mutation == "unbound":
        settings["permissions"]["allow"] = []
    elif mutation == "workspace_rules":
        (workspace_root / "AGENTS.md").write_text("untrusted extra rules")
    else:
        (native["home"] / ".gemini/config/mcp_config.json").write_text('{"mcpServers":{"remote":{}}}')
    settings_path.write_text(json.dumps(settings))
    before = settings_path.read_bytes()
    result = call_native(antigravity_installation, native)
    assert result["status"] == "failed" and result["reason"] in {"permission_profile_rejected", "workspace_read_binding_required"}
    assert not native["marker"].exists() and settings_path.read_bytes() == before


def test_actual_binary_version_is_checked_without_dispatch(antigravity_installation, isolated_env, workspace_root):
    native = setup_native(isolated_env, workspace_root, antigravity_installation)
    native["binary"].write_text(native["binary"].read_text().replace('print("1.2.0")', 'print("1.2.1")'))
    native["options"]["binary_sha256"] = hashlib.sha256(native["binary"].read_bytes()).hexdigest()
    result = call_native(antigravity_installation, native)
    assert result["reason"] == "upstream_version_mismatch"
    assert native["marker"].read_text() == "version\n"


@pytest.mark.parametrize("method", ["execution.observe", "execution.cancel", "execution.reconcile"])
def test_recovery_preserves_exact_native_id_but_never_replays(antigravity_installation, isolated_env, workspace_root, method):
    native = setup_native(isolated_env, workspace_root, antigravity_installation)
    native["context"]["provider_session_id"] = "exact-native-session"
    result = call_native(antigravity_installation, native, method=method)
    assert result["status"] == "pending_reconcile" and result["terminated_verified"] is False
    assert result["provider_session_id"] == "exact-native-session" and result["supported"] is False
    assert not native["marker"].exists()


def test_core_application_records_native_cumulative_once_and_retains_failure_category(antigravity_installation, isolated_env, workspace_root):
    native = setup_native(isolated_env, workspace_root, antigravity_installation, "cumulative")
    config = plugin_config(isolated_env, workspace_root, antigravity_installation, native)
    app = Application.from_paths(config, isolated_env / "state")
    try:
        result = submit(app)
        assert result.data["run"]["status"] == "succeeded", result.to_dict()
        run = app.store.get_run(app.store.get_task(app.store.list_task_ids()[0]).current_run_id)
        assert run.output == {"text": "fixture response"} and run.native["provider_session_id"]
        pool = next(iter(app.config.billing_pools))
        rows = app.admission.store.observations(pool)
        assert len(rows) == 1 and rows[0]["observation"]["used"] == 30
        observation = rows[0]["observation"]
        assert observation["scope"] == "session_cumulative" and observation["scope_ref"] == run.native["provider_session_id"]
        duplicate = app.admission.store.record_observation(deepcopy(observation))
        assert duplicate == rows[0] and len(app.admission.store.observations(pool)) == 1
        app._apply_backend_result(run, {"status": "succeeded", "terminated": True, "native": dict(run.native),
            "usage": [{key: value for key, value in observation.items() if key not in {"pool_ref", "source_kind"}}]}, "backend.external")
        assert len(app.admission.store.observations(pool)) == 1
        events = [event for event in app.store.list_events(run.id) if event.source == "plugin.observation"]
        assert events[0].payload["observation"]["category"] == "antigravity.native_terminal"
    finally:
        app.close()


@pytest.mark.parametrize("mode,expected", [("soft_deny", "antigravity.tool_permission_denied"),
                                          ("quota", "antigravity.native_run_failed")])
def test_core_preserves_native_failure_classification(antigravity_installation, isolated_env, workspace_root, mode, expected):
    native = setup_native(isolated_env, workspace_root, antigravity_installation, mode)
    config = plugin_config(isolated_env, workspace_root, antigravity_installation, native)
    app = Application.from_paths(config, isolated_env / "state")
    try:
        result = submit(app)
        assert result.data["run"]["status"] == "failed", result.to_dict()
        run = app.store.get_run(app.store.get_task(app.store.list_task_ids()[0]).current_run_id)
        categories = [event.payload["observation"]["category"] for event in app.store.list_events(run.id) if event.source == "plugin.observation"]
        assert expected in categories
        assert "private diagnostic" not in json.dumps(result.to_dict())
    finally:
        app.close()


def test_core_recovery_after_eof_and_disable_does_not_create_again(antigravity_installation, isolated_env, workspace_root):
    native = setup_native(isolated_env, workspace_root, antigravity_installation, "eof")
    config = plugin_config(isolated_env, workspace_root, antigravity_installation, native)
    state = isolated_env / "state"
    app = Application.from_paths(config, state)
    first = submit(app)
    assert first.data["run"]["status"] == "pending_reconcile"
    session = first.data["run"]["native"]["provider_session_id"]
    assert app.handle("plugin.disable", {"plugin_id": "google.antigravity-cli"}).ok
    app.close()
    app = Application.from_paths(config, state)
    try:
        reconcile = app.handle("task.reconcile", {"task_id": first.ids["task_id"]})
        assert reconcile.ok, reconcile.error
        reconciled = app.task_get(first.ids["task_id"])
        assert reconciled.data["run"]["status"] == "pending_reconcile"
        assert reconciled.data["run"]["native"]["provider_session_id"] == session
        cancel = app.handle("task.cancel", {"task_id": first.ids["task_id"]})
        assert cancel.ok and cancel.data["terminated"] is False
        assert native["marker"].read_text() == "version\ndispatch\n"
        assert app.admission.store.binding_for_run(first.ids["run_id"])["status"] == "uncertain"
    finally:
        app.close()


def test_worker_deadline_kills_actual_native_child_in_shared_group(antigravity_installation, isolated_env, workspace_root, monkeypatch):
    from types import SimpleNamespace
    from asterun.plugins import worker

    native = setup_native(isolated_env, workspace_root, antigravity_installation, "timeout")
    native["options"]["timeout_seconds"] = 20
    pid_path = native["marker"].with_suffix(".pid")
    started = []
    startup_deadline = time.monotonic() + 10

    def host_clock():
        # Exercise deadline cleanup after a real child has started, independent
        # of interpreter imports and preflight speed. Other clocks stay real.
        assert time.monotonic() < startup_deadline, "离线原生 child 未在启动期限内就绪"
        if not started and pid_path.exists():
            text = pid_path.read_text().strip()
            if text.isdecimal():
                pid = int(text)
                os.kill(pid, 0)
                started.append(pid)
        return 1.0 if started else 0.0

    with monkeypatch.context() as patch:
        patch.setattr(worker, "time", SimpleNamespace(monotonic=host_clock))
        with pytest.raises(WorkerFailure) as failed:
            call_native(antigravity_installation, native, timeout=.8)
    assert failed.value.request_sent and failed.value.reason == "timeout"
    assert pid_path.exists(), "离线原生 child 必须已真正启动"
    pid = int(pid_path.read_text())
    assert started == [pid]
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        state = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
        if state.returncode != 0 or state.stdout.strip().startswith("Z"):
            break
        time.sleep(.02)
    else:
        pytest.fail("宿主截止后 fixture 原生 child 仍在运行")


def test_quality_role_cannot_use_external_execution_as_snapshot_review(antigravity_installation, isolated_env, workspace_root):
    native = setup_native(isolated_env, workspace_root, antigravity_installation)
    config = plugin_config(isolated_env, workspace_root, antigravity_installation, native)
    raw = json.loads(config.read_text())
    raw["workflow"] = {"require_review": True, "review_backend": "external"}
    config.write_text(json.dumps(raw))
    app = Application.from_paths(config, isolated_env / "state")
    try:
        first = submit(app)
        before = native["marker"].read_text()
        started = app.handle("workflow.start", {"task_id": first.ids["task_id"], "idempotency_key": "quality",
            "target_paths": ["note.txt"], "checks": [{"kind": "file_exists", "path": "note.txt"}]})
        assert started.ok, started.error
        for _ in range(3):
            app.poll()
        result = app.task_get(first.ids["task_id"])
        assert result.data["task"]["acceptance"] != "passed"
        assert result.data["task"]["paused"]
        assert result.data["task"]["review_status"] == "unavailable"
        assert result.data["task"]["quality"]["blocked_reason"]["code"] == "CAPABILITY_UNSUPPORTED"
        assert native["marker"].read_text() == before
    finally:
        app.close()


def test_default_execution_gate_is_off_in_core_and_explained(antigravity_installation, isolated_env, workspace_root):
    native = setup_native(isolated_env, workspace_root, antigravity_installation)
    native["options"].pop("execution_enabled")
    config = plugin_config(isolated_env, workspace_root, antigravity_installation, native)
    app = Application.from_paths(config, isolated_env / "state")
    try:
        result = submit(app)
        assert result.data["run"]["status"] == "failed"
        assert result.data["run"]["output"] == {"text": "execution_not_enabled"}
        run = app.store.get_run(app.store.get_task(app.store.list_task_ids()[0]).current_run_id)
        events = [event.payload["observation"] for event in app.store.list_events(run.id) if event.source == "plugin.observation"]
        assert events[0]["category"] == "antigravity.preflight.execution_not_enabled"
        assert not native["marker"].exists()
    finally:
        app.close()


def test_native_process_never_receives_parent_api_keys(antigravity_installation, isolated_env, workspace_root, monkeypatch):
    native = setup_native(isolated_env, workspace_root, antigravity_installation)
    monkeypatch.setenv("GEMINI_API_KEY", "fixture-secret-never-real")
    monkeypatch.setenv("GOOGLE_API_KEY", "fixture-secret-never-real")
    monkeypatch.setenv("ANTIGRAVITY_PERM_GRANTS", "untrusted")
    result = call_native(antigravity_installation, native)
    assert result["status"] == "succeeded"  # fixture 进程实际断言了环境中不存在这些字段。
    assert "fixture-secret" not in json.dumps(result)


def test_version_probe_failure_never_exposes_native_stderr(antigravity_installation, isolated_env, workspace_root):
    native = setup_native(isolated_env, workspace_root, antigravity_installation)
    native["binary"].write_text(f'#!{antigravity_installation["python"]}\nimport sys\nprint("fixture-secret-diagnostic", file=sys.stderr)\nraise SystemExit(2)\n')
    native["options"]["binary_sha256"] = hashlib.sha256(native["binary"].read_bytes()).hexdigest()
    result = call_native(antigravity_installation, native)
    assert result["status"] == "failed" and result["reason"] == "upstream_version_mismatch"
    assert "fixture-secret" not in json.dumps(result)


@pytest.mark.parametrize("raw", [b"bad-json\n", b'{"jsonrpc":"2.0","id":1,"method":"execution.start","params":{}}\n',
    b'{"jsonrpc":"2.0","id":1,"method":"execution.start","params":{"protocol_version":"asterun-worker/v2"}}\n',
    b'{"jsonrpc":"2.0","id":1,"method":"execution.start","params":{"protocol_version":"asterun-worker/v1","input":{},"context":{},"unexpected":true}}\n',
    b'{"jsonrpc":"2.0","jsonrpc":"2.0","id":1,"method":"plugin.describe","params":{"protocol_version":"asterun-worker/v1"}}\n'])
def test_standalone_worker_rejects_bad_wire_before_any_native_spawn(antigravity_installation, tmp_path, raw):
    result = subprocess.run(antigravity_installation["runner"], input=raw, env=antigravity_installation["env"],
                            cwd=tmp_path, capture_output=True, timeout=5)
    reply = json.loads(result.stdout)
    assert reply["error"] == {"code": -32600, "message": "invalid_request"}
    assert result.stderr == b"" and not list(tmp_path.iterdir())
