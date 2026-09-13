from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_import_does_not_create_home_files_or_start_processes(tmp_path: Path) -> None:
    home = tmp_path / "empty-home"
    home.mkdir()
    script = """
import subprocess
import sys

def boom(*args, **kwargs):
    raise AssertionError("import 期间启动了进程")

subprocess.Popen = boom
subprocess.run = boom
subprocess.call = boom
import asterun
print(asterun.__version__)
"""
    env = {
        "HOME": str(home),
        "ASTERUN_HOME": str(tmp_path / "missing-asterun-home"),
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0.1.0a13"
    assert list(home.iterdir()) == []
    assert not (tmp_path / "missing-asterun-home").exists()


def test_public_package_exports_only_version() -> None:
    import asterun

    assert asterun.__all__ == ["__version__"]
