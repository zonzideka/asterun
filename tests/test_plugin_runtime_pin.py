"""外部插件默认必须钉定实际运行代码，开发模式开关默认关闭。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

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


def test_runtime_pin_rejects_interpreter_and_unrelated_tree(tmp_path):
    package = tmp_path / "sample_plugin"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "worker.py").write_text("print('real worker')\n")
    files = _files(tmp_path)
    files["runner"] = [sys.executable, "-m", "sample_plugin.worker"]
    interpreter = Path(files["runner"][0])
    with pytest.raises(AsterunError, match="入口"):
        PluginRegistry().register_external(
            **files, runtime_path=interpreter, runtime_sha256=installation_digest(interpreter),
        )
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "worker.py").write_text("print('other copy')\n")
    with pytest.raises(AsterunError, match="入口"):
        PluginRegistry().register_external(
            **files, runtime_path=decoy, runtime_sha256=installation_digest(decoy),
        )


def test_module_entry_pin_rejects_unrelated_file_and_changed_package(tmp_path, monkeypatch):
    venv = tmp_path / "venv"
    subprocess.check_call([sys.executable, "-m", "venv", "--without-pip", str(venv)], timeout=60)
    python = venv / "bin" / "python"
    purelib = Path(subprocess.check_output(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        text=True, timeout=30,
    ).strip())
    package = purelib / "sample_plugin"
    package.mkdir()
    (package / "__init__.py").write_text("")
    worker = package / "worker.py"
    worker.write_text("print('worker')\n")
    files = _files(tmp_path)
    files["runner"] = [str(python), "-m", "sample_plugin.worker"]
    with pytest.raises(AsterunError, match="入口"):
        PluginRegistry().register_external(
            **files, runtime_path=python, runtime_sha256=installation_digest(python),
        )
    with pytest.raises(AsterunError, match="入口"):
        PluginRegistry().register_external(
            **files, runtime_path=worker, runtime_sha256=installation_digest(worker),
        )
    registry = PluginRegistry()
    registration = registry.register_external(
        **files, runtime_path=package, runtime_sha256=installation_digest(package), enabled=True,
    )
    worker.write_text("print('changed worker')\n")
    monkeypatch.setattr(
        "asterun.plugins.worker.subprocess.Popen",
        lambda *args, **kwargs: pytest.fail("改动后的插件包不应被启动"),
    )
    with pytest.raises(AsterunError, match="运行内容已改变"):
        WorkerHost(registry, registration).call("plugin.describe", {}, {}, cwd=tmp_path)


def test_execution_rejects_changed_worker_before_spawn(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    package = runtime / "sample_plugin"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    worker = package / "worker.py"
    worker.write_text("print('real worker')\n")
    files = _files(tmp_path)
    files["runner"] = [sys.executable, "-m", "sample_plugin.worker"]
    registry = PluginRegistry()
    registration = registry.register_external(
        **files, runtime_path=runtime, runtime_sha256=installation_digest(runtime), enabled=True,
    )
    worker.write_text("print('changed worker')\n")
    monkeypatch.setattr(
        "asterun.plugins.worker.subprocess.Popen",
        lambda *args, **kwargs: pytest.fail("改动后的 worker 不应被启动"),
    )
    with pytest.raises(AsterunError, match="运行内容已改变"):
        WorkerHost(registry, registration).call("plugin.describe", {}, {}, cwd=tmp_path)


def test_pinned_runtime_is_rechecked_before_use(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    runner = runtime / "runner"
    runner.write_text("#!/bin/sh\nexit 97\n")
    runner.chmod(0o700)
    (runtime / "worker.py").write_text("print('ok')\n")
    files = _files(tmp_path)
    files["runner"] = [str(runner)]
    registry = PluginRegistry()
    registration = registry.register_external(
        **files, runtime_path=runtime, runtime_sha256=installation_digest(runtime), enabled=True,
    )
    assert registry.describe(registration.plugin_id)["runtime_integrity_pinned"] is True
    registry.verify_installation(registration.plugin_id)
    (runtime / "worker.py").write_text("print('changed')\n")
    with pytest.raises(AsterunError, match="运行内容已改变"):
        registry.verify_installation(registration.plugin_id)


def test_dash_c_decoy_and_relative_script_are_rejected(tmp_path, monkeypatch):
    decoy = tmp_path / "decoy.py"
    decoy.write_text("print('decoy')\n")
    files = _files(tmp_path)
    files["runner"] = [sys.executable, "-c", str(decoy)]
    with pytest.raises(AsterunError, match="runner"):
        PluginRegistry().register_external(
            **files, runtime_path=decoy, runtime_sha256=installation_digest(decoy),
        )
    script = tmp_path / "real_worker.py"
    script.write_text("print('real')\n")
    files["runner"] = [sys.executable, str(script)]
    with pytest.raises(AsterunError, match="runner"):
        PluginRegistry().register_external(
            **files, runtime_path=script, runtime_sha256=installation_digest(script),
        )
    core = tmp_path / "core"
    task = tmp_path / "task"
    core.mkdir()
    task.mkdir()
    benign = core / "worker.py"
    benign.write_text("print('benign')\n")
    (task / "worker.py").write_text("print('unpinned')\n")
    monkeypatch.chdir(core)
    files["runner"] = [sys.executable, "worker.py"]
    with pytest.raises(AsterunError, match="runner"):
        PluginRegistry().register_external(
            **files, runtime_path=benign, runtime_sha256=installation_digest(benign),
        )


def test_registration_and_launch_do_not_execute_site_hooks(tmp_path, monkeypatch):
    marker = tmp_path / "site-hook"
    venv = tmp_path / "venv"
    subprocess.check_call([sys.executable, "-m", "venv", "--without-pip", str(venv)], timeout=60)
    python = venv / "bin" / "python"
    purelib = Path(subprocess.check_output(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        text=True, timeout=30,
    ).strip())
    (purelib / "sitecustomize.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('site')\n"
    )
    (purelib / "hook.pth").write_text("import sitecustomize\n")
    package = purelib / "sample_plugin"
    package.mkdir()
    (package / "__init__.py").write_text("")
    worker_marker = tmp_path / "worker-ran"
    (package / "worker.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(worker_marker)!r}).write_text('worker')\n"
    )
    files = _files(tmp_path)
    files["runner"] = [str(python), "-m", "sample_plugin.worker"]
    calls = []
    original = subprocess.Popen

    def spawn(args, **kwargs):
        command = list(args)
        calls.append(command)
        if "-I" not in command or "-S" not in command:
            pytest.fail("插件解释器启动未使用隔离模式")
        return original(args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spawn)
    registry = PluginRegistry()
    registration = registry.register_external(
        **files, runtime_path=package, runtime_sha256=installation_digest(package), enabled=True,
    )
    assert calls == []
    assert not marker.exists()
    with pytest.raises(Exception):
        WorkerHost(registry, registration).call("plugin.describe", {}, {}, cwd=tmp_path)
    assert any("-I" in command and "-S" in command for command in calls)
    assert not marker.exists()
    assert worker_marker.read_text() == "worker"


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
