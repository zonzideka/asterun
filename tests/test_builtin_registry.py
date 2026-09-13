from asterun.application import build_backends
from asterun.config import load_config
from asterun.plugins.builtins import BUILTINS, builtin_registry
from tests.conftest import write_config


def test_builtin_registry_discovery_is_lazy(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("发现不能导入供应商或启动进程")
    monkeypatch.setattr("asterun.plugins.registry.importlib.import_module", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    rows = builtin_registry().describe()
    assert {row["plugin_id"] for row in rows} == {entry["plugin_id"] for entry in BUILTINS.values()}


def test_original_adapter_identity_is_preserved(isolated_env, workspace_root):
    from asterun.backends.antigravity import AntigravityBackend
    from asterun.backends.claude import ClaudeBackend
    from asterun.backends.codex import CodexBackend
    from asterun.backends.fake import FakeBackend
    from asterun.backends.grok import GrokBackend
    expected = {"antigravity": AntigravityBackend, "claude": ClaudeBackend,
                "codex": CodexBackend, "fake": FakeBackend, "grok": GrokBackend}
    path = write_config(isolated_env / "config.json", workspace_root,
                        extra_backends={kind: {"kind": kind} for kind in expected})
    backends = build_backends(load_config(path))
    assert {kind: type(value) for kind, value in backends.items()} == expected


def test_core_import_does_not_import_disabled_provider_modules(tmp_path):
    import os
    from pathlib import Path
    import subprocess
    import sys
    code = '''
import sys
from asterun.application import Application
for name in ('codex', 'codex_runtime', 'grok', 'claude', 'antigravity', 'antigravity_runtime'):
    assert 'asterun.backends.' + name not in sys.modules, name
'''
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
           "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    result = subprocess.run([sys.executable, "-c", code], env=env, cwd=tmp_path,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
