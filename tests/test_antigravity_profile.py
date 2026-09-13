import hashlib
import json
from pathlib import Path

import pytest

from asterun.antigravity_profile import (
    SETTINGS_TEXT, bind_workspace, build_environment, main, prepare_home, validate_home, validate_workspace,
    upgrade_permissions, validate_workspace_binding,
)
from asterun.errors import AsterunError


def test_prepare_is_private_idempotent_and_never_overwrites(tmp_path):
    home = tmp_path / "native"
    result = prepare_home(home)
    assert result["created"] is True
    assert result["isolation"] == "policy_only"
    assert result["authentication_verified"] is False
    assert home.stat().st_mode & 0o777 == 0o700
    settings = Path(result["settings"])
    assert settings.stat().st_mode & 0o777 == 0o600
    assert json.loads(settings.read_text())["toolPermission"] == "request-review"
    assert prepare_home(home)["created"] is False
    settings.write_text('{"modelProvider":"gemini"}')
    with pytest.raises(AsterunError, match="偏离"):
        prepare_home(home)
    assert settings.read_text() == '{"modelProvider":"gemini"}'


@pytest.mark.parametrize("source", [
    ".gemini/GEMINI.md", ".gemini/settings.json", ".gemini/extensions/mcp.json",
    ".gemini/config/hooks.json", ".gemini/config/mcp_config.json",
    ".gemini/config/plugins/plugin.json", ".gemini/antigravity-cli/hooks.json",
    ".gemini/antigravity-cli/plugins/p/plugin.json", ".gemini/antigravity-cli/skills/a.md",
    ".agents/hooks.json", ".gemini/antigravity-cli/custom-model.json",
])
def test_additional_configuration_and_extension_sources_fail_closed(tmp_path, source):
    home = tmp_path / "native"
    prepare_home(home)
    file = home / source
    file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    file.write_text("configuration")
    file.chmod(0o600)
    with pytest.raises(AsterunError):
        validate_home(home)


def test_native_caches_allowed_without_reading_their_contents(tmp_path, monkeypatch):
    home = tmp_path / "native"
    prepare_home(home)
    cli = home / ".gemini" / "antigravity-cli"
    cache = cli / "cache"
    cache.mkdir(mode=0o755)
    (cache / "last_conversations.json").write_text("opaque native state")
    auth = cli / "auth.json"
    auth.write_bytes(b"opaque credentials, not even JSON")
    auth.chmod(0o600)
    original_read_text = Path.read_text
    def read_settings_only(path, *args, **kwargs):
        assert path.name == "settings.json"
        return original_read_text(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read_settings_only)
    assert validate_home(home) == home
    auth.chmod(0o644)
    with pytest.raises(AsterunError):
        validate_home(home)


@pytest.mark.parametrize("relative", [".gemini", ".gemini/config", ".gemini/antigravity-cli/settings.json", ".gemini/antigravity-cli/cache"])
def test_symlinks_in_profile_never_followed(tmp_path, relative):
    home = tmp_path / "native"
    prepare_home(home)
    source = home / relative
    if source.exists():
        source.rename(tmp_path / "original")
    source.symlink_to(tmp_path / "missing")
    with pytest.raises(AsterunError):
        validate_home(home)


def test_symlinks_and_external_write_permissions_rejected_in_native_cache(tmp_path):
    home = tmp_path / "native"
    prepare_home(home)
    cache = home / ".gemini" / "antigravity-cli" / "cache"
    cache.mkdir()
    (cache / "alias").symlink_to(tmp_path / "other")
    with pytest.raises(AsterunError):
        validate_home(home)
    (cache / "alias").unlink()
    (cache / "state").touch()
    (cache / "state").chmod(0o666)
    with pytest.raises(AsterunError):
        validate_home(home)


def test_global_empty_placeholders_allowed_but_nonempty_maps_rejected(tmp_path):
    home = tmp_path / "native"
    prepare_home(home)
    cli = home / ".gemini" / "antigravity-cli"
    (cli / "plugins").mkdir(mode=0o700)
    (cli / "hooks.json").touch(mode=0o600)
    assert validate_home(home) == home
    (cli / "hooks.json").write_text('{}')
    with pytest.raises(AsterunError):
        validate_home(home)


def test_environment_does_not_inherit_keys_loaders_or_parent_agent_controls(tmp_path):
    home = tmp_path / "native"
    prepare_home(home)
    source = {key: "untrusted" for key in (
        "HOME", "GEMINI_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_GEMINI_BASE_URL",
        "ANTIGRAVITY_LS_ADDRESS", "ANTIGRAVITY_CSRF_TOKEN", "ANTIGRAVITY_PERM_GRANTS",
        "AGY_ADC_AUTH", "JETSKI_APP_DATA_DIR", "CODEX_HOME", "XDG_CONFIG_HOME",
        "LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "PYTHONPATH", "SSH_AUTH_SOCK",
    )}
    source.update(PATH="/usr/bin:/bin", HTTPS_PROXY="http://proxy.invalid:8080")
    result = build_environment(home, source)
    assert "untrusted" not in result.values()
    assert result == {"HOME": str(home), "PATH": "/usr/bin:/bin", "HTTPS_PROXY": "http://proxy.invalid:8080"}


@pytest.mark.parametrize("name", [".agents", ".agent", ".gemini", "AGENTS.md", "GEMINI.md"])
def test_workspace_ancestor_customization_sources_rejected(tmp_path, name):
    parent = tmp_path / "snapshot"
    workspace = parent / "nested"
    workspace.mkdir(parents=True)
    assert validate_workspace(workspace) == workspace
    (parent / name).touch()
    with pytest.raises(AsterunError, match="祖先"):
        validate_workspace(workspace)


def test_invalid_or_shared_home_is_not_modified(tmp_path):
    home = tmp_path / "native"
    home.mkdir(mode=0o755)
    with pytest.raises(AsterunError):
        prepare_home(home)
    assert not any(home.iterdir())
    with pytest.raises(AsterunError):
        prepare_home("relative")


def test_settings_reject_duplicate_keys_or_extra_hooks(tmp_path):
    home = tmp_path / "native"
    prepare_home(home)
    settings = home / ".gemini" / "antigravity-cli" / "settings.json"
    settings.write_text(SETTINGS_TEXT.replace('"toolPermission": "request-review",', '"toolPermission": "always-proceed",\n  "toolPermission": "request-review",'))
    with pytest.raises(AsterunError):
        validate_home(home)


def test_path_resolution_errors_are_reported_as_configuration_errors(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError("opaque permission error")
    monkeypatch.setattr(Path, "resolve", denied)
    with pytest.raises(AsterunError, match="PermissionError"):
        prepare_home(tmp_path / "native")
    with pytest.raises(AsterunError, match="PermissionError"):
        validate_workspace(tmp_path)


def test_native_state_walk_is_bounded(tmp_path, monkeypatch):
    import asterun.antigravity_profile as profile
    home = tmp_path / "native"
    prepare_home(home)
    cache = home / ".gemini" / "antigravity-cli" / "cache"
    cache.mkdir()
    for number in range(3):
        (cache / str(number)).touch()
    monkeypatch.setattr(profile, "_MAX_STATE_ENTRIES", 2)
    with pytest.raises(AsterunError, match="上限"):
        validate_home(home)


def test_cli_inspection_reports_policy_only_without_authentication_claim(tmp_path, capsys):
    home = tmp_path / "native"
    assert main(["prepare", "--home", str(home)]) == 0
    capsys.readouterr()
    assert main(["inspect", "--home", str(home)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["profile"] == "workspace-read-v1"
    assert report["isolation"] == "policy_only"
    assert report["keyring_reuse_verified"] is False


def _native_sparse_settings(home):
    path = home / ".gemini" / "antigravity-cli" / "settings.json"
    value = json.loads(SETTINGS_TEXT)
    for key in ("toolPermission", "allowNonWorkspaceAccess", "useG1Credits", "artifactReviewPolicy", "notifications"):
        value.pop(key)
    value["permissions"].pop("allow")
    value["permissions"].pop("ask")
    path.write_text(json.dumps(value, sort_keys=True))
    return path, value


def test_native_sparse_settings_keep_effective_policy(tmp_path):
    home = tmp_path / "native"
    prepare_home(home)
    _native_sparse_settings(home)
    assert validate_home(home) == home
    assert prepare_home(home)["created"] is False


@pytest.mark.parametrize("change", [
    {"allowNonWorkspaceAccess": True}, {"useG1Credits": True},
    {"allowNonWorkspaceAccess": 0}, {"toolPermission": "always-proceed"},
    {"plugins": []}, {"permissions": {"deny": []}},
])
def test_sparse_configuration_does_not_permit_privilege_or_billing_changes(tmp_path, change):
    home = tmp_path / "native"
    prepare_home(home)
    path, value = _native_sparse_settings(home)
    value.update(change)
    path.write_text(json.dumps(value))
    with pytest.raises(AsterunError):
        validate_home(home)


@pytest.mark.parametrize("key", ["showTips", "showFeedbackSurvey", "permissions"])
def test_removing_required_nondefault_settings_does_not_restore_unsafe_defaults(tmp_path, key):
    home = tmp_path / "native"
    prepare_home(home)
    path, value = _native_sparse_settings(home)
    value.pop(key)
    path.write_text(json.dumps(value))
    with pytest.raises(AsterunError):
        validate_home(home)


def test_observed_native_state_and_empty_project_allowed(tmp_path, monkeypatch):
    home = tmp_path / "native"
    prepare_home(home)
    cli = home / ".gemini" / "antigravity-cli"
    for name in ("brain", "crashes", "knowledge", "log", "updater"):
        (cli / name).mkdir()
        (cli / name / "opaque-state").write_bytes(b"not configuration")
    for name in ("conversation_summaries.db", "conversation_summaries.db-shm", "conversation_summaries.db-wal", "installation_id", "last_check.timestamp", "jetski_state.pbtxt"):
        (cli / name).write_bytes(b"opaque native state")
    (cli / "jetski_state.pbtxt").chmod(0o600)
    config = home / ".gemini" / "config"
    (config / ".migrated").touch()
    (config / "mcp_config.json").touch()
    (config / "config.json").write_text(json.dumps({"userSettings": {"remoteControlHostname": "validation-host"}}))
    (config / "config.json").chmod(0o600)
    (config / "projects").mkdir()
    (config / "projects" / "default-cli-project.json").write_text(json.dumps({"id": "default-cli-project", "name": "CLI Project", "projectResources": {}}))
    original = Path.read_text
    def public_configuration_only(path, *args, **kwargs):
        assert path.name in {"settings.json", "config.json", "default-cli-project.json"}
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", public_configuration_only)
    assert validate_home(home) == home


@pytest.mark.parametrize("relative,payload", [
    ("config.json", {"userSettings": {"remoteControlHostname": "host", "useG1Credits": True}}),
    ("plugins.json", {"entries": [{"path": "/external/plugins"}]}),
    ("skills.json", {"inherits": [{"path": "/external/skills.json"}]}),
    ("projects/default-cli-project.json", {"id": "default-cli-project", "name": "CLI Project", "projectResources": {"unexpected": "/external"}}),
])
def test_global_native_config_cannot_import_sources_or_bind_other_workspaces(tmp_path, relative, payload):
    home = tmp_path / "native"
    prepare_home(home)
    path = home / ".gemini" / "config" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    with pytest.raises(AsterunError):
        validate_home(home)


def test_builtin_manifest_rejects_modified_and_additional_extensions(tmp_path, monkeypatch):
    import asterun.antigravity_profile as profile
    home = tmp_path / "native"
    prepare_home(home)
    builtin = home / ".gemini" / "antigravity-cli" / "builtin"
    skill = builtin / "skills" / "builtin-example" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    content = b"native bundle content"
    skill.write_bytes(content)
    monkeypatch.setattr(profile, "_BUILTIN_SHA256", {"skills/builtin-example/SKILL.md": hashlib.sha256(content).hexdigest()})
    assert validate_home(home) == home
    skill.write_bytes(b"changed native content")
    with pytest.raises(AsterunError):
        validate_home(home)
    skill.write_bytes(content)
    (builtin / "plugins").mkdir()
    with pytest.raises(AsterunError):
        validate_home(home)
    (builtin / "plugins").rmdir()
    skill.unlink()
    skill.symlink_to(tmp_path / "not-a-builtin")
    with pytest.raises(AsterunError):
        validate_home(home)


def test_only_empty_observed_playwright_cache_is_accepted(tmp_path):
    home = tmp_path / "native"
    prepare_home(home)
    cache = home / "Library" / "Caches" / "ms-playwright-go" / "1.57.0"
    cache.mkdir(parents=True)
    assert validate_home(home) == home
    (cache / "unverified-browser").touch()
    with pytest.raises(AsterunError):
        validate_home(home)


def test_native_log_alias_is_confined_to_its_owned_log_directory(tmp_path):
    home = tmp_path / "native"
    prepare_home(home)
    cli = home / ".gemini" / "antigravity-cli"
    (cli / "log").mkdir()
    target = cli / "log" / "cli-20260910_230714.log"
    target.touch()
    alias = cli / "cli.log"
    alias.symlink_to("log/cli-20260910_230714.log")
    assert validate_home(home) == home
    target.unlink()
    target.symlink_to(tmp_path / "outside")
    with pytest.raises(AsterunError):
        validate_home(home)
    alias.unlink()
    alias.symlink_to("../../outside")
    with pytest.raises(AsterunError):
        validate_home(home)


def test_native_oauth_token_is_checked_only_by_private_metadata(tmp_path, monkeypatch):
    home = tmp_path / "native"
    prepare_home(home)
    token = home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    token.write_bytes(b"opaque native credentials")
    token.chmod(0o600)
    original = Path.open
    def noncredential_open(path, *args, **kwargs):
        assert path != token
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", noncredential_open)
    assert validate_home(home) == home
    token.chmod(0o644)
    with pytest.raises(AsterunError):
        validate_home(home)


def test_implicit_state_only_allows_regular_uuid_protobuf_files(tmp_path):
    home = tmp_path / "native"
    prepare_home(home)
    implicit = home / ".gemini" / "antigravity-cli" / "implicit"
    implicit.mkdir()
    (implicit / "12345678-abcd-1234-5678-123456789abc.pb").write_bytes(b"opaque protobuf")
    assert validate_home(home) == home
    (implicit / "hooks.json").touch()
    with pytest.raises(AsterunError):
        validate_home(home)


def test_native_encoder_requires_its_exact_manifest_and_no_other_binaries(tmp_path, monkeypatch):
    import asterun.antigravity_profile as profile
    home = tmp_path / "native"
    prepare_home(home)
    bin_dir = home / ".gemini" / "antigravity-cli" / "bin"
    bin_dir.mkdir()
    content = b"known upstream executable bytes"
    binary = bin_dir / "webm_encoder"
    binary.write_bytes(content)
    binary.chmod(0o755)
    monkeypatch.setattr(profile, "_WEBM_ENCODER_SIZE", len(content))
    monkeypatch.setattr(profile, "_WEBM_ENCODER_SHA256", hashlib.sha256(content).hexdigest())
    assert validate_home(home) == home
    binary.write_bytes(b"x" * len(content))
    with pytest.raises(AsterunError):
        validate_home(home)
    binary.write_bytes(content)
    (bin_dir / "additional-executable").touch()
    with pytest.raises(AsterunError):
        validate_home(home)


def test_native_workspace_trust_must_bind_to_the_dispatched_workspace(tmp_path):
    home = tmp_path / "native"
    prepare_home(home)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    settings = home / ".gemini" / "antigravity-cli" / "settings.json"
    value = json.loads(SETTINGS_TEXT)
    value["trustedWorkspaces"] = [str(workspace)]
    settings.write_text(json.dumps(value))
    assert validate_home(home) == home
    assert validate_workspace_binding(home, workspace) == workspace
    with pytest.raises(AsterunError, match="不一致"):
        validate_workspace_binding(home, other)
    value["trustedWorkspaces"].append(str(other))
    settings.write_text(json.dumps(value))
    with pytest.raises(AsterunError):
        validate_home(home)


@pytest.mark.parametrize("name,suffix,mode", [("annotations", ".pbtxt", 0o644), ("presence", ".lock", 0o600)])
def test_postrun_annotation_and_presence_states_are_metadata_only(tmp_path, monkeypatch, name, suffix, mode):
    home = tmp_path / "native"
    prepare_home(home)
    directory = home / ".gemini" / "antigravity-cli" / name
    directory.mkdir()
    state = directory / ("12345678-abcd-1234-5678-123456789abc" + suffix)
    state.write_bytes(b"opaque native state")
    state.chmod(mode)
    original = Path.open
    def configuration_only(path, *args, **kwargs):
        assert path != state
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", configuration_only)
    assert validate_home(home) == home
    state.unlink()
    state.symlink_to(tmp_path / "outside")
    with pytest.raises(AsterunError):
        validate_home(home)
    state.unlink()
    (directory / "hooks.json").touch()
    with pytest.raises(AsterunError):
        validate_home(home)


def test_native_scratch_is_accepted_only_while_empty(tmp_path):
    home = tmp_path / "native"
    prepare_home(home)
    scratch = home / ".gemini" / "antigravity-cli" / "scratch"
    scratch.mkdir()
    assert validate_home(home) == home
    (scratch / "unverified-file").touch()
    with pytest.raises(AsterunError):
        validate_home(home)


def test_explicit_binding_only_adds_one_read_rule_and_is_idempotent(tmp_path):
    home = tmp_path / "native"
    prepare_home(home)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path, before = _native_sparse_settings(home)
    result = bind_workspace(home, workspace)
    after = json.loads(path.read_text())
    assert result["changed"] is True
    assert after["permissions"].pop("allow") == [f"read_file({workspace})"]
    assert after == before
    assert "trustedWorkspaces" not in after
    assert path.stat().st_mode & 0o777 == 0o600
    assert validate_workspace_binding(home, workspace) == workspace
    saved = path.read_bytes()
    assert bind_workspace(home, workspace)["changed"] is False
    assert path.read_bytes() == saved


def test_binding_never_overwrites_different_existing_read_or_trust_root(tmp_path):
    home = tmp_path / "native"
    prepare_home(home)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    settings = home / ".gemini" / "antigravity-cli" / "settings.json"
    bind_workspace(home, first)
    before = settings.read_bytes()
    with pytest.raises(AsterunError, match="不一致"):
        bind_workspace(home, second)
    assert settings.read_bytes() == before
    value = json.loads(SETTINGS_TEXT)
    value["trustedWorkspaces"] = [str(first)]
    settings.write_text(json.dumps(value))
    before = settings.read_bytes()
    with pytest.raises(AsterunError, match="不一致"):
        bind_workspace(home, second)
    assert settings.read_bytes() == before


@pytest.mark.parametrize("name", ["with*star", "with(open", "with)close", "with\nnewline", "with\\slash", "with?glob", "with[glob]"])
def test_binding_rejects_permission_syntax_characters_before_writing(tmp_path, name):
    home = tmp_path / "native"
    prepare_home(home)
    workspace = tmp_path / name
    workspace.mkdir()
    settings = home / ".gemini" / "antigravity-cli" / "settings.json"
    before = settings.read_bytes()
    with pytest.raises(AsterunError, match="权限语法"):
        bind_workspace(home, workspace)
    assert settings.read_bytes() == before


@pytest.mark.parametrize("allow", [["read_file(*)"], ["command(git)"], ["read_file(/tmp/../tmp)"], ["read_file(/tmp)", "read_file(/var)"]])
def test_manually_widened_allow_rules_fail_closed(tmp_path, allow):
    home = tmp_path / "native"
    prepare_home(home)
    settings = home / ".gemini" / "antigravity-cli" / "settings.json"
    value = json.loads(SETTINGS_TEXT)
    value["permissions"]["allow"] = allow
    settings.write_text(json.dumps(value))
    with pytest.raises(AsterunError):
        validate_home(home)


def test_bound_workspace_survives_native_sparse_persistence(tmp_path):
    home = tmp_path / "native"
    prepare_home(home)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bind_workspace(home, workspace)
    settings = home / ".gemini" / "antigravity-cli" / "settings.json"
    value = json.loads(settings.read_text())
    for key in ("toolPermission", "allowNonWorkspaceAccess", "useG1Credits", "artifactReviewPolicy", "notifications"):
        value.pop(key)
    value["permissions"].pop("ask")
    settings.write_text(json.dumps(value, sort_keys=True))
    assert validate_home(home) == home
    assert validate_workspace_binding(home, workspace) == workspace


def test_bind_cli_does_not_grant_native_trust(tmp_path, capsys):
    home = tmp_path / "native"
    prepare_home(home)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assert main(["bind-workspace", "--home", str(home), "--workspace", str(workspace)]) == 0
    assert json.loads(capsys.readouterr().out)["changed"] is True
    settings = json.loads((home / ".gemini" / "antigravity-cli" / "settings.json").read_text())
    assert "trustedWorkspaces" not in settings


def test_binding_atomic_replace_failure_preserves_original_and_cleans_temp(tmp_path, monkeypatch):
    import asterun.antigravity_profile as profile
    home = tmp_path / "native"
    prepare_home(home)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = home / ".gemini" / "antigravity-cli" / "settings.json"
    original = settings.read_bytes()
    def denied(*args, **kwargs):
        raise PermissionError("cannot replace")
    monkeypatch.setattr(profile.os, "replace", denied)
    with pytest.raises(AsterunError, match="PermissionError"):
        bind_workspace(home, workspace)
    assert settings.read_bytes() == original
    assert sorted(path.name for path in settings.parent.iterdir()) == ["settings.json"]


def _legacy_profile(tmp_path):
    home = tmp_path / "native"
    prepare_home(home)
    settings, value = _native_sparse_settings(home)
    value["toolPermission"] = "strict"
    settings.write_text(json.dumps(value))
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o700)
    return home, settings, value, backups / "settings.before.json"


def test_legacy_profile_requires_explicit_upgrade_without_mutation(tmp_path, capsys):
    home, settings, _, _ = _legacy_profile(tmp_path)
    original = settings.read_bytes()
    for operation in (validate_home, prepare_home, build_environment):
        with pytest.raises(AsterunError, match="显式迁移") as error:
            operation(home)
        assert "upgrade-permissions" in error.value.next_action
        assert settings.read_bytes() == original
    assert main(["inspect", "--home", str(home)]) == 1
    assert "upgrade-permissions" in json.loads(capsys.readouterr().out)["error"]["next_action"]


def test_permission_upgrade_preserves_bindings_credentials_and_exact_rollback(tmp_path, monkeypatch):
    home, path, settings, backup = _legacy_profile(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings["trustedWorkspaces"] = [str(workspace)]
    settings["permissions"]["allow"] = [f"read_file({workspace})"]
    path.write_text(json.dumps(settings, indent=4) + "\n")
    original = path.read_bytes()
    credential = path.parent / "antigravity-oauth-token"
    credential.write_bytes(b"opaque test credential")
    credential.chmod(0o600)
    credential_metadata = credential.stat()
    original_open = Path.open
    def noncredential_open(path, *args, **kwargs):
        assert path != credential
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", noncredential_open)
    report = upgrade_permissions(home, backup)
    assert report["changed"] is True
    assert report["previous_permission_mode"] == "strict"
    assert report["permission_mode"] == "request-review"
    assert report["backup_sha256"] == hashlib.sha256(original).hexdigest()
    assert report["settings_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert backup.read_bytes() == original
    assert backup.stat().st_mode & 0o777 == 0o600
    assert path.stat().st_mode & 0o777 == 0o600
    assert credential.stat() == credential_metadata
    actual = json.loads(path.read_text())
    settings["toolPermission"] = "request-review"
    assert actual == settings
    assert validate_workspace_binding(home, workspace) == workspace
    path.write_bytes(backup.read_bytes())
    assert path.read_bytes() == original
    with pytest.raises(AsterunError, match="显式迁移"):
        validate_home(home)


@pytest.mark.parametrize("change", [
    {"toolPermission": "always-proceed"}, {"toolPermission": "request-review"},
    {"permissions": {"deny": []}}, {"useG1Credits": True}, {"hooks": {}},
])
def test_upgrade_rejects_nonlegacy_or_changed_policy_without_backup(tmp_path, change):
    home, path, settings, backup = _legacy_profile(tmp_path)
    settings.update(change)
    path.write_text(json.dumps(settings))
    original = path.read_bytes()
    with pytest.raises(AsterunError):
        upgrade_permissions(home, backup)
    assert path.read_bytes() == original
    assert not backup.exists()


def test_upgrade_validates_native_sources_before_creating_backup(tmp_path):
    home, path, _, backup = _legacy_profile(tmp_path)
    unknown = path.parent / "custom-model.json"
    unknown.write_text('{}')
    unknown.chmod(0o600)
    original = path.read_bytes()
    with pytest.raises(AsterunError, match="未知配置"):
        upgrade_permissions(home, backup)
    assert path.read_bytes() == original
    assert not backup.exists()


@pytest.mark.parametrize("location", ["existing", "home", "workspace", "shared", "relative"])
def test_upgrade_rejects_unsafe_backup_destinations_without_modifying_settings(tmp_path, location):
    home, path, settings, backup = _legacy_profile(tmp_path)
    if location == "existing":
        backup.write_bytes(b"existing backup")
    elif location == "home":
        backup = path.parent / "settings.backup.json"
    elif location == "workspace":
        workspace = tmp_path / "workspace"
        workspace.mkdir(mode=0o700)
        settings["permissions"]["allow"] = [f"read_file({workspace})"]
        path.write_text(json.dumps(settings))
        backup = workspace / "settings.backup.json"
    elif location == "shared":
        backup.parent.chmod(0o755)
    else:
        backup = Path("relative.backup.json")
    original = path.read_bytes()
    with pytest.raises(AsterunError):
        upgrade_permissions(home, backup)
    assert path.read_bytes() == original
    if location == "existing":
        assert backup.read_bytes() == b"existing backup"
    else:
        assert not backup.exists()


def test_upgrade_replace_failure_preserves_source_and_recoverable_backup(tmp_path, monkeypatch):
    import asterun.antigravity_profile as profile
    home, settings, _, backup = _legacy_profile(tmp_path)
    original = settings.read_bytes()
    def denied(*args, **kwargs):
        raise PermissionError("cannot replace")
    monkeypatch.setattr(profile.os, "replace", denied)
    with pytest.raises(AsterunError, match="PermissionError"):
        upgrade_permissions(home, backup)
    assert settings.read_bytes() == original
    assert backup.read_bytes() == original
    assert sorted(path.name for path in settings.parent.iterdir()) == ["settings.json"]


def test_upgrade_stops_when_native_cli_changes_settings_during_backup(tmp_path, monkeypatch):
    import asterun.antigravity_profile as profile
    home, settings, _, backup = _legacy_profile(tmp_path)
    original = settings.read_bytes()
    changed = original + b"\n"
    original_mkstemp = profile.tempfile.mkstemp
    def native_rewrite(*args, **kwargs):
        settings.write_bytes(changed)
        return original_mkstemp(*args, **kwargs)
    monkeypatch.setattr(profile.tempfile, "mkstemp", native_rewrite)
    with pytest.raises(AsterunError, match="迁移期间变化"):
        upgrade_permissions(home, backup)
    assert settings.read_bytes() == changed
    assert backup.read_bytes() == original
    assert sorted(path.name for path in settings.parent.iterdir()) == ["settings.json"]


def test_upgrade_cli_reports_backup_and_new_effective_mode(tmp_path, capsys):
    home, _, _, backup = _legacy_profile(tmp_path)
    assert main(["upgrade-permissions", "--home", str(home), "--backup", str(backup)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["backup"] == str(backup)
    assert report["permission_mode"] == "request-review"
    assert validate_home(home) == home
