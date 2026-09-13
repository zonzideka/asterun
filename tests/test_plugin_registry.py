from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from asterun.errors import AsterunError
from asterun.plugin_api import (
    PluginManifest, WORKER_PROTOCOL_VERSION, validate_worker_request, validate_worker_response,
)
from asterun.plugins.registry import PluginRegistry, installation_digest


@pytest.fixture
def manifest_raw():
    path = Path(__file__).parent / "fixtures/plugins/external_fake/src/asterun_external_fake/manifest.json"
    return json.loads(path.read_text())


@pytest.fixture
def registration_files(tmp_path, manifest_raw):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(manifest_raw))
    artifact = tmp_path / "plugin.whl"
    artifact.write_bytes(b"test registration artifact; real wheel installation has a separate acceptance test")
    runner = tmp_path / "runner"
    runner.write_text("#!/bin/sh\nexit 97\n")
    runner.chmod(0o700)
    return {
        "manifest_path": manifest, "runner": [str(runner)],
        "installation_path": artifact, "installation_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
    }


def test_external_registration_is_explicit_static_and_disabled(registration_files, monkeypatch):
    def prohibited(*args, **kwargs):
        pytest.fail("静态注册/发现不得 import、启动 worker 或联网")
    monkeypatch.setattr("asterun.plugins.registry.importlib.import_module", prohibited)
    monkeypatch.setattr(subprocess, "Popen", prohibited)
    monkeypatch.setattr("socket.create_connection", prohibited)
    registry = PluginRegistry()
    registration = registry.register_external(**registration_files)
    assert registration.plugin_id == "example.external-fake"
    described = registry.describe("example.external-fake")
    assert described["enabled"] is False
    assert described["registered"] is True
    assert described["callable"] == "not_tested"
    assert described["runtime_integrity_verified"] is False
    assert described["runtime_integrity_pinned"] is False
    assert registry.describe() == [described]
    with pytest.raises(AsterunError, match="停用"):
        registry.load_factory(registration.plugin_id)
    registry.set_enabled(registration.plugin_id, True)
    with pytest.raises(AsterunError, match="核心禁止导入"):
        registry.load_factory(registration.plugin_id)


def test_builtin_factory_loads_only_after_enable(manifest_raw, tmp_path, monkeypatch):
    manifest_raw["plugin_id"] = "example.lazy-builtin"
    module_name = "asterun_registry_test_lazy_factory"
    marker = tmp_path / "imported"
    (tmp_path / f"{module_name}.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('loaded')\ndef factory(config):\n    return config\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    registry = PluginRegistry()
    registry.register_builtin(manifest_raw, factory=f"{module_name}:factory", enabled=False)
    registry.describe()
    assert not marker.exists()
    with pytest.raises(AsterunError):
        registry.load_factory(manifest_raw["plugin_id"])
    assert not marker.exists()
    registry.set_enabled(manifest_raw["plugin_id"], True)
    assert not marker.exists()
    try:
        assert registry.load_factory(manifest_raw["plugin_id"])("same object") == "same object"
        assert marker.read_text() == "loaded"
    finally:
        sys.modules.pop(module_name, None)


@pytest.mark.parametrize("mutation", [
    lambda raw: raw.update(unknown=True),
    lambda raw: raw.pop("entry_point"),
    lambda raw: raw.update(schema_version="asterun-plugin/v2"),
    lambda raw: raw.update(plugin_api_major=2),
    lambda raw: raw.update(plugin_api_major=True),
    lambda raw: raw.update(plugin_id="bad id"),
    lambda raw: raw.update(entry_point="python -c code"),
    lambda raw: raw.update(roles=["agent", "agent"]),
    lambda raw: raw.update(roles=["root"]),
    lambda raw: raw.update(capabilities=[]),
    lambda raw: raw["capabilities"].append(copy.deepcopy(raw["capabilities"][0])),
    lambda raw: raw["capabilities"][0].update(unknown=True),
    lambda raw: raw["capabilities"][0].update(support=[]),
    lambda raw: raw["capabilities"][0].update(effect_class=[]),
    lambda raw: raw["capabilities"][0].update(input_schema={"type": "not-a-json-type"}),
    lambda raw: raw["capabilities"][0].update(output_schema={"$ref": "https://example.invalid/schema"}),
    lambda raw: raw["capabilities"][0].update(input_schema={"$defs": {"foo": {"$dynamicRef": "file:///secret"}}}),
    lambda raw: raw["capabilities"][0].update(input_schema={"$id": "https://example.invalid/base", "$ref": "#"}),
    lambda raw: raw["permissions"].update(unknown=[]),
    lambda raw: raw["platforms"][0].update(unknown=True),
    lambda raw: raw["platforms"][0].update(verification=[]),
    lambda raw: raw.update(extensions={"unscoped": {}}),
])
def test_manifest_rejects_invalid_contracts(manifest_raw, mutation, monkeypatch):
    monkeypatch.setattr("socket.create_connection", lambda *a, **k: pytest.fail("schema 不得联网"))
    mutation(manifest_raw)
    with pytest.raises(AsterunError):
        PluginManifest.from_dict(manifest_raw)


def test_capability_input_output_schemas_and_local_refs(manifest_raw):
    cap = manifest_raw["capabilities"][0]
    cap["input_schema"] = {"$defs": {"payload": {"type": "string", "minLength": 1}}, "$ref": "#/$defs/payload"}
    manifest = PluginManifest.from_dict(manifest_raw)
    assert manifest.validate_input("agent.execute", "text") == "text"
    with pytest.raises(AsterunError):
        manifest.validate_input("agent.execute", "")
    assert manifest.validate_output("agent.execute", {"text": "ok"}) == {"text": "ok"}
    with pytest.raises(AsterunError):
        manifest.validate_output("agent.execute", {"status": "succeeded"})
    with pytest.raises(AsterunError, match="未声明能力"):
        manifest.validate_input("unregistered.execute", {})


def test_manifest_and_registration_return_independent_values(manifest_raw, registration_files):
    manifest = PluginManifest.from_dict(manifest_raw)
    manifest_raw["plugin_id"] = "changed"
    before = manifest.sha256
    manifest.to_dict()["capabilities"].clear()
    assert manifest.sha256 == before
    registry = PluginRegistry()
    registration = registry.register_external(**registration_files)
    registry.describe(registration.plugin_id)["manifest"]["capabilities"].clear()
    assert registry.get(registration.plugin_id).manifest.to_dict()["capabilities"]


def test_manifest_file_rejects_duplicate_key_and_size(tmp_path, manifest_raw):
    path = tmp_path / "manifest.json"
    path.write_text('{"schema_version":"asterun-plugin/v1","schema_version":"other"}')
    with pytest.raises(AsterunError, match="重复"):
        PluginManifest.from_path(path)
    path.write_bytes(b" " * (256 * 1024 + 1))
    with pytest.raises(AsterunError, match="256 KiB"):
        PluginManifest.from_path(path)


def test_registry_conflicts_unknown_id_and_invalid_enable(registration_files):
    registry = PluginRegistry()
    registration = registry.register_external(**registration_files)
    with pytest.raises(AsterunError, match="冲突"):
        registry.register_external(**registration_files)
    with pytest.raises(AsterunError, match="尚未"):
        registry.get("not-registered")
    with pytest.raises(AsterunError):
        registry.set_enabled(registration.plugin_id, 1)


@pytest.mark.parametrize("field", ["manifest_path", "installation_path", "runner"])
def test_installation_pins_detect_modified_files(registration_files, field):
    registry = PluginRegistry()
    registration = registry.register_external(**registration_files)
    path = Path(registration_files[field][0]) if field == "runner" else registration_files[field]
    path.write_bytes(path.read_bytes() + b"\nchanged")
    # 静态 discover 不掩盖注册；执行入口必须显式重新核验。
    assert registry.describe(registration.plugin_id)["registered"]
    with pytest.raises(AsterunError, match="改变"):
        registry.verify_installation(registration.plugin_id)
    with pytest.raises(AsterunError):
        registry.set_enabled(registration.plugin_id, True)


def test_runtime_tree_pin_detects_installed_code_change(registration_files, tmp_path):
    runtime = tmp_path / "site-packages"
    runtime.mkdir()
    (runtime / "worker.py").write_text("VERSION = 1\n")
    registry = PluginRegistry()
    registration = registry.register_external(
        **registration_files, runtime_path=runtime, runtime_sha256=installation_digest(runtime),
    )
    assert registry.describe(registration.plugin_id)["runtime_integrity_pinned"]
    registry.verify_installation(registration.plugin_id)
    (runtime / "worker.py").write_text("VERSION = 2\n")
    with pytest.raises(AsterunError, match="运行内容已改变"):
        registry.verify_installation(registration.plugin_id)


def test_runtime_tree_pin_rejects_symlinks(tmp_path):
    target = tmp_path / "target"
    target.write_text("outside")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "escape").symlink_to(target)
    with pytest.raises(AsterunError, match="符号链接"):
        installation_digest(runtime)


@pytest.mark.parametrize("change", [
    {"runner": "python -m plugin"}, {"runner": ["python"]},
    {"installation_sha256": "0" * 64}, {"enabled": "yes"},
])
def test_registration_rejects_invalid_runner_or_digest(registration_files, change):
    with pytest.raises(AsterunError):
        PluginRegistry().register_external(**(registration_files | change))


def request():
    return {"jsonrpc": "2.0", "id": "req-1", "method": "execution.start", "params": {
        "protocol_version": WORKER_PROTOCOL_VERSION, "input": {"text": "hello"}, "context": {},
    }}


def test_worker_roundtrip_contract():
    wire = request()
    assert validate_worker_request(wire) == wire
    response = {"jsonrpc": "2.0", "id": "req-1", "result": {"protocol_version": WORKER_PROTOCOL_VERSION, "output": {"text": "ok"}}}
    assert validate_worker_response(response, request_id="req-1") == response
    error = {"jsonrpc": "2.0", "id": "req-1", "error": {"code": -32000, "message": "unsupported", "data": {"code": "CAPABILITY_UNSUPPORTED"}}}
    assert validate_worker_response(error, request_id="req-1") == error


@pytest.mark.parametrize("mutation", [
    lambda wire: wire.update(jsonrpc="1.0"),
    lambda wire: wire.update(id=True),
    lambda wire: wire.pop("id"),
    lambda wire: wire.update(method="shell.run"),
    lambda wire: wire.update(unknown=True),
    lambda wire: wire["params"].update(protocol_version="asterun-worker/v2"),
    lambda wire: wire["params"].update(input=[]),
    lambda wire: wire["params"].update(context=[]),
    lambda wire: wire["params"].update(unknown=True),
    lambda wire: wire["params"].update(input={"value": float("nan")}),
])
def test_worker_request_rejects_invalid_contract(mutation):
    wire = request()
    mutation(wire)
    with pytest.raises(AsterunError):
        validate_worker_request(wire)


@pytest.mark.parametrize("response", [
    {"jsonrpc": "2.0", "id": "other", "result": {"protocol_version": WORKER_PROTOCOL_VERSION}},
    {"jsonrpc": "2.0", "id": "req-1", "result": {}},
    {"jsonrpc": "2.0", "id": "req-1", "result": {"protocol_version": "other"}},
    {"jsonrpc": "2.0", "id": "req-1", "result": {"protocol_version": WORKER_PROTOCOL_VERSION}, "error": {}},
    {"jsonrpc": "2.0", "id": "req-1", "error": {"code": "oops", "message": "error"}},
    {"jsonrpc": "2.0", "id": "req-1", "error": {"code": -1, "message": "error", "extra": True}},
])
def test_worker_response_rejects_wrong_id_version_shape(response):
    with pytest.raises(AsterunError):
        validate_worker_response(response, request_id="req-1")
