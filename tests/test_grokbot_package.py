import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "integrations/grokbot/scripts/package.py"
spec = importlib.util.spec_from_file_location("grokbot_package", SCRIPT)
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "template"
    root.mkdir()
    names = ["release-files.json", "release-lock.json", "README.md"]
    (root / names[0]).write_text(json.dumps(names))
    (root / names[1]).write_text(json.dumps({"template_version": "0.1.0-rc1", "core_version": "0.1.0a13"}))
    (root / names[2]).write_text("通用 Agent 任务。", encoding="utf-8")
    return root


def test_bundle_is_reproducible_and_uses_allowlist(source, tmp_path):
    (source / "private.json").write_text('{"author": "private"}')
    first, second = tmp_path / "one.zip", tmp_path / "two.zip"
    a = package.build(source, first, "a" * 40)
    b = package.build(source, second, "a" * 40)
    assert a["sha256"] == b["sha256"]
    with zipfile.ZipFile(first) as archive:
        prefix = "asterun-grokbot-0.1.0-rc1/"
        manifest = json.loads(archive.read(prefix + "manifest.json"))
        assert len(archive.namelist()) == 4
        assert all("private" not in name for name in archive.namelist())
        for name, metadata in manifest["files"].items():
            blob = archive.read(prefix + name)
            assert hashlib.sha256(blob).hexdigest() == metadata["sha256"]


@pytest.mark.parametrize("name", ["../outside", "/absolute", "x/../README.md", "x\\README.md", "./README.md"])
def test_member_traversal_is_rejected(source, tmp_path, name):
    (source / "release-files.json").write_text(json.dumps(["release-files.json", "release-lock.json", name]))
    with pytest.raises(package.PackageError):
        package.build(source, tmp_path / "bad.zip", "a" * 40)


def test_symlink_and_existing_output_are_rejected(source, tmp_path):
    (source / "README.md").unlink()
    (source / "README.md").symlink_to(source / "release-lock.json")
    with pytest.raises(package.PackageError, match="symbolic"):
        package.build(source, tmp_path / "bad.zip", "a" * 40)
    (source / "README.md").unlink()
    (source / "README.md").write_text("safe")
    output = tmp_path / "existing.zip"
    output.write_bytes(b"keep")
    with pytest.raises(package.PackageError, match="exists"):
        package.build(source, output, "a" * 40)
    assert output.read_bytes() == b"keep"


def test_author_paths_and_secret_patterns_are_rejected(source, tmp_path):
    (source / "README.md").write_text("/Users/" + "example/private")
    with pytest.raises(package.PackageError, match="privacy"):
        package.build(source, tmp_path / "bad.zip", "a" * 40)
