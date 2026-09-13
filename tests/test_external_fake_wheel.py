import json
import subprocess


def test_external_wheel_executes_without_core(external_fake_installation, tmp_path):
    plugin = external_fake_installation
    request = {"jsonrpc": "2.0", "id": "independent", "method": "execution.start",
               "params": {"protocol_version": "asterun-worker/v1",
                          "input": {"text": "独立 wheel 实际执行"}, "context": {}}}
    result = subprocess.run(plugin["runner"], input=json.dumps(request) + "\n",
                            env=plugin["env"], cwd=tmp_path, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0
    response = json.loads(result.stdout)
    assert response["result"]["output"]["text"] == "独立 wheel 实际执行"
    check = subprocess.run([plugin["runner"][0], "-c", "import importlib.util; assert importlib.util.find_spec('asterun') is None"],
                           env=plugin["env"], cwd=tmp_path, capture_output=True, timeout=10)
    assert check.returncode == 0


def test_static_external_registration_never_imports(external_fake_installation, tmp_path, monkeypatch):
    from asterun.plugins.registry import PluginRegistry

    plugin = external_fake_installation
    trap = tmp_path / "imported"
    monkeypatch.setenv("EXTERNAL_FAKE_IMPORT_TRAP", str(trap))
    registry = PluginRegistry()
    registry.register_external(plugin["manifest"], runner=plugin["runner"],
                               installation_path=plugin["wheel"], installation_sha256=plugin["sha256"])
    assert registry.describe()
    assert not trap.exists()
    registry.verify_installation("example.external-fake")
    assert not trap.exists()
