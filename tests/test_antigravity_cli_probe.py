"""CLI version and feature probing for the core Antigravity backend."""
from pathlib import Path
import sys

import pytest

from asterun.backends import antigravity
from asterun.errors import AsterunError


def _fake(tmp_path, version="1.3.2", flags=antigravity.REQUIRED_CLI_FLAGS, name="agy"):
    path = tmp_path / name
    lines = "".join(f"    print('  {flag}  x', file=sys.stderr)\n" for flag in flags)
    path.write_text(
        f"#!{sys.executable}\nimport os, sys\n"
        "assert os.environ.get('AGY_CLI_DISABLE_AUTO_UPDATE') == '1'\n"
        "assert os.environ['HOME'] != os.path.expanduser('~root')\n"
        f"if sys.argv[1:] == ['--version']:\n    print({version!r})\n    raise SystemExit(0)\n"
        f"if sys.argv[1:] == ['--help']:\n{lines}    raise SystemExit(0)\n"
        "raise SystemExit(3)\n")
    path.chmod(0o700)
    return path


@pytest.mark.parametrize("text,expected", [
    ("1.2.0", (1, 2, 0)), ("v1.3.2", (1, 3, 2)), ("agy 1.4.2", (1, 4, 2)),
    ("Antigravity CLI 2.0.0-beta.1", (2, 0, 0)), ("garbage", None), ("1.2", None),
])
def test_parse_cli_version(text, expected):
    assert antigravity.parse_cli_version(text) == expected


@pytest.mark.parametrize("version", ["1.2.0", "1.3.2", "1.4.2", "2.0.0"])
def test_probe_accepts_any_version_at_or_above_minimum(tmp_path, version):
    result = antigravity.probe_cli(_fake(tmp_path, version, name=f"agy-{version}"))
    assert result["version"] == version


def test_probe_rejects_old_or_unparseable_versions(tmp_path):
    with pytest.raises(AsterunError, match="最低版本"):
        antigravity.probe_cli(_fake(tmp_path, "1.1.9", name="old"))
    with pytest.raises(AsterunError, match="版本号"):
        antigravity.probe_cli(_fake(tmp_path, "unknown", name="odd"))


def test_probe_reports_missing_features_clearly(tmp_path):
    flags = tuple(flag for flag in antigravity.REQUIRED_CLI_FLAGS if flag != "--print-timeout")
    with pytest.raises(AsterunError, match="--print-timeout"):
        antigravity.probe_cli(_fake(tmp_path, flags=flags, name="nofeature"))


def test_probe_never_touches_the_real_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "real-home"))
    antigravity.probe_cli(_fake(tmp_path, name="isolated"))
    assert not (tmp_path / "real-home").exists()


def test_inspect_does_not_execute_binary_without_live_gate(tmp_path, monkeypatch):
    from asterun.backends import antigravity
    calls = []
    monkeypatch.setattr(antigravity, "probe_cli", lambda binary: calls.append(binary) or {"version": "9.9.9"})
    monkeypatch.delenv(antigravity.LIVE_ENV, raising=False)
    binary = tmp_path / "agy"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    from asterun.config import BackendConfig
    backend = antigravity.AntigravityBackend(BackendConfig(
        name="agy", kind="antigravity", enabled=True, bin=str(binary)))
    result = backend.inspect()
    assert calls == []
    assert result["cli_version"] is None
    monkeypatch.setenv(antigravity.LIVE_ENV, "1")
    again = backend.inspect()
    # inspect probes once; can_dispatch's preflight probes the same binary.
    assert calls == [binary.resolve(), binary.resolve()]
    assert again["cli_version"] == "9.9.9"


def test_probe_caps_output_while_reading(tmp_path):
    path = tmp_path / "noisy"
    path.write_text(f"#!{sys.executable}\nimport sys\nsys.stdout.write('x' * 10_000_000)\n")
    path.chmod(0o700)
    with pytest.raises(AsterunError, match="上限"):
        antigravity.probe_cli(path)


def test_probe_times_out_and_kills_the_process_group(tmp_path, monkeypatch):
    monkeypatch.setattr(antigravity, "_PROBE_TIMEOUT", 1)
    marker = tmp_path / "child-alive"
    path = tmp_path / "hang"
    path.write_text(
        f"#!{sys.executable}\nimport subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', "
        f"\"import time; time.sleep(5); open({str(marker)!r}, 'w').close()\"])\n"
        "time.sleep(30)\n")
    path.chmod(0o700)
    with pytest.raises(AsterunError, match="超时"):
        antigravity.probe_cli(path)
    import time
    time.sleep(6)
    assert not marker.exists()
