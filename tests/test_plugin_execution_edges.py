"""独立审查复现：接受引用不丢失，已确认停用抵达线程内的最终派发检查。"""

from threading import Event
import time

import pytest

from asterun.application import Application
from asterun.contracts import RunStatus
from tests.test_plugin_execution import plugin_config, invoke


def test_invalid_typed_output_keeps_accepted_native_reference_for_reconcile(plugin_config, monkeypatch):
    app = Application.from_paths(plugin_config, plugin_config.parent / "state")
    try:
        # 经 worker 净化的原生接受引用有效，但业务 output 不满足声明的 schema。
        monkeypatch.setattr(app.backends["external"], "dispatch_capability", lambda *a, **k: {
            "status": "succeeded", "terminated": True,
            "native": {"plugin_id": "example.external-fake", "provider_job_ref": "accepted-job-42"},
            "output": {"wrong": "not expected text"}})
        response = invoke(app)
        run = app.store.get_run(app.store.get_task(app.store.list_task_ids()[0]).current_run_id)
        assert run.status == RunStatus.PENDING_RECONCILE
        assert run.native["provider_job_ref"] == "accepted-job-42"
        assert run.output is None
        assert app.admission.store.binding_for_run(run.id.value)["status"] == "uncertain"
        reconcile = app.handle("task.reconcile", {"task_id": response.ids["task_id"]})
        assert reconcile.ok, reconcile.error
        context, _ = app.backends["external"]._binding(run.id)
        assert context["provider_job_ref"] == "accepted-job-42"
    finally:
        app.close()


def test_disable_reaches_worker_guard_after_credentials_wait(plugin_config, monkeypatch):
    app = Application.from_paths(plugin_config, plugin_config.parent / "state", background=True)
    entered, resume = Event(), Event()
    backend = app.backends["external"]
    original = backend._resolve
    def held_before_final_call(*args, **kwargs):
        entered.set()
        assert resume.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(backend, "_resolve", held_before_final_call)
    try:
        # 若错派，真实隔离 fixture 会在工作区写入 accepted.txt 再退出。
        response = invoke(app, "crash")
        assert response.ok and entered.wait(5)
        disabled = app.handle("plugin.disable", {"plugin_id": "example.external-fake"})
        assert disabled.ok and not disabled.data["new_execution_allowed"]
        assert not backend.can_dispatch()
        resume.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            app.poll()
            run = app.store.get_run(app.store.get_task(app.store.list_task_ids()[0]).current_run_id)
            if run.status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.PENDING_RECONCILE}:
                break
            time.sleep(.01)
        assert run.status in {RunStatus.FAILED, RunStatus.PENDING_RECONCILE}
        assert not (app.config.workspaces["demo"].root / "accepted.txt").exists()
    finally:
        resume.set()
        app.close()


def test_disable_is_reapplied_to_worker_registry_before_restart_recovery(plugin_config):
    app = Application.from_paths(plugin_config, plugin_config.parent / "state")
    assert app.handle("plugin.disable", {"plugin_id": "example.external-fake"}).ok
    app.close()
    reopened = Application.from_paths(plugin_config, plugin_config.parent / "state")
    try:
        assert not reopened.backends["external"].can_dispatch()
        assert reopened.handle("plugin.enable", {"plugin_id": "example.external-fake"}).ok
        assert reopened.backends["external"].can_dispatch()
        result = invoke(reopened)
        assert result.ok and result.data["run"]["status"] == "succeeded"
    finally:
        reopened.close()


@pytest.mark.parametrize("public_input", ["request", "response"])
def test_background_typed_input_does_not_alias_public_request_or_response(plugin_config, monkeypatch, public_input):
    app = Application.from_paths(plugin_config, plugin_config.parent / "state", background=True)
    entered, resume = Event(), Event()
    backend = app.backends["external"]
    original = backend.dispatch_capability_stream
    def held_before_backend_validation(*args, **kwargs):
        entered.set()
        assert resume.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(backend, "dispatch_capability_stream", held_before_backend_validation)
    payload = {"workspace": "demo", "backend": "external", "capability": "local.echo",
               "input": {"text": "prepared input"}, "idempotency_key": "immutable-input"}
    try:
        response = app.handle("capability.invoke", payload)
        assert response.ok and entered.wait(5)
        exposed = payload["input"] if public_input == "request" else response.data["task"]["capability_input"]
        exposed["text"] = "changed after admission"
        resume.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            app.poll()
            task = app.store.get_task(app.store.list_task_ids()[0])
            run = app.store.get_run(task.current_run_id)
            if run.status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.PENDING_RECONCILE}:
                break
            time.sleep(.01)
        assert task.capability_input == {"text": "prepared input"}
        assert run.status == RunStatus.SUCCEEDED
        assert run.output == {"text": "prepared input"}
    finally:
        resume.set()
        app.close()
