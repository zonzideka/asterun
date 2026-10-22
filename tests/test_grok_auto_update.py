"""Grok profiles follow the latest CLI release by default."""
import tomllib

import pytest

from asterun import grok_profile
from asterun.errors import AsterunError


def test_new_profile_enables_auto_update_and_environment_does_not_disable_it(tmp_path):
    home = tmp_path / "grok"
    grok_profile.prepare_home(home)
    assert tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))["cli"]["auto_update"] is True
    env = grok_profile.build_environment(home, {"PATH": "/usr/bin", "GROK_DISABLE_AUTOUPDATER": "1"})
    assert "GROK_DISABLE_AUTOUPDATER" not in env


@pytest.mark.parametrize("value,valid", [("false", True), ("true", True), ('"yes"', False), ("1", False)])
def test_auto_update_is_an_operator_boolean(tmp_path, value, valid):
    home = tmp_path / "grok"
    grok_profile.prepare_home(home)
    config = home / "config.toml"
    config.write_text(config.read_text(encoding="utf-8").replace("auto_update = true", f"auto_update = {value}"), encoding="utf-8")
    if valid:
        assert grok_profile.validate_home(home) == home.resolve()
    else:
        with pytest.raises(AsterunError):
            grok_profile.validate_home(home)


def test_other_cli_keys_still_must_match(tmp_path):
    home = tmp_path / "grok"
    grok_profile.prepare_home(home)
    config = home / "config.toml"
    config.write_text(config.read_text(encoding="utf-8").replace('installer = "internal"', 'installer = "npm"'), encoding="utf-8")
    with pytest.raises(AsterunError):
        grok_profile.validate_home(home)
