from pathlib import Path
from types import SimpleNamespace

from asterun.backends.codex import discover_codex_bin
from asterun.codex_discovery import discover_codex_candidates


def binary(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o700)
    return path


def test_invalid_explicit_and_environment_selection_never_fall_back(isolated_env, monkeypatch):
    good = binary(isolated_env / "bin/codex")
    monkeypatch.setenv("PATH", str(good.parent))
    monkeypatch.setenv("CODEX_BIN", str(good))
    assert discover_codex_bin(str(isolated_env / "missing")) is None
    assert discover_codex_bin("") is None
    monkeypatch.setenv("CODEX_BIN", str(isolated_env / "missing"))
    assert discover_codex_bin() is None
    monkeypatch.setenv("CODEX_BIN", "")
    assert discover_codex_bin() is None


def test_discovery_lists_application_candidates_without_spawning(isolated_env, monkeypatch):
    apps = isolated_env / "Applications"
    expected = [binary(apps / name / "Contents/Resources/codex") for name in ("ChatGPT.app", "Codex.app", "Other.app")]
    monkeypatch.setattr("asterun.codex_discovery.subprocess.run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不得 spawn")))
    result = discover_codex_candidates(application_dirs=[apps])
    assert {row["path"] for row in result["candidates"]} == {str(path) for path in expected}
    assert all(row["version_probe"] == "not_requested" for row in result["candidates"])
    assert result["model_called"] is False
    blocked = discover_codex_candidates(explicit=str(isolated_env / "missing"), application_dirs=[apps])
    assert blocked["selected"] is None
    assert len(blocked["candidates"]) == 4


def test_explicit_version_probe_parses_only_version_and_has_clean_environment(isolated_env, monkeypatch):
    selected = binary(isolated_env / "codex")
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"codex-cli 0.128.0\n", stderr=b"SECRET")
    monkeypatch.setattr("asterun.codex_discovery.subprocess.run", run)
    result = discover_codex_candidates(explicit=str(selected), application_dirs=[], probe_version=True)
    assert result["candidates"][0]["version"] == "0.128.0"
    assert calls[0][0] == [str(selected), "--version"]
    assert calls[0][1]["env"] == {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    monkeypatch.setattr("asterun.codex_discovery.subprocess.run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"TOKEN=SECRET\n", stderr=b""))
    result = discover_codex_candidates(explicit=str(selected), application_dirs=[], probe_version=True)
    assert result["candidates"][0]["version"] is None
    assert "SECRET" not in str(result)
