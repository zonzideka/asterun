"""旧导入路径保持可用；只有明确读取导出类时才导入供应商实现。"""
from importlib import import_module

_EXPORTS = {
    "AntigravityBackend": "antigravity", "Backend": "base", "BackendCall": "base",
    "ClaudeBackend": "claude", "CodexBackend": "codex", "DisabledBackend": "base",
    "FAKE_SCRIPTS": "fake", "FakeBackend": "fake", "GrokBackend": "grok",
}
__all__ = list(_EXPORTS)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    value = getattr(import_module(f"asterun.backends.{_EXPORTS[name]}"), name)
    globals()[name] = value
    return value
