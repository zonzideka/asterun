import json
import os
from pathlib import Path
import stat
import subprocess
import tomllib

import pytest

from asterun.errors import AsterunError
from asterun.grok_profile import (
    CODE_PROFILE, CODE_SANDBOX, CODE_SANDBOX_TEXT, PROFILE, build_environment,
    main, prepare_home, validate_home, validate_workspace,
)


def test_prepare_never_overwrites_and_inspection_does_not_read_credentials(tmp_path, monkeypatch):
    home = tmp_path / "native"
    assert prepare_home(home)["created"] is True
    assert prepare_home(home)["created"] is False
    auth = home / "auth.json"
    auth.write_text("not-even-json")
    auth.chmod(0o600)
    read_text = Path.read_text
    def no_credential_read(path, *args, **kwargs):
        assert path != auth
        return read_text(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", no_credential_read)
    assert validate_home(home) == home
    config = home / "config.toml"
    config.write_text('[auth]\nauth_provider_command="unexpected command"\n')
    with pytest.raises(AsterunError):
        prepare_home(home)
    assert "unexpected command" in config.read_text()


@pytest.mark.parametrize("source", ["hooks/hook.json", "managed_config.toml", "requirements.toml", "plugins/manifest.json"])
def test_extra_native_execution_sources_fail_closed(tmp_path, source):
    home = tmp_path / "native"
    prepare_home(home)
    file = home / source
    file.parent.mkdir(exist_ok=True)
    file.write_text("additional execution source")
    with pytest.raises(AsterunError):
        validate_home(home)


def test_empty_native_placeholders_allowed_but_symlink_auth_rejected(tmp_path):
    home = tmp_path / "native"
    prepare_home(home)
    (home / "hooks").mkdir()
    (home / "managed_config.toml").touch()
    assert validate_home(home) == home
    secret = tmp_path / "auth.json"
    secret.write_text("opaque")
    secret.chmod(0o600)
    (home / "auth.json").symlink_to(secret)
    with pytest.raises(AsterunError):
        validate_home(home)


def test_environment_removes_auth_providers_overlays_keys_and_loader_injection(tmp_path):
    home = tmp_path / "native"
    prepare_home(home)
    env = build_environment(home, {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin",
        "GROK_AUTH_PROVIDER_COMMAND": "bad", "GROK_CONFIG": "bad", "GROK_HOME": "bad",
        "XAI_API_KEY": "bad", "GROK_MODEL": "bad", "LD_PRELOAD": "bad", "PYTHONPATH": "bad"})
    assert "bad" not in env.values()
    assert env["GROK_HOME"] == str(home)
    assert env["GROK_DISABLE_API_KEY_AUTH"] == "true"
    assert env["GROK_CLAUDE_HOOKS_ENABLED"] == "false"


def test_workspace_ancestor_hooks_and_permissive_home_rejected(tmp_path):
    workspace = tmp_path / "work" / "nested"
    workspace.mkdir(parents=True)
    (workspace.parent / ".grok").mkdir()
    (workspace.parent / ".grok" / "bin").mkdir()
    assert validate_workspace(workspace) == workspace
    (workspace.parent / ".grok" / "hooks").mkdir()
    with pytest.raises(AsterunError):
        validate_workspace(workspace)
    home = tmp_path / "native"
    prepare_home(home)
    home.chmod(0o755)
    with pytest.raises(AsterunError):
        validate_home(home)


def test_coding_home_upgrade_preserves_config_and_unread_credentials(tmp_path, monkeypatch):
    home = tmp_path / "native"
    prepare_home(home)
    config_before = (home / "config.toml").read_bytes()
    auth = home / "auth.json"
    auth.write_bytes(b"opaque-native-credential")
    auth.chmod(0o600)
    auth_stat = auth.stat()
    read_text = Path.read_text

    def no_credential_read(path, *args, **kwargs):
        assert path != auth
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", no_credential_read)
    with pytest.raises(AsterunError):
        validate_home(home, execution_profile=CODE_PROFILE)
    assert prepare_home(home, execution_profile=CODE_PROFILE) == {
        "profile": CODE_PROFILE, "home": str(home), "created": False,
    }
    assert validate_home(home, execution_profile=CODE_PROFILE) == home
    assert validate_home(home) == home  # Existing text-only callers stay compatible.
    assert (home / "config.toml").read_bytes() == config_before
    assert auth.stat() == auth_stat
    sandbox = home / "sandbox.toml"
    assert stat.S_IMODE(sandbox.stat().st_mode) == 0o600
    assert tomllib.loads(sandbox.read_text()) == {
        "profiles": {CODE_SANDBOX: {"extends": "workspace"}},
    }
    before = sandbox.stat()
    prepare_home(home, execution_profile=CODE_PROFILE)
    assert sandbox.stat() == before


def test_new_coding_home_is_explicit_and_text_default_does_not_add_sandbox(tmp_path):
    text_home = tmp_path / "text"
    assert prepare_home(text_home)["profile"] == PROFILE
    assert not (text_home / "sandbox.toml").exists()
    code_home = tmp_path / "code"
    result = prepare_home(code_home, execution_profile=CODE_PROFILE)
    assert result["created"] is True
    assert result["profile"] == CODE_PROFILE
    assert (code_home / "sandbox.toml").read_text() == CODE_SANDBOX_TEXT
    assert prepare_home(code_home)["created"] is False


def test_coding_upgrade_fills_native_empty_placeholder_without_replacing_it(tmp_path):
    home = tmp_path / "native"
    prepare_home(home)
    auth = home / "auth.json"
    auth.write_bytes(b"opaque-native-credential")
    auth.chmod(0o600)
    config_before = (home / "config.toml").read_bytes()
    auth_before = auth.stat()
    sandbox = home / "sandbox.toml"
    sandbox.touch(mode=0o600)
    placeholder_inode = sandbox.stat().st_ino
    assert validate_home(home) == home
    result = prepare_home(home, execution_profile=CODE_PROFILE)
    assert result["created"] is False
    assert sandbox.read_text() == CODE_SANDBOX_TEXT
    assert sandbox.stat().st_ino == placeholder_inode
    assert auth.stat() == auth_before
    assert (home / "config.toml").read_bytes() == config_before
    assert validate_home(home, execution_profile=CODE_PROFILE) == home


def test_coding_upgrade_rejects_permissive_empty_native_placeholder(tmp_path):
    home = tmp_path / "native"
    prepare_home(home)
    sandbox = home / "sandbox.toml"
    sandbox.touch()
    sandbox.chmod(0o644)
    with pytest.raises(AsterunError):
        prepare_home(home, execution_profile=CODE_PROFILE)
    assert sandbox.read_bytes() == b""


def test_coding_upgrade_does_not_write_through_hardlinked_empty_placeholder(tmp_path):
    home = tmp_path / "native"
    prepare_home(home)
    other = tmp_path / "existing-native-file"
    other.touch(mode=0o600)
    (home / "sandbox.toml").hardlink_to(other)
    with pytest.raises(AsterunError):
        prepare_home(home, execution_profile=CODE_PROFILE)
    assert other.read_bytes() == b""


@pytest.mark.parametrize("contents", [
    "# unknown empty profile\n", "invalid TOML [",
    '[profiles.other]\nextends = "workspace"\n',
    f'[profiles.{CODE_SANDBOX}]\nextends = "off"\n',
    CODE_SANDBOX_TEXT + 'read_write = ["/"]\n',
    CODE_SANDBOX_TEXT + '[profiles.extra]\nextends = "devbox"\n',
])
def test_coding_prepare_never_overwrites_unknown_sandbox(tmp_path, contents):
    home = tmp_path / "native"
    prepare_home(home)
    sandbox = home / "sandbox.toml"
    sandbox.write_text(contents)
    sandbox.chmod(0o600)
    before = sandbox.read_bytes()
    with pytest.raises(AsterunError):
        prepare_home(home, execution_profile=CODE_PROFILE)
    assert sandbox.read_bytes() == before


@pytest.mark.parametrize("kind", ["symlink", "permissive", "directory"])
def test_coding_sandbox_rejects_unsafe_native_paths(tmp_path, kind):
    home = tmp_path / "native"
    prepare_home(home)
    sandbox = home / "sandbox.toml"
    if kind == "symlink":
        other = tmp_path / "other.toml"
        other.write_text(CODE_SANDBOX_TEXT)
        other.chmod(0o600)
        sandbox.symlink_to(other)
    elif kind == "directory":
        sandbox.mkdir(mode=0o700)
    else:
        sandbox.write_text(CODE_SANDBOX_TEXT)
        sandbox.chmod(0o644)
    with pytest.raises(AsterunError):
        validate_home(home, execution_profile=CODE_PROFILE)


def _git(*args):
    env = {"PATH": os.environ.get("PATH", ""), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull}
    subprocess.run(["git", "-c", f"core.hooksPath={os.devnull}", *map(str, args)],
                   env=env, capture_output=True, check=True)


@pytest.mark.parametrize("git_layout", ["directory", "worktree-file"])
def test_coding_workspace_stops_at_git_root_not_native_home(tmp_path, git_layout):
    native_grok = tmp_path / ".grok"
    native_grok.mkdir()
    (native_grok / "config.toml").write_text("external native config")
    root = tmp_path / "repo"
    if git_layout == "directory":
        _git("init", root)
    else:
        source = tmp_path / "source"
        _git("init", source)
        _git("-C", source, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
             "commit", "--allow-empty", "-m", "Fixture")
        _git("-C", source, "worktree", "add", "--detach", root)
    workspace = root / "src" / "package"
    workspace.mkdir(parents=True)
    assert validate_workspace(workspace, execution_profile=CODE_PROFILE) == workspace
    with pytest.raises(AsterunError):
        validate_workspace(workspace)  # Preserve the original text-only boundary.
    project_config = root / ".grok"
    project_config.mkdir()
    (project_config / "config.toml").write_text("project source")
    with pytest.raises(AsterunError):
        validate_workspace(workspace, execution_profile=CODE_PROFILE)


def test_coding_workspace_checks_all_in_repo_ancestors_and_non_git_fallback(tmp_path):
    root = tmp_path / "repo"
    workspace = root / "src" / "package"
    workspace.mkdir(parents=True)
    _git("init", root)
    source = root / "src" / ".grok"
    source.mkdir()
    (source / "hooks").mkdir()
    with pytest.raises(AsterunError):
        validate_workspace(workspace, execution_profile=CODE_PROFILE)
    (source / "hooks").rmdir()
    source.rmdir()
    outside = tmp_path / ".grok"
    outside.mkdir()
    (outside / "skills").mkdir()
    assert validate_workspace(workspace, execution_profile=CODE_PROFILE) == workspace
    (root / ".git").rename(root / "inactive-git-metadata")
    with pytest.raises(AsterunError):
        validate_workspace(workspace, execution_profile=CODE_PROFILE)


@pytest.mark.parametrize("marker", ["empty-file", "empty-directory", "broken-worktree"])
def test_forged_git_marker_cannot_hide_project_configuration_ancestor(tmp_path, marker):
    root = tmp_path / "repo"
    _git("init", root)
    source = root / ".grok"
    source.mkdir()
    (source / "config.toml").write_text("parent project source")
    workspace = root / "nested"
    workspace.mkdir()
    fake = workspace / ".git"
    if marker == "empty-file":
        fake.touch()
    elif marker == "empty-directory":
        fake.mkdir()
    else:
        fake.write_text("gitdir: /nonexistent-asterun-fixture\n")
    with pytest.raises(AsterunError):
        validate_workspace(workspace, execution_profile=CODE_PROFILE)


def test_git_environment_override_cannot_change_workspace_boundary(tmp_path, monkeypatch):
    workspace = tmp_path / "nongit" / "nested"
    workspace.mkdir(parents=True)
    source = workspace.parent / ".grok"
    source.mkdir()
    (source / "config.toml").write_text("project source")
    repo = tmp_path / "other"
    _git("init", repo)
    monkeypatch.setenv("GIT_DIR", str(repo / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(workspace))
    with pytest.raises(AsterunError):
        validate_workspace(workspace, execution_profile=CODE_PROFILE)


def test_coding_environment_and_cli_require_explicit_prepared_profile(tmp_path, capsys):
    home = tmp_path / "native"
    prepare_home(home)
    args = ["--home", str(home), "--execution-profile", CODE_PROFILE]
    assert main(["inspect", *args]) == 1
    capsys.readouterr()
    assert main(["prepare", *args]) == 0
    assert json.loads(capsys.readouterr().out)["profile"] == CODE_PROFILE
    assert main(["inspect", *args]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["profile"] == CODE_PROFILE
    assert result["authentication_verified"] is False
    env = build_environment(home, {"PATH": "/usr/bin", "GROK_SANDBOX": "off"},
                            execution_profile=CODE_PROFILE)
    assert "GROK_SANDBOX" not in env
    assert env["GROK_HOME"] == str(home)


def test_unknown_execution_profile_has_no_filesystem_side_effect(tmp_path):
    home = tmp_path / "native"
    with pytest.raises(AsterunError):
        prepare_home(home, execution_profile="unrestricted")
    assert not home.exists()
