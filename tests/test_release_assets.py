"""标签发行资产的形状、校验文件和工作流约束。"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def _loader():
    import importlib.util

    path = ROOT / "scripts" / "build-release-assets.py"
    spec = importlib.util.spec_from_file_location("build_release_assets", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_checksums_match_manual_release_format(tmp_path):
    module = _loader()
    (tmp_path / "b.txt").write_bytes(b"beta")
    (tmp_path / "a.txt").write_bytes(b"alpha")
    target = module.write_checksums(tmp_path)
    text = target.read_text(encoding="utf-8")
    assert text.endswith("\n") and "SHA256SUMS" not in text
    lines = text.splitlines()
    assert [line.split("  ", 1)[1] for line in lines] == ["a.txt", "b.txt"]
    for line in lines:
        digest, name = line.split("  ", 1)
        assert len(digest) == 64 and digest == digest.lower()
        assert hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() == digest


def test_expected_assets_cover_core_skill_and_every_external_plugin():
    module = _loader()
    names = module.expected_asset_names(ROOT)
    assert names == sorted(names)
    assert "asterun-0.1.0a17-py3-none-any.whl" in names
    assert "asterun-0.1.0a17.tar.gz" in names
    assert "asterun-skill-0.1.0a17.zip" in names
    for plugin in ("antigravity", "cursor", "jules"):
        prefix = f"asterun_plugin_{plugin}-"
        assert any(name.startswith(prefix) and name.endswith(".whl") for name in names)
        assert any(name.startswith(prefix) and name.endswith(".tar.gz") for name in names)
    assert all(not name.endswith("SHA256SUMS") for name in names)


def test_release_workflow_is_tag_triggered_and_uses_only_github_token():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert 'tags:' in text and '- "v*"' in text
    assert "pull_request" not in text
    assert "actions/attest-build-provenance@v4" in text
    assert "id-token: write" in text
    assert "attestations: write" in text
    assert "contents: write" in text
    assert "--prerelease" in text
    assert "github.token" in text
    assert "scripts/build-release-assets.py" in text
    assert "SHA256SUMS" in text
    assert "secrets." not in text
    lowered = text.lower()
    assert "pypi" not in lowered
    assert "sigstore" not in lowered


def test_built_release_assets_pass_packaging_checks(tmp_path):
    module = _loader()
    output = tmp_path / "dist"
    report = module.build_release_assets(ROOT, output, smoke=False)
    assert report["ok"], report["problems"]
    names = sorted(path.name for path in output.iterdir() if path.name != "SHA256SUMS")
    assert names == module.expected_asset_names(ROOT)
    checksums = (output / "SHA256SUMS").read_text(encoding="utf-8")
    assert checksums.endswith("\n")
    listed = []
    for line in checksums.splitlines():
        digest, name = line.split("  ", 1)
        listed.append(name)
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == digest
    assert listed == names
    import json
    import zipfile
    skill = next(output.glob("asterun-skill-*.zip"))
    with zipfile.ZipFile(skill) as archive:
        manifest = json.loads(archive.read("asterun/manifest.json"))
    assert len(manifest["source_commit"]) == 40
    assert "asterun/SKILL.md" in zipfile.ZipFile(skill).namelist()
