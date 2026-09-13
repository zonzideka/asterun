import hashlib
import importlib.util
import io
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "integrations/grokbot/scripts/fetch_runtime.py"
spec = importlib.util.spec_from_file_location("fetch_grokbot_runtime", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def lock(blob=b"wheel"):
    return {"core_version": "0.1.0a13", "core_wheel": {
        "filename": "asterun-0.1.0a13-py3-none-any.whl",
        "url": "https://github.com/zonzideka/asterun/releases/download/v0.1.0a13/asterun-0.1.0a13-py3-none-any.whl",
        "sha256": hashlib.sha256(blob).hexdigest()}}


def test_verified_download_reuses_without_network(tmp_path):
    directory = tmp_path.resolve() / "downloads"
    first = module.fetch(lock(), directory, opener=lambda *a, **k: io.BytesIO(b"wheel"))
    assert not first["installed"] and not first["reused"]
    assert Path(first["path"]).stat().st_mode & 0o777 == 0o600
    def unexpected(*args, **kwargs):
        pytest.fail("existing verified wheel should not be downloaded again")
    assert module.fetch(lock(), directory, opener=unexpected)["reused"]


def test_wrong_digest_creates_no_wheel(tmp_path):
    directory = tmp_path.resolve() / "downloads"
    with pytest.raises(ValueError, match="release lock"):
        module.fetch(lock(), directory, opener=lambda *a, **k: io.BytesIO(b"changed"))
    assert list(directory.iterdir()) == []


def test_conflicting_file_and_symlinks_preserved(tmp_path):
    directory = tmp_path.resolve()
    target = directory / lock()["core_wheel"]["filename"]
    target.write_bytes(b"keep")
    with pytest.raises(ValueError):
        module.fetch(lock(), directory)
    assert target.read_bytes() == b"keep"
    target.unlink()
    target.symlink_to(directory / "missing")
    with pytest.raises(ValueError, match="symbolic"):
        module.fetch(lock(), directory)
    assert target.is_symlink()
