from __future__ import annotations

import json
from pathlib import Path

import pytest

from asterun.application import Application
from asterun.config import load_config
from asterun.errors import CAPABILITY_UNSUPPORTED
from asterun.policy import AllowConfiguredWorkspaces, Principal
from asterun.redact import redact_ref, redact_text
from asterun.store import MemoryStore
from tests.conftest import write_config


def test_diagnose_redacts_account_and_home(app: Application, isolated_env: Path) -> None:
    result = app.handle("diagnose", {})
    assert result.ok
    dumped = json.dumps(result.data, ensure_ascii=False)
    assert "account:[redacted]" in dumped
    assert "host:[redacted]" in dumped
    assert "account:test" not in dumped
    assert str(isolated_env) not in dumped or dumped.count("[redacted]") >= 1
    assert result.data["notes"]["does_not_include_credentials"] is True
    assert result.data["notes"]["clean_install_is_not_two_real_user_tasks"] is True
    assert "ANTHROPIC_API_KEY" in result.data["secret_env"]


def test_redact_helpers() -> None:
    assert redact_ref("account:secret-user") == "account:[redacted]"
    assert "secret-user" not in redact_text("path=/home/secret-user/token", extras=["/home/secret-user"])


def test_cli_entry_disable_stops_new_tasks(isolated_env: Path, workspace_root: Path) -> None:
    path = write_config(
        isolated_env / "config.json",
        workspace_root,
        extra={"entries": {"cli": {"enabled": False}, "mcp": {"enabled": True}}},
    )
    config = load_config(path)
    cli = Application(
        config,
        store=MemoryStore(),
        policy=AllowConfiguredWorkspaces({"demo"}),
        principal=Principal("test:user", "test"),
        entry="cli",
    )
    blocked = cli.handle("task.submit", {"workspace": "demo", "script": "success"})
    assert blocked.ok is False
    assert blocked.error["code"] == CAPABILITY_UNSUPPORTED
    assert cli.backends["fake"].dispatch_count == 0
    inspect = cli.handle("backend.inspect", {})
    assert inspect.ok
    mcp = Application(
        config,
        store=MemoryStore(),
        policy=AllowConfiguredWorkspaces({"demo"}),
        principal=Principal("test:user", "test"),
        entry="mcp",
    )
    submitted = mcp.handle("task.submit", {"workspace": "demo", "script": "success"})
    assert submitted.ok


def test_mcp_entry_disable_leaves_cli(isolated_env: Path, workspace_root: Path) -> None:
    path = write_config(
        isolated_env / "config.json",
        workspace_root,
        extra={"entries": {"cli": True, "mcp": False}},
    )
    config = load_config(path)
    mcp = Application(
        config,
        store=MemoryStore(),
        policy=AllowConfiguredWorkspaces({"demo"}),
        principal=Principal("test:user", "test"),
        entry="mcp",
    )
    blocked = mcp.handle("task.submit", {"workspace": "demo", "script": "success"})
    assert blocked.error["code"] == CAPABILITY_UNSUPPORTED
    cli = Application(
        config,
        store=MemoryStore(),
        policy=AllowConfiguredWorkspaces({"demo"}),
        principal=Principal("test:user", "test"),
        entry="cli",
    )
    assert cli.handle("task.submit", {"workspace": "demo", "script": "success"}).ok


def test_non_code_example_workspace(isolated_env: Path) -> None:
    example = Path(__file__).resolve().parents[1] / "examples" / "non-code"
    inbox = example / "inbox"
    path = write_config(isolated_env / "config.json", inbox, extra={"default_backend": "fake"})
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["workspaces"] = {"inbox": {"root": str(inbox), "allow_non_git": True}}
    path.write_text(json.dumps(raw), encoding="utf-8")
    app = Application(
        load_config(path),
        store=MemoryStore(),
        policy=AllowConfiguredWorkspaces({"inbox"}),
        principal=Principal("test:user", "test"),
    )
    submitted = app.handle(
        "task.submit",
        {"workspace": "inbox", "path": "meeting-notes.txt", "script": "success"},
    )
    assert submitted.ok
    assert submitted.data["task"]["acceptance"] == "pending"
    evaluated = app.handle(
        "workflow.evaluate",
        {
            "task_id": submitted.ids["task_id"],
            "checks": [{"kind": "contains", "text": "行动项"}],
        },
    )
    assert evaluated.data["acceptance"] == "passed"
    assert not (inbox / ".git").exists()


def test_backup_and_restore_roundtrip(config_path: Path, isolated_env: Path) -> None:
    from asterun.cli import main

    state = isolated_env / "state"
    archive = isolated_env / "backup.tar.gz"
    assert (
        main(
            [
                "--config",
                str(config_path),
                "--state-dir",
                str(state),
                "task-submit",
                "--workspace",
                "demo",
                "--script",
                "success",
                "--text",
                "备份样例",
            ]
        )
        == 0
    )
    assert main(["--config", str(config_path), "--state-dir", str(state), "state-backup", "--output", str(archive)]) == 0
    restored_dir = isolated_env / "restored"
    assert (
        main(
            [
                "--config",
                str(config_path),
                "--state-dir",
                str(restored_dir),
                "state-restore",
                "--input",
                str(archive),
            ]
        )
        == 0
    )
    from asterun.sqlite_store import SqliteStore
    from asterun.ids import TaskId

    store = SqliteStore(restored_dir / "asterun.sqlite")
    ids = store.list_task_ids()
    assert ids
    task = store.get_task(ids[0])
    assert "备份样例" in task.text
    store.close()


def test_release_artifacts_exclude_references() -> None:
    root = Path(__file__).resolve().parents[1]
    report = _build_release(root)
    assert report["ok"], report["problems"]


def _release_checker():
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    path = root / "scripts" / "check-release-package.py"
    spec = importlib.util.spec_from_file_location("check_release_package", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build_release(root: Path) -> dict:
    return _release_checker().build_and_check(root)


def _archive(tmp_path: Path, kind: str, files: dict[str, bytes], *, licensed: bool = True) -> Path:
    import io
    import tarfile
    import zipfile

    files = dict(files)
    if licensed:
        prefix = "asterun_test-1.dist-info" if kind == "wheel" else "asterun-test"
        metadata_name = "METADATA" if kind == "wheel" else "PKG-INFO"
        files[f"{prefix}/{metadata_name}"] = b"Metadata-Version: 2.1\nName: asterun-test\nLicense: Apache-2.0\n"
        files[f"{prefix}/LICENSE"] = (Path(__file__).resolve().parents[1] / "LICENSE").read_bytes()
    path = tmp_path / ("asterun-test.whl" if kind == "wheel" else "asterun-test.tar.gz")
    if kind == "wheel":
        with zipfile.ZipFile(path, "w") as archive:
            for name, content in files.items():
                archive.writestr(name, content)
    else:
        with tarfile.open(path, "w:gz") as archive:
            for name, content in files.items():
                member = tarfile.TarInfo(name)
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
    return path


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
def test_release_rejects_private_content_in_normal_filename(tmp_path: Path, kind: str) -> None:
    path = _archive(tmp_path, kind, {
        "asterun/__init__.py": b"",
        "public-guide.md": b"A local result: /Users/example/private/account-data\n",
    })
    problems = _release_checker().inspect_archive(path)
    assert any("public-guide.md" in value and "作者家目录" in value for value in problems)
    assert "account-data" not in "\n".join(problems)


@pytest.mark.parametrize("local_path", [b"/home/example/private", b"C:\\Users\\example\\private", b"/Volumes/example/private", "/Users/示例/private".encode()])
def test_release_rejects_other_local_environment_paths(tmp_path: Path, local_path: bytes) -> None:
    path = _archive(tmp_path, "wheel", {
        "asterun/__init__.py": b"",
        "public-guide.md": local_path,
    })
    problems = _release_checker().inspect_archive(path)
    assert any("public-guide.md" in value for value in problems)
    assert "example" not in "\n".join(problems)


def test_release_accepts_generic_path_example(tmp_path: Path) -> None:
    path = _archive(tmp_path, "wheel", {
        "asterun/__init__.py": b"",
        "public-guide.md": b"Use /path/to/workspace and /path/to/state.\n",
    })
    assert _release_checker().inspect_archive(path) == []


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
def test_release_rejects_historical_receipts_without_author_path(tmp_path: Path, kind: str) -> None:
    path = _archive(tmp_path, kind, {
        "asterun/__init__.py": b"",
        "docs/reviews/local-run.json": b'{"session_id":"private-session"}',
    })
    assert any("本机历史" in value for value in _release_checker().inspect_archive(path))


def test_release_check_script_is_safe_to_bundle(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts/check-release-package.py"
    path = _archive(tmp_path, "sdist", {
        "asterun/__init__.py": b"",
        "scripts/check-release-package.py": script.read_bytes(),
    })
    assert _release_checker().inspect_archive(path) == []


def test_release_manual_relative_links_resolve_inside_sdist(tmp_path: Path) -> None:
    path = _archive(tmp_path, "sdist", {
        "asterun/src/asterun/__init__.py": b"",
        "asterun/docs/install.md": b"[Source](../src/asterun/__init__.py#L1)\n"
        b"[Web](https://example.invalid/guide) [Anchor](#install) [Missing](absent.md)\n",
    })
    problems = _release_checker().inspect_archive(path)
    assert len(problems) == 1
    assert "absent.md" in problems[0]


@pytest.mark.parametrize("package", ["asterun", "asterun-plugin-antigravity", "asterun-plugin-cursor", "asterun-plugin-jules"])
def test_release_requires_english_documents_even_without_navigation_link(package: str) -> None:
    files = {f"{package}/PKG-INFO": f"Name: {package}\n".encode(), f"{package}/README.md": b"# Asterun\n"}
    problems = _release_checker().inspect_bilingual_documents(files)
    assert any("README.en.md" in problem for problem in problems)
    if package == "asterun":
        assert any("docs/en/user-manual.md" in problem for problem in problems)
        assert any("docs/en/protocol/IMPLEMENTATION.md" in problem for problem in problems)
    else:
        files[f"{package}/README.en.md"] = b"# Asterun\n"
        assert _release_checker().inspect_bilingual_documents(files) == []


def test_release_rejects_english_status_record(tmp_path: Path) -> None:
    path = _archive(tmp_path, "sdist", {"asterun/__init__.py": b"", "STATUS.en.md": b"# Status\n"})
    assert any("仓库状态" in problem for problem in _release_checker().inspect_archive(path))


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
@pytest.mark.parametrize("defect", ["missing_metadata", "wrong_license", "missing_text", "truncated_text"])
def test_release_rejects_incomplete_license(tmp_path: Path, kind: str, defect: str) -> None:
    prefix = "asterun_test-1.dist-info" if kind == "wheel" else "asterun-test"
    metadata_name = "METADATA" if kind == "wheel" else "PKG-INFO"
    files = {"asterun/__init__.py": b""}
    if defect != "missing_metadata":
        license_name = "LicenseRef-Proprietary" if defect == "wrong_license" else "Apache-2.0"
        files[f"{prefix}/{metadata_name}"] = f"License: {license_name}\n".encode()
    if defect != "missing_text":
        text = (Path(__file__).resolve().parents[1] / "LICENSE").read_bytes()
        files[f"{prefix}/LICENSE"] = text[:100] if defect == "truncated_text" else text
    problems = _release_checker().inspect_archive(_archive(tmp_path, kind, files, licensed=False))
    assert problems
    assert all("许可" in problem or "LICENSE" in problem for problem in problems)


@pytest.mark.parametrize("plugin", ["antigravity", "cursor", "jules"])
def test_plugin_distributions_include_license(tmp_path: Path, plugin: str) -> None:
    import os
    import shutil
    import warnings

    from setuptools.build_meta import build_sdist, build_wheel

    root = Path(__file__).resolve().parents[1]
    source = root / "packages" / f"asterun-plugin-{plugin}"
    copied = tmp_path / source.name
    shutil.copytree(source, copied, ignore=shutil.ignore_patterns("build", "dist", "*.egg-info", "__pycache__"))
    previous = Path.cwd()
    try:
        os.chdir(copied)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            archives = [tmp_path / build_sdist(str(tmp_path)), tmp_path / build_wheel(str(tmp_path))]
    finally:
        os.chdir(previous)
    for archive in archives:
        problems = _release_checker().inspect_archive(archive)
        assert problems == [], problems
