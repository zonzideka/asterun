"""外部插件默认必须钉定实际运行代码，开发模式开关默认关闭。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from asterun.config import load_config
from asterun.config_migration import migrate_v1_to_v2
from asterun.errors import AsterunError
from asterun.plugins.registry import PluginRegistry, installation_digest
from asterun.plugins.worker import WorkerHost


def _files(tmp_path):
    manifest_raw = json.loads((Path(__file__).parent / "fixtures/plugins/external_fake/src/asterun_external_fake/manifest.json").read_text())
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(manifest_raw))
    artifact = tmp_path / "plugin.whl"
    artifact.write_bytes(b"registration artifact")
    runner = tmp_path / "runner"
    runner.write_text("#!/bin/sh\nexit 97\n")
    runner.chmod(0o700)
    return {
        "manifest_path": manifest, "runner": [str(runner)],
        "installation_path": artifact, "installation_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
    }


def test_registration_rejects_unpinned_runtime_by_default(tmp_path):
    with pytest.raises(AsterunError, match="钉定"):
        PluginRegistry().register_external(**_files(tmp_path))


def test_dev_mode_allows_registration_but_runtime_still_refuses_execution(tmp_path):
    registry = PluginRegistry()
    registration = registry.register_external(**_files(tmp_path), allow_unpinned_runtime=True, enabled=True)
    described = registry.describe(registration.plugin_id)
    assert described["runtime_integrity_pinned"] is False
    assert described["allow_unpinned_runtime"] is True
    with pytest.raises(AsterunError, match="runtime"):
        WorkerHost(registry, registration).call("plugin.describe", {}, {}, cwd=tmp_path)


def test_pinned_runtime_is_rechecked_before_use(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "worker.py").write_text("print('ok')\n")
    registry = PluginRegistry()
    registration = registry.register_external(
        **_files(tmp_path), runtime_path=runtime, runtime_sha256=installation_digest(runtime), enabled=True,
    )
    assert registry.describe(registration.plugin_id)["runtime_integrity_pinned"] is True
    registry.verify_installation(registration.plugin_id)
    (runtime / "worker.py").write_text("print('changed')\n")
    with pytest.raises(AsterunError, match="运行内容已改变"):
        registry.verify_installation(registration.plugin_id)


def test_v2_config_requires_runtime_pin_unless_dev_mode(config_path, external_fake_installation, tmp_path):
    plugin = external_fake_installation
    v2_raw = migrate_v1_to_v2(config_path)["config"]
    plugin_body = {
        "enabled": False, "manifest_path": str(plugin["manifest"]), "runner": plugin["runner"],
        "installation_path": str(plugin["wheel"]), "installation_sha256": plugin["sha256"],
    }
    v2_raw["plugins"] = {"example.external-fake": dict(plugin_body)}
    next(iter(v2_raw["connections"].values())).update(plugin_id="example.external-fake", options={})
    path = tmp_path / "unpinned.json"
    path.write_text(json.dumps(v2_raw))
    with pytest.raises(AsterunError, match="钉定"):
        load_config(path)
    plugin_body["allow_unpinned_runtime"] = True
    v2_raw["plugins"] = {"example.external-fake": plugin_body}
    path.write_text(json.dumps(v2_raw))
    loaded = load_config(path)
    assert loaded.plugins["example.external-fake"]["allow_unpinned_runtime"] is True
