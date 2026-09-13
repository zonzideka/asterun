from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
import pickle
import traceback

import pytest

from asterun.connections.credentials import MAX_SECRET_BYTES, resolve_credentials
from asterun.errors import AsterunError


@pytest.fixture
def credential_context():
    manifest_path = Path(__file__).parent / "fixtures/plugins/external_fake/src/asterun_external_fake/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["auth_modes"] = ["api_key", "user_api_key", "native_account", "oauth", "none"]
    manifest["permissions"]["credential_classes"] = ["api_key", "native_cli_account_session", "oauth_access_token"]
    manifest["extensions"]["asterun.credentials"] = {"credential_class": "api_key", "env_var": "EXAMPLE_API_KEY"}
    connection = {"plugin_id": manifest["plugin_id"], "enabled": True, "provider_account_ref": "provider-example", "auth_mode": "api_key", "options": {}}
    account = {"provider": "example", "credential_ref": "env:SOURCE_PLUGIN_KEY", "credential_revision": 1, "revoked": False}
    return connection, account, manifest


def resolve(context, **kwargs):
    return resolve_credentials(*context, authorized=True, **kwargs)


def test_env_reads_only_explicit_reference_and_never_default_environment(credential_context, monkeypatch):
    secret = "synthetic-value-only"
    monkeypatch.setenv("SOURCE_PLUGIN_KEY", secret)
    with pytest.raises(AsterunError, match="显式环境变量来源"):
        resolve(credential_context)
    class ExplicitMapping(Mapping):
        def __getitem__(self, key):
            assert key == "SOURCE_PLUGIN_KEY"
            return secret
        def __iter__(self):
            pytest.fail("不得遍历宿主环境")
        def __len__(self):
            pytest.fail("不得复制宿主环境")
    result = resolve(credential_context, environment=ExplicitMapping())
    assert result.env == {"EXAMPLE_API_KEY": secret}
    assert result.redactions == (secret,)
    assert result.native_home is None
    assert "HOME" not in result.env


@pytest.mark.parametrize("authorized", [False, None, 1, "true"])
def test_authorization_required_before_reading_any_reference(credential_context, monkeypatch, authorized):
    monkeypatch.setattr(os, "open", lambda *a, **k: pytest.fail("未授权不得读文件"))
    with pytest.raises(AsterunError) as captured:
        resolve_credentials(*credential_context, authorized=authorized, environment={"SOURCE_PLUGIN_KEY": "synthetic"})
    assert captured.value.code == "SCOPE_DENIED"


@pytest.mark.parametrize("mutation", [
    lambda connection, account: connection.update(enabled=False),
    lambda connection, account: account.update(revoked=True),
    lambda connection, account: account.update(ref="different-provider"),
    lambda connection, account: connection.update(provider_account_ref=None),
])
def test_disabled_revoked_or_rebound_account_cannot_resolve(credential_context, mutation):
    connection, account, _ = credential_context
    mutation(connection, account)
    with pytest.raises(AsterunError):
        resolve(credential_context, environment={"SOURCE_PLUGIN_KEY": "synthetic"})


@pytest.mark.parametrize("operation", ["execution.observe", "execution.reconcile", "execution.cancel"])
def test_disabled_connection_allows_authorized_existing_operation(credential_context, operation):
    credential_context[0]["enabled"] = False
    result = resolve(credential_context, operation=operation, environment={"SOURCE_PLUGIN_KEY": "synthetic"})
    assert result.env == {"EXAMPLE_API_KEY": "synthetic"}
    assert credential_context[0]["enabled"] is False
    credential_context[1]["revoked"] = True
    with pytest.raises(AsterunError, match="撤销"):
        resolve(credential_context, operation=operation, environment={"SOURCE_PLUGIN_KEY": "synthetic"})


@pytest.mark.parametrize("operation", ["plugin.describe", "connection.inspect", "usage.observe", "capability.prepare", "arbitrary"])
def test_readonly_management_does_not_parse_credentials(credential_context, operation):
    with pytest.raises(AsterunError, match="不得解析"):
        resolve(credential_context, operation=operation, environment={"SOURCE_PLUGIN_KEY": "synthetic"})


def test_none_auth_returns_empty_and_never_inherits_home(credential_context, monkeypatch):
    connection, account, _ = credential_context
    connection["auth_mode"] = "none"
    account["credential_ref"] = None
    monkeypatch.setenv("HOME", "/must-not-inherit")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-inherit")
    result = resolve(credential_context)
    assert result.env == {}
    assert result.redactions == ()
    assert result.native_home is None


def test_none_auth_rejects_secret_injection(credential_context):
    credential_context[0]["auth_mode"] = "none"
    with pytest.raises(AsterunError, match="不接受凭据"):
        resolve(credential_context, environment={"SOURCE_PLUGIN_KEY": "synthetic"})


def test_opaque_reference_is_unsupported_without_fallback(credential_context, monkeypatch):
    credential_context[1]["credential_ref"] = "credential-ref:unconfigured-resolver"
    monkeypatch.setattr(os, "open", lambda *a, **k: pytest.fail("不透明引用不能探测文件"))
    with pytest.raises(AsterunError) as captured:
        resolve(credential_context, environment={"SOURCE_PLUGIN_KEY": "must-not-fallback"})
    assert captured.value.code == "CAPABILITY_UNSUPPORTED"


def private_file(tmp_path, content=b"synthetic-key\n"):
    root = tmp_path.resolve()
    path = root / "credential"
    path.write_bytes(content)
    path.chmod(0o600)
    return path


def test_private_file_injects_single_line_value_only_in_memory(credential_context, tmp_path):
    path = private_file(tmp_path)
    credential_context[1]["credential_ref"] = "file:" + str(path)
    result = resolve(credential_context)
    assert result.env == {"EXAMPLE_API_KEY": "synthetic-key"}
    assert result.redactions == ("synthetic-key",)
    assert path.read_bytes() == b"synthetic-key\n"


@pytest.mark.parametrize("permissions", [0o644, 0o640, 0o666, 0o700])
def test_credentials_file_requires_private_nonexecutable_permissions(credential_context, tmp_path, permissions):
    path = private_file(tmp_path)
    path.chmod(permissions)
    credential_context[1]["credential_ref"] = "file:" + str(path)
    with pytest.raises(AsterunError, match="只允许该用户"):
        resolve(credential_context)


def test_owner_mismatch_rejected(credential_context, tmp_path, monkeypatch):
    path = private_file(tmp_path)
    credential_context[1]["credential_ref"] = "file:" + str(path)
    original_uid = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: original_uid + 1)
    with pytest.raises(AsterunError, match="当前用户"):
        resolve(credential_context)


@pytest.mark.parametrize("ancestor", [False, True])
def test_credentials_reject_leaf_and_ancestor_symlink(credential_context, tmp_path, ancestor):
    root = tmp_path.resolve()
    real = root / "real"
    real.mkdir()
    path = private_file(real)
    link = root / "linked"
    if ancestor:
        link.symlink_to(real, target_is_directory=True)
        path = link / path.name
    else:
        link.symlink_to(path)
        path = link
    credential_context[1]["credential_ref"] = "file:" + str(path)
    with pytest.raises(AsterunError, match="符号链接"):
        resolve(credential_context)


def test_credentials_reject_fifo_without_blocking(credential_context, tmp_path):
    path = tmp_path.resolve() / "fifo"
    os.mkfifo(path, mode=0o600)
    credential_context[1]["credential_ref"] = "file:" + str(path)
    with pytest.raises(AsterunError):
        resolve(credential_context)


@pytest.mark.parametrize("content", [b"", b"two\nlines", b"carriage\rreturn", b"space ", b"\x00null", b"\xff", b"x" * (MAX_SECRET_BYTES + 3)])
def test_credentials_reject_non_utf8_multiline_or_oversized_files(credential_context, tmp_path, content):
    path = private_file(tmp_path, content)
    credential_context[1]["credential_ref"] = "file:" + str(path)
    with pytest.raises(AsterunError):
        resolve(credential_context)


def test_file_changed_during_read_is_not_used(credential_context, tmp_path, monkeypatch):
    path = private_file(tmp_path)
    credential_context[1]["credential_ref"] = "file:" + str(path)
    original_read = os.read
    modified = False
    def changed_read(fd, amount):
        nonlocal modified
        result = original_read(fd, amount)
        if not modified:
            modified = True
            path.write_bytes(b"different-synthetic-key\n")
        return result
    monkeypatch.setattr(os, "read", changed_read)
    with pytest.raises(AsterunError, match="已改变"):
        resolve(credential_context)


def test_native_account_returns_explicit_private_home_without_reading_credentials(credential_context, tmp_path, monkeypatch):
    home = tmp_path.resolve() / "native-home"
    home.mkdir(mode=0o700)
    (home / "credentials.json").write_text("must-not-read-this-synthetic-state")
    connection, account, _ = credential_context
    connection["auth_mode"] = "native_account"
    account["credential_ref"] = "native-home:" + str(home)
    monkeypatch.setattr(os, "read", lambda *a, **k: pytest.fail("不得读取原生会话内容"))
    result = resolve(credential_context)
    assert result.native_home == home
    assert result.env == {}
    assert result.redactions == ()


@pytest.mark.parametrize("reference", ["env:SOURCE_PLUGIN_KEY", "file:/must-not-read", None])
def test_native_account_cannot_fallback_to_api_key_or_options_home(credential_context, reference, tmp_path):
    connection, account, _ = credential_context
    connection.update(auth_mode="native_account", options={"home": str(tmp_path.resolve())})
    account["credential_ref"] = reference
    with pytest.raises(AsterunError):
        resolve(credential_context, environment={"SOURCE_PLUGIN_KEY": "must-not-use"})


def test_api_key_cannot_use_native_home(credential_context, tmp_path):
    credential_context[1]["credential_ref"] = "native-home:" + str(tmp_path.resolve())
    with pytest.raises(AsterunError, match="不能借用"):
        resolve(credential_context)


@pytest.mark.parametrize("env_name", ["HOME", "PATH", "PYTHONPATH", "DYLD_INSERT_LIBRARIES", "LD_PRELOAD", "HTTP_PROXY", "ASTERUN_RUN_CODEX", "lower_case", "KEY=value"])
def test_manifest_cannot_turn_secret_injection_into_process_control(credential_context, env_name):
    credential_context[2]["extensions"]["asterun.credentials"]["env_var"] = env_name
    with pytest.raises(AsterunError, match="控制环境变量"):
        resolve(credential_context, environment={"SOURCE_PLUGIN_KEY": "synthetic"})


def test_env_secret_requires_declared_credential_class(credential_context):
    credential_context[2]["permissions"]["credential_classes"] = []
    with pytest.raises(AsterunError, match="凭据类别"):
        resolve(credential_context, environment={"SOURCE_PLUGIN_KEY": "synthetic"})


def test_oauth_requires_oauth_class(credential_context):
    connection, _, manifest = credential_context
    connection["auth_mode"] = "oauth"
    with pytest.raises(AsterunError, match="凭据类别"):
        resolve(credential_context, environment={"SOURCE_PLUGIN_KEY": "synthetic"})
    manifest["extensions"]["asterun.credentials"]["credential_class"] = "oauth_access_token"
    assert resolve(credential_context, environment={"SOURCE_PLUGIN_KEY": "synthetic"}).env == {"EXAMPLE_API_KEY": "synthetic"}


def test_connection_reference_takes_precedence_without_fallback(credential_context):
    connection, _, _ = credential_context
    connection["credential_ref"] = "env:EXPLICIT_MISSING"
    with pytest.raises(AsterunError):
        resolve(credential_context, environment={"SOURCE_PLUGIN_KEY": "must-not-fallback"})


def test_secret_container_repr_clear_and_serialization_are_safe(credential_context):
    secret = "synthetic-sensitive-value"
    result = resolve(credential_context, environment={"SOURCE_PLUGIN_KEY": secret})
    assert secret not in repr(result)
    with pytest.raises(TypeError):
        json.dumps(result)
    with pytest.raises(TypeError):
        pickle.dumps(result)
    copy = result.env
    copy.clear()
    assert result.env == {"EXAMPLE_API_KEY": secret}
    result.clear()
    assert result.env == {} and result.redactions == () and result.native_home is None


def test_errors_do_not_include_secret_values_or_original_exception(credential_context):
    secret = "synthetic-secret-must-not-appear\nmultiline"
    with pytest.raises(AsterunError) as captured:
        resolve(credential_context, environment={"SOURCE_PLUGIN_KEY": secret})
    assert secret not in json.dumps(captured.value.to_dict())
    rendered = "".join(traceback.format_exception(captured.value))
    assert "synthetic-secret-must-not-appear" not in rendered
