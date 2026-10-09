"""标签发行资产的形状、校验文件和工作流约束。"""
from __future__ import annotations

import hashlib
import json
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


def test_source_date_epoch_comes_from_the_tag_commit(monkeypatch):
    module = _loader()
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1")
    stamp = module._source_date(ROOT)
    expected = subprocess.run(
        ["git", "log", "-1", "--format=%ct"], cwd=ROOT, check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert stamp == expected and stamp != "1"


def test_archive_metadata_normalizes_to_identical_bytes(tmp_path):
    import gzip
    import io
    import tarfile
    import zipfile

    module = _loader()
    epoch = 1700000000

    def write_tar(path: Path, mtime: int, uid: int) -> None:
        buffer = io.BytesIO()
        with gzip.GzipFile(filename=f"build-{uid}", mode="wb", fileobj=buffer, mtime=mtime) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                payload = b"same-bytes"
                info = tarfile.TarInfo("pkg/a.txt")
                info.mtime = mtime
                info.uid = uid
                info.gid = uid
                info.uname = f"builder-{uid}"
                info.gname = "group"
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
        path.write_bytes(buffer.getvalue())

    def write_zip(path: Path, date_time: tuple[int, int, int, int, int, int]) -> None:
        with zipfile.ZipFile(path, "w") as archive:
            info = zipfile.ZipInfo("pkg/a.txt", date_time=date_time)
            info.external_attr = (0o100755 if date_time[0] == 2020 else 0o100644) << 16
            archive.writestr(info, b"same-bytes")

    first_tar = tmp_path / "first.tar.gz"
    second_tar = tmp_path / "second.tar.gz"
    write_tar(first_tar, 10, 1000)
    write_tar(second_tar, 99, 0)
    module.normalize_archive(first_tar, epoch)
    module.normalize_archive(second_tar, epoch)
    assert first_tar.read_bytes() == second_tar.read_bytes()

    first_zip = tmp_path / "first.whl"
    second_zip = tmp_path / "second.whl"
    write_zip(first_zip, (2020, 1, 2, 3, 4, 5))
    write_zip(second_zip, (2024, 6, 7, 8, 9, 10))
    module.normalize_archive(first_zip, epoch)
    module.normalize_archive(second_zip, epoch)
    assert first_zip.read_bytes() == second_zip.read_bytes()


def test_two_release_builds_are_byte_identical(tmp_path, monkeypatch):
    module = _loader()
    first = tmp_path / "first"
    second = tmp_path / "second"
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1")
    first_report = module.build_release_assets(ROOT, first, smoke=False)
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "4102444800")
    second_report = module.build_release_assets(ROOT, second, smoke=False)
    assert first_report["ok"], first_report["problems"]
    assert second_report["ok"], second_report["problems"]
    first_names = sorted(path.name for path in first.iterdir())
    second_names = sorted(path.name for path in second.iterdir())
    assert first_names == second_names
    assert first_names
    for name in first_names:
        assert (first / name).read_bytes() == (second / name).read_bytes()


def test_build_dependencies_are_pinned():
    module = _loader()
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert f"setuptools=={module.SETUPTOOLS_VERSION}" in workflow
    assert f"wheel=={module.WHEEL_VERSION}" in workflow
    assert "setuptools>=" not in workflow
    pins = (f"setuptools=={module.SETUPTOOLS_VERSION}", f"wheel=={module.WHEEL_VERSION}")
    files = [
        ROOT / "pyproject.toml",
        ROOT / "requirements-lock.txt",
        ROOT / "packages" / "asterun-plugin-antigravity" / "pyproject.toml",
        ROOT / "packages" / "asterun-plugin-cursor" / "pyproject.toml",
        ROOT / "packages" / "asterun-plugin-jules" / "pyproject.toml",
    ]
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert pins[0] in text and pins[1] in text, path


def test_resume_fills_missing_files_from_the_original_artifact_only():
    module = _loader_publish()
    artifact = {"a.whl": "aa", "SHA256SUMS": "bb"}
    remote = {"a.whl": "aa"}
    rebuilt = {"a.whl": "zz", "SHA256SUMS": "yy", "other.txt": "cc"}
    assert module.choose_resume_files(
        exists=True, draft=True, remote=remote, artifact=artifact, rebuilt=rebuilt,
    ) == {"action": "resume", "upload": ["SHA256SUMS"]}
    assert module.choose_resume_files(
        exists=True, draft=True, remote=dict(artifact), artifact=artifact, rebuilt=rebuilt,
    ) == {"action": "publish", "upload": []}
    assert module.choose_resume_files(
        exists=True, draft=False, remote=dict(artifact), artifact=artifact, rebuilt=rebuilt,
    ) == {"action": "noop", "upload": []}
    with pytest.raises(module.ReleaseRejected):
        module.choose_resume_files(
            exists=True, draft=True,
            remote={"a.whl": "aa"},
            artifact={"a.whl": "ff", "SHA256SUMS": "bb"},
            rebuilt={"a.whl": "aa", "SHA256SUMS": "bb"},
        )
    with pytest.raises(module.ReleaseRejected):
        module.choose_resume_files(
            exists=True, draft=False, remote=remote, artifact=artifact, rebuilt=artifact,
        )
    with pytest.raises(module.ReleaseRejected):
        module.choose_resume_files(
            exists=False, draft=False, remote={}, artifact=artifact, rebuilt=rebuilt,
        )


def test_publish_source_ignores_a_rebuild_when_the_release_exists(tmp_path, monkeypatch):
    module = _loader_publish()
    origin = tmp_path / "origin"
    origin.mkdir()
    (origin / "a.whl").write_bytes(b"original")
    (origin / "SHA256SUMS").write_text("checksum\n", encoding="utf-8")
    rebuilt = tmp_path / "rebuilt"
    rebuilt.mkdir()
    (rebuilt / "a.whl").write_bytes(b"rebuilt-bytes")
    (rebuilt / "extra.txt").write_bytes(b"do-not-upload")
    state = {
        "draft": True,
        "assets": {"a.whl": hashlib.sha256(b"original").hexdigest()},
    }

    def fetch(tag, release_state, repo):
        assert tag == "v0.1.0a17"
        assert release_state is state
        assert repo == "zonzideka/asterun"
        return origin

    monkeypatch.setattr(module, "fetch_original_artifact", fetch)
    source = module.publish_source(state=state, current=rebuilt, tag="v0.1.0a17", repo="zonzideka/asterun")
    assert source == origin
    plan = module.choose_resume_files(
        exists=True, draft=True, remote=state["assets"],
        artifact=module.local_digests(source), rebuilt=module.local_digests(rebuilt),
    )
    assert plan == {"action": "resume", "upload": ["SHA256SUMS"]}
    fresh = module.publish_source(state=None, current=rebuilt, tag="v0.1.0a17", repo="zonzideka/asterun")
    assert fresh == rebuilt
    empty = {"draft": True, "assets": {}}
    assert module.publish_source(state=empty, current=rebuilt, tag="v0.1.0a17", repo="zonzideka/asterun") == rebuilt


def test_partial_upload_then_publish_failure_resumes_without_rebuilding(tmp_path, monkeypatch):
    module = _loader_publish()
    original = tmp_path / "original"
    original.mkdir()
    payload = {"a.whl": b"original-wheel", "SHA256SUMS": b"checksum\n"}
    for name, data in payload.items():
        (original / name).write_bytes(data)
    rebuilt = tmp_path / "rebuilt"
    rebuilt.mkdir()
    (rebuilt / "a.whl").write_bytes(b"rebuilt-wheel")
    (rebuilt / "SHA256SUMS").write_bytes(b"rebuilt-checksum\n")
    (rebuilt / "extra.txt").write_bytes(b"do-not-upload")
    release: dict[str, object] | None = None
    uploads: list[list[str]] = []
    publish_attempts = {"count": 0}
    repo = "zonzideka/asterun"
    attestation = json.dumps([{
        "verificationResult": {
            "statement": {
                "predicate": {
                    "runDetails": {
                        "metadata": {
                            "invocationId": f"https://github.com/{repo}/actions/runs/77/attempts/1",
                        },
                    },
                },
            },
        },
    }])

    def finish(code: int = 0, stdout: str = "", stderr: str = ""):
        return subprocess.CompletedProcess([], code, stdout, stderr)

    def fake_gh(command: str, *args: str):
        nonlocal release
        if command == "gh release view":
            if release is None:
                return finish(1, stderr="release not found")
            assets = release["assets"]
            assert isinstance(assets, dict)
            listed = [
                {"name": name, "digest": "sha256:" + hashlib.sha256(data).hexdigest()}
                for name, data in sorted(assets.items())
            ]
            return finish(stdout=json.dumps({"isDraft": release["draft"], "assets": listed}))
        if command == "gh release create":
            release = {"draft": True, "assets": {}}
            return finish()
        if command == "gh release upload":
            names = [Path(arg).name for arg in args if Path(arg).is_file()]
            uploads.append(names)
            assert "extra.txt" not in names
            assets = release["assets"]
            assert isinstance(assets, dict)
            if len(uploads) == 1:
                first = Path(args[[Path(arg).is_file() for arg in args].index(True)])
                assets[first.name] = first.read_bytes()
                return finish(1, stderr="partial upload failed")
            for arg in args:
                path = Path(arg)
                if path.is_file():
                    assets[path.name] = path.read_bytes()
            return finish()
        if command == "gh release download":
            pattern = args[args.index("--pattern") + 1]
            dest = Path(args[args.index("--dir") + 1])
            assets = release["assets"]
            assert isinstance(assets, dict)
            (dest / pattern).write_bytes(assets[pattern])
            return finish()
        if command == "gh attestation verify":
            return finish(stdout=attestation)
        if command == "gh run download":
            dest = Path(args[args.index("--dir") + 1])
            for name, data in payload.items():
                (dest / name).write_bytes(data)
            return finish()
        if command == "gh release edit" and "--draft=false" in args:
            publish_attempts["count"] += 1
            if publish_attempts["count"] == 1:
                return finish(1, stderr="publish failed")
            release["draft"] = False
            return finish()
        if command == "gh release edit":
            return finish()
        raise AssertionError(command)

    monkeypatch.setenv("GITHUB_REPOSITORY", repo)
    monkeypatch.setattr(module, "_gh", fake_gh)
    with pytest.raises(module.ReleaseRejected, match="partial upload failed"):
        module.apply_release(original, "v0.1.0a17", ROOT)
    assert uploads == [["SHA256SUMS", "a.whl"]]
    assert set(release["assets"]) == {"SHA256SUMS"}
    with pytest.raises(module.ReleaseRejected, match="publish failed"):
        module.apply_release(rebuilt, "v0.1.0a17", ROOT)
    assert uploads[1] == ["a.whl"]
    assert release["assets"]["a.whl"] == payload["a.whl"]
    assert release["draft"] is True
    report = module.apply_release(rebuilt, "v0.1.0a17", ROOT)
    assert report == {"ok": True, "action": "published", "upload": []}
    assert len(uploads) == 2
    assert release["draft"] is False
    assert release["assets"] == payload


def test_workflow_run_id_comes_from_the_attestation():
    module = _loader_publish()
    older = {
        "verificationResult": {
            "statement": {
                "predicate": {
                    "runDetails": {
                        "metadata": {
                            "invocationId": "https://github.com/zonzideka/asterun/actions/runs/42/attempts/1",
                        },
                    },
                },
            },
        },
    }
    newer = {
        "predicate": {
            "runDetails": {
                "metadata": {
                    "invocationId": "https://github.com/zonzideka/asterun/actions/runs/9/attempts/2",
                },
            },
        },
    }
    assert module.workflow_run_id(older) == "42"
    assert module.workflow_run_id([older, newer]) == "9"
    with pytest.raises(module.ReleaseRejected):
        module.workflow_run_id({"predicate": {}})


def test_resume_skips_rebuild_and_reuses_the_attested_artifact():
    text = WORKFLOW.read_text(encoding="utf-8")
    publish = _publish_script()
    assert "needs.gate.outputs.mode == 'create'" in text
    assert "actions/upload-artifact@v4" in text
    assert "retention-days: 90" in text
    assert "asterun-dist" in text
    assert "gh run download" in publish
    assert "attestation verify" in publish
    assert "choose_resume_files" in publish
    assert "fetch_original_artifact" in publish
    build_job = text.split("needs.gate.outputs.mode == 'create'", 1)[0]
    assert "scripts/build-release-assets.py" not in build_job.split("jobs:", 1)[-1].split("build:", 1)[0]


def test_smoke_imports_dependencies_and_runs_entry_points(tmp_path):
    module = _loader()
    probe = module.smoke_probe_source()
    for needle in (
        "top_level.txt",
        "importlib.import_module",
        "plugin.describe",
        "asterun-worker/v1",
        "prepare",
        "--home",
        "subprocess",
        "register_external",
        "runtime_sha256",
        "isolated_worker_argv",
        '"-I"',
        '"-S"',
        '"-B"',
    ):
        assert needle in probe
    assert "metadata.version(" not in probe
    assert hasattr(module, "run_release_smoke")
    output = tmp_path / "dist"
    report = module.build_release_assets(ROOT, output, smoke=False)
    assert report["ok"], report["problems"]
    module.run_release_smoke(output, "0.1.0a17")
