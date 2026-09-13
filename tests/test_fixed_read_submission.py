from __future__ import annotations

import hashlib
import json

import pytest

from asterun.application import Application, _input_hash
from asterun.contracts import Task
from asterun.errors import AsterunError
from asterun.requests import validate_request
from asterun.review_recipe import _read, _read_manifest, prepare_review, submit_review
from tests.test_native_lifecycle import native_config, wait_for
from tests.test_review_recipe import setup, prepare, Hub


def scope(root, name="one"):
    content = b"fixed source\n"
    (root / (name + ".txt")).write_bytes(content)
    manifest = {"schema": "asterun-read-scope/v1", "files": [
        {"name": "source.txt", "path": name + ".txt", "size": len(content),
         "sha256": hashlib.sha256(content).hexdigest()}]}
    path = root / (name + ".json")
    path.write_text(json.dumps(manifest))
    return {"manifest_path": path.name, "manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def isolated_dispatch(app, captured):
    def dispatch(*args, **kwargs):
        captured.append(kwargs.get("read_scope"))
        return {"status": "succeeded", "terminated": True, "summary": "offline",
                "native": {"thread_id": "thread-offline", "turn_id": "turn-offline"}}
    app.backends["codex"].dispatch_stream = dispatch


def test_scope_is_persisted_input_bound_and_passed_to_executor(native_config, isolated_env, workspace_root):
    app = Application.from_paths(native_config, isolated_env / "state", background=True)
    captured = []
    isolated_dispatch(app, captured)
    binding = scope(workspace_root)
    try:
        submitted = app.handle("task.submit", {"workspace": "demo", "backend": "codex", "text": "read",
            "read_scope": binding, "idempotency_key": "fixed-one"})
        assert submitted.ok, submitted.error
        result = wait_for(app, submitted.ids["task_id"], lambda data: data["run"]["status"] == "succeeded")
        assert result["approval"] is None
        task = result["task"]
        assert task["read_scope"] == binding
        assert Task.from_dict(task).to_dict()["read_scope"] == binding
        assert task["input_hash"] == _input_hash("demo", "read", "success", "codex", None, "", read_scope=binding)
        assert task["input_hash"] != _input_hash("demo", "read", "success", "codex", None, "")
        assert len(captured) == 1 and captured[0].binding == binding
        app.close()
        app = Application.from_paths(native_config, isolated_env / "state", background=True)
        recovered = app.handle("task.get", {"task_id": submitted.ids["task_id"]})
        assert recovered.ok and recovered.data["task"]["read_scope"] == binding
    finally:
        app.close()


@pytest.mark.parametrize("initial,following", [("scope", None), (None, "scope"), ("scope", "other")])
def test_conversation_cannot_change_fixed_read_scope(native_config, isolated_env, workspace_root, initial, following):
    app = Application.from_paths(native_config, isolated_env / "state", background=True)
    captured = []
    isolated_dispatch(app, captured)
    bindings = {None: None, "scope": scope(workspace_root), "other": scope(workspace_root, "two")}
    try:
        payload = {"workspace": "demo", "backend": "codex", "text": "first"}
        if initial:
            payload["read_scope"] = bindings[initial]
        first = app.handle("task.submit", payload)
        assert first.ok, first.error
        wait_for(app, first.ids["task_id"], lambda data: data["run"]["status"] == "succeeded")
        second = {"workspace": "demo", "backend": "codex", "text": "second",
                  "conversation_id": first.ids["conversation_id"]}
        if following:
            second["read_scope"] = bindings[following]
        result = app.handle("task.submit", second)
        assert not result.ok and result.error["code"] == "BINDING_MISMATCH"
        assert len(captured) == 1 and len(app.store.list_task_ids()) == 1
    finally:
        app.close()


def test_invalid_scope_rejected_before_intent(native_config, isolated_env, workspace_root):
    app = Application.from_paths(native_config, isolated_env / "state", background=True)
    binding = scope(workspace_root)
    binding["manifest_sha256"] = "0" * 64
    try:
        result = app.handle("task.submit", {"workspace": "demo", "backend": "codex", "read_scope": binding})
        assert not result.ok and not app.store.list_task_ids()
    finally:
        app.close()


def test_preflight_rejection_proves_no_prompt_only_for_exact_bound_run(
        native_config, isolated_env, workspace_root, monkeypatch):
    from asterun.ids import RunId
    from asterun.backends import codex_protocol
    def unsupported(_):
        raise AsterunError("CAPABILITY_UNSUPPORTED", "fixture version unsupported")
    monkeypatch.setattr(codex_protocol, "preflight_codex_read_scope", unsupported)
    app = Application.from_paths(native_config, isolated_env / "state", background=True)
    try:
        first = app.handle("task.submit", {"workspace": "demo", "backend": "codex",
            "read_scope": scope(workspace_root), "text": "read"})
        assert first.ok, first.error
        wait_for(app, first.ids["task_id"], lambda data: data["run"]["status"] == "failed")
        run = app.store.get_run(RunId(first.ids["run_id"]))
        assert app.control._confirmed_prompt_not_sent(run)
        run.native["asterun.codex_dispatch_v1"]["run_id"] = "different-run"
        assert not app.control._confirmed_prompt_not_sent(run)
        assert not (workspace_root / "native-calls.jsonl").exists()
    finally:
        app.close()


def test_profile_never_silently_falls_back_to_fake(app, workspace_root):
    result = app.handle("task.submit", {"workspace": "demo", "read_scope": scope(workspace_root)})
    assert not result.ok and result.error["code"] == "CAPABILITY_UNSUPPORTED"
    assert not app.store.list_task_ids()


def test_schema_and_legacy_hash_remain_compatible():
    old = {"workspace": "demo", "text": "read"}
    assert validate_request("task.submit", old) == old
    with pytest.raises(AsterunError):
        validate_request("task.submit", {**old, "read_scope": {"manifest_path": "scope.json"}})
    assert _input_hash("demo", "read", "success", "codex", None, "") == hashlib.sha256(
        b'{"backend":"codex","conversation_id":"","path":null,"script":"success","text":"read","workspace":"demo"}').hexdigest()


def test_new_codex_recipe_submits_fixed_scope(setup):
    client, state, hub = setup
    result = prepare(setup)
    assert result["reading"]["mode"] == "native_fixed_scope"
    manifest = _read(state / "reviews" / result["id"] / "manifest.json")
    from asterun.read_scope import FixedReadScope
    reader = FixedReadScope(manifest["workspace_root"], manifest["read_scope"])
    assert {row["name"] for row in reader.files} == {"source/code.py", "pr.diff"}
    submit_review(client, state, result["id"], github=hub)
    request = next(payload for method, payload in client.calls if method == "task.submit")
    assert request["read_scope"] == manifest["read_scope"]
    validate_request("task.submit", request)


def test_existing_manual_attempt_is_not_silently_migrated(setup):
    client, state, hub = setup
    first = prepare_review(client, state, workspace="review", backend="codex", repo="owner/repo", pr=55,
                           attempt="one", github=hub, read_mode="manual")
    assert first["reading"]["mode"] == "manual_approval"
    assert prepare(setup) == first
    with pytest.raises(AsterunError):
        prepare_review(client, state, workspace="review", backend="codex", repo="owner/repo", pr=55,
                       attempt="one", github=hub, read_mode="native")
    submit_review(client, state, first["id"], github=hub)
    assert "read_scope" not in next(payload for method, payload in client.calls if method == "task.submit")


def test_review_manifest_cannot_expand_scope_to_another_workspace_file(setup):
    from pathlib import Path
    client, state, hub = setup
    result = prepare(setup)
    path = state / "reviews" / result["id"] / "manifest.json"
    manifest = _read(path)
    root = Path(manifest["workspace_root"])
    (root / "unrelated.txt").write_bytes(b"outside review")
    scope_path = root / manifest["read_scope"]["manifest_path"]
    document = json.loads(scope_path.read_text())
    document["files"].append({"name": "source/unrelated.txt", "path": "unrelated.txt",
        "size": 14, "sha256": hashlib.sha256(b"outside review").hexdigest()})
    scope_path.chmod(0o600)
    scope_path.write_text(json.dumps(document))
    manifest["read_scope"]["manifest_sha256"] = hashlib.sha256(scope_path.read_bytes()).hexdigest()
    manifest["task_input_hash"] = _input_hash("review", manifest["prompt"], "success", "codex", None, "",
                                             read_scope=manifest["read_scope"])
    path.write_text(json.dumps(manifest))
    with pytest.raises(AsterunError, match="固定快照"):
        submit_review(client, state, result["id"], github=hub)
    assert not client.results


def test_bad_persisted_scope_has_structured_failure(setup):
    result = prepare(setup)
    path = setup[1] / "reviews" / result["id"] / "manifest.json"
    manifest = _read(path)
    manifest["read_scope"] = []
    path.write_text(json.dumps(manifest))
    with pytest.raises(AsterunError):
        _read_manifest(path)
