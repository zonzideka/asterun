"""标签发行资产的形状、校验文件和工作流约束。"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def _load(path: Path, name: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _loader():
    return _load(ROOT / "scripts" / "build-release-assets.py", "build_release_assets")


def _loader_publish():
    return _load(ROOT / "scripts" / "publish-release-assets.py", "publish_release_assets")


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


def _publish_script() -> str:
    return (ROOT / "scripts" / "publish-release-assets.py").read_text(encoding="utf-8")


def test_release_workflow_is_tag_triggered_and_uses_only_github_token():
    text = WORKFLOW.read_text(encoding="utf-8")
    publish = _publish_script()
    assert 'tags:' in text and '- "v*"' in text
    assert "pull_request" not in text
    assert "actions/attest-build-provenance@v4" in text
    assert "id-token: write" in text
    assert "attestations: write" in text
    assert "contents: write" in text
    assert "--prerelease" in publish
    assert "github.token" in text
    assert "scripts/build-release-assets.py" in text
    assert "SHA256SUMS" in text
    assert "secrets." not in text and "secrets." not in publish
    lowered = (text + publish).lower()
    assert "pypi" not in lowered
    assert "sigstore" not in lowered
    assert "--clobber" not in lowered


def test_release_verifies_offline_matrix_before_publish_and_requires_main():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "needs: verify" in text
    assert "scripts/verify-offline.sh" in text
    assert text.count("scripts/check-release-tag.py") >= 2
    assert '"3.11"' in text and '"3.12"' in text
    assert "fail-fast: false" in text
    assert "--phase gate" in text and "--phase apply" in text
    verify = text.split("release:", 1)[0]
    assert "scripts/verify-offline.sh" in verify
    publish = text.split("needs: verify", 1)[1]
    assert "actions/attest-build-provenance@v4" in publish
    assert "scripts/build-release-assets.py" in publish


def test_release_plan_resumes_drafts_and_refuses_published_mismatch():
    module = _loader_publish()
    local = {"a.whl": "aa", "SHA256SUMS": "bb"}
    assert module.plan_release(exists=False, draft=False, remote={}, local=local) == {
        "action": "create", "upload": ["SHA256SUMS", "a.whl"],
    }
    assert module.plan_release(exists=True, draft=True, remote={"a.whl": "aa"}, local=local) == {
        "action": "resume", "upload": ["SHA256SUMS"],
    }
    assert module.plan_release(exists=True, draft=True, remote=dict(local), local=local) == {
        "action": "publish", "upload": [],
    }
    assert module.plan_release(exists=True, draft=False, remote=dict(local), local=local) == {
        "action": "noop", "upload": [],
    }
    for remote, draft in (
        ({"a.whl": "ff", "SHA256SUMS": "bb"}, True),
        ({"a.whl": "aa", "SHA256SUMS": "bb", "extra.txt": "cc"}, True),
        ({"a.whl": "aa"}, False),
        ({"a.whl": "ff", "SHA256SUMS": "bb"}, False),
    ):
        with pytest.raises(module.ReleaseRejected):
            module.plan_release(exists=True, draft=draft, remote=remote, local=local)


def test_smoke_install_includes_plugin_dependencies():
    module = _loader()
    args = module.smoke_install_arguments(Path("/venv/bin/pip"), [Path("core.whl"), Path("plug.whl")])
    assert "--no-deps" not in args
    assert args[0].endswith("/pip") and "install" in args
    assert "core.whl" in args and "plug.whl" in args
    assert module.requirement_project("jsonschema[format-nongpl]==4.26.0") == "jsonschema"
    assert module.requirement_project('Foo_Bar>=1,<2; extra == "test"') == "Foo_Bar"
    assert module.is_extra_requirement('pytest==8.3.5; extra == "test"')
    assert not module.is_extra_requirement("jsonschema[format-nongpl]==4.26.0")
    probe = module.smoke_probe_source()
    assert "dist.requires" in probe and "requirement_project" in probe and "is_extra_requirement" in probe
    assert "--no-deps" not in (ROOT / "scripts" / "build-release-assets.py").read_text(encoding="utf-8")


def _git(repo: Path, *args: str) -> str:
    env = os.environ.copy()
    env.update(GIT_AUTHOR_NAME="release-test", GIT_AUTHOR_EMAIL="release-test@example.com",
               GIT_COMMITTER_NAME="release-test", GIT_COMMITTER_EMAIL="release-test@example.com")
    result = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=env)
    return result.stdout.strip()


def test_tag_commit_must_match_version_and_sit_on_main(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "pyproject.toml").write_text('version = "0.1.0a17"\n', encoding="utf-8")
    _git(repo, "add", "pyproject.toml")
    _git(repo, "commit", "-m", "init")
    module = _load(ROOT / "scripts" / "check-release-tag.py", "check_release_tag")
    assert module.ensure_tag_commit(repo, "v0.1.0a17")
    with pytest.raises(SystemExit):
        module.ensure_tag_commit(repo, "v9.9.9")
    _git(repo, "checkout", "-b", "side")
    (repo / "side.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "side.txt")
    _git(repo, "commit", "-m", "side")
    with pytest.raises(SystemExit):
        module.ensure_tag_commit(repo, "v0.1.0a17")


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
