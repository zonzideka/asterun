"""插件副本变化而版本未升高时，检查脚本必须失败。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check-plugin-copy-version.py"
PLUGIN = Path("packages/asterun-plugin-antigravity")


def git_env(repo: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": "Asterun Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Asterun Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    })
    return env


def git(repo: Path, *args: str) -> None:
    result = subprocess.run(["git", "-C", str(repo), *args], env=git_env(repo), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def write_plugin(repo: Path, version: str, marker: str) -> None:
    root = repo / PLUGIN / "src/asterun_plugin_antigravity"
    (root / "_vendor").mkdir(parents=True, exist_ok=True)
    (root / "_vendor/marker.py").write_text(marker)
    (root / "vendor-source.json").write_text(json.dumps({"marker": marker}))
    (repo / PLUGIN / "pyproject.toml").write_text(f'[project]\nversion = "{version}"\n')
    (root / "manifest.json").write_text(json.dumps({"plugin_version": version}))


def baseline(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    write_plugin(repo, "1.0.1", "original\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "baseline")
    return repo


def check(repo: Path) -> subprocess.CompletedProcess[str]:
    env = git_env(repo)
    env["ASTERUN_PLUGIN_BASE"] = "main"
    return subprocess.run([sys.executable, str(SCRIPT), str(repo)], env=env, capture_output=True, text=True)


def test_unchanged_copy_passes(tmp_path):
    result = check(baseline(tmp_path))
    assert result.returncode == 0, result.stderr


def test_copy_change_without_version_bump_fails(tmp_path):
    repo = baseline(tmp_path)
    write_plugin(repo, "1.0.1", "changed\n")
    result = check(repo)
    assert result.returncode != 0
    assert "插件副本内容已变化，但插件版本未升高" in result.stderr


def test_partial_version_bump_fails(tmp_path):
    repo = baseline(tmp_path)
    write_plugin(repo, "1.0.2", "changed\n")
    manifest = repo / PLUGIN / "src/asterun_plugin_antigravity/manifest.json"
    manifest.write_text(json.dumps({"plugin_version": "1.0.1"}))
    result = check(repo)
    assert result.returncode != 0
    assert "插件副本内容已变化，但插件版本未升高" in result.stderr


def test_matching_version_bump_passes(tmp_path):
    repo = baseline(tmp_path)
    write_plugin(repo, "1.0.2", "changed\n")
    result = check(repo)
    assert result.returncode == 0, result.stderr


def test_version_downgrade_fails(tmp_path):
    repo = baseline(tmp_path)
    write_plugin(repo, "1.0.2", "original\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "bump")
    write_plugin(repo, "1.0.1", "changed\n")
    result = check(repo)
    assert result.returncode != 0
    assert "插件副本内容已变化，但插件版本未升高" in result.stderr


def test_prerelease_is_not_newer_and_numeric_segments_compare(tmp_path):
    repo = baseline(tmp_path)
    write_plugin(repo, "1.0.1a2", "changed\n")
    prerelease = check(repo)
    assert prerelease.returncode != 0
    assert "插件副本内容已变化，但插件版本未升高" in prerelease.stderr
    numeric_root = tmp_path / "numeric"
    numeric_root.mkdir()
    released = baseline(numeric_root)
    write_plugin(released, "1.0.10", "changed\n")
    numeric = check(released)
    assert numeric.returncode == 0, numeric.stderr
