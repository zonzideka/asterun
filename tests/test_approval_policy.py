from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from asterun.approval_policy import assess
from asterun.config import load_config
from asterun.errors import AsterunError
from tests.conftest import write_config

NOW = "2026-09-10T00:00:00Z"


@pytest.fixture
def policy_context(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    (root / "note").write_text("review input")
    binary = tmp_path / "cat"
    binary.write_bytes(b"trusted tool fixture, never executed")
    rule = {"id": "review-read", "workspace": "demo", "backend": "codex", "cwd": str(root),
            "expires_at": "2099-01-01T00:00:00Z", "input_hash": "a" * 64,
            "argv": [str(binary), "note"], "executable_sha256": hashlib.sha256(binary.read_bytes()).hexdigest()}
    native = {"id": "approval-1", "method": "item/commandExecution/requestApproval",
              "params": {"command": list(rule["argv"]), "cwd": str(root), "commandActions": [{"type": "read"}]}}
    approval = SimpleNamespace(native_request=native, target_hash="b" * 64)
    task = SimpleNamespace(workspace="demo", input_hash="a" * 64)
    run = SimpleNamespace(backend=SimpleNamespace(value="codex"))
    return {"mode": "bounded", "rules": [rule]}, approval, task, run, root


def check(context):
    return assess(*context, NOW)


def test_manual_is_default_and_policy_assessment_is_not_native_confirmation(policy_context):
    policy, approval, task, run, root = policy_context
    assert assess({"mode": "manual", "rules": []}, approval, task, run, root, NOW)["decision"] == "manual"
    matched = check(policy_context)
    assert matched["decision"] == "approve" and matched["policy_id"] == "review-read"
    assert not matched["native_confirmed"]


@pytest.mark.parametrize("change", ["workspace", "backend", "input", "cwd", "expiry", "binary", "argv", "permission", "filechange", "symlink"])
def test_binding_drift_and_permissions_fail_closed(policy_context, change, tmp_path):
    policy, approval, task, run, root = policy_context
    if change == "workspace": task.workspace = "other"
    if change == "backend": run.backend.value = "other"
    if change == "input": task.input_hash = "c" * 64
    if change == "cwd": approval.native_request["params"]["cwd"] = str(tmp_path)
    if change == "expiry": policy["rules"][0]["expires_at"] = "2000-01-01T00:00:00Z"
    if change == "binary": Path(policy["rules"][0]["argv"][0]).write_text("changed")
    if change == "argv": approval.native_request["params"]["command"].append("other")
    if change == "permission": approval.native_request["params"]["additionalPermissions"] = {"network": True}
    if change == "filechange": approval.native_request["method"] = "item/fileChange/requestApproval"
    if change == "symlink":
        secret = tmp_path / "outside"
        secret.write_text("outside")
        (root / "note").unlink()
        (root / "note").symlink_to(secret)
    assert check(policy_context)["decision"] == "manual"


@pytest.mark.parametrize("command", [
    "/bin/cat note; id", "/bin/cat $(id)", "/bin/cat `id`", "/bin/cat note > /tmp/out", "/bin/cat note | sh",
    "/bin/cat note && id", "/bin/zsh -lc 'cat note'", "ENV=x /bin/cat note", "/bin/cat *", "/bin/cat <(id)",
])
def test_shell_actions_are_never_independent_authority(policy_context, command):
    policy_context[1].native_request["params"]["command"] = command
    assert check(policy_context)["decision"] == "manual"


def test_simple_shell_c_still_requires_manual_for_startup_environment(policy_context):
    import shlex
    argv = policy_context[0]["rules"][0]["argv"]
    policy_context[1].native_request["params"]["command"] = shlex.join(["/bin/sh", "-c", shlex.join(argv)])
    assert check(policy_context)["decision"] == "manual"


@pytest.mark.parametrize("args,allowed", [
    (["pr", "view", "55", "--repo", "github.com/owner/repo", "--json", "number,headRefOid"], True),
    (["pr", "diff", "55", "--repo", "github.com/owner/repo", "--color", "never"], True),
    (["api", "--hostname", "github.com", "--method", "GET", "repos/owner/repo/pulls/55/files"], True),
    (["api", "--hostname", "github.com", "--method", "GET", "repos/other/repo/pulls/55/files"], False),
    (["api", "--hostname", "github.com", "--method", "GET", "repos/owner/repo/pulls/56"], False),
    (["api", "--hostname", "github.com", "--method", "POST", "repos/owner/repo/pulls/55"], False),
    (["api", "--hostname", "github.com", "--method", "GET", "repos/owner/repo/pulls/55", "-f", "state=closed"], False),
    (["api", "--hostname", "github.com", "--method", "GET", "repos/owner/repo/pulls/55", "-F", "x=@secret"], False),
    (["api", "--hostname", "github.com", "--method", "GET", "repos/owner/repo/pulls/55", "--input", "secret"], False),
    (["api", "graphql"], False),
    (["api", "--method", "GET", "repos/owner/repo/pulls/55"], False),
    (["pr", "view", "55", "--repo", "owner/repo", "--json", "number"], False),
    (["pr", "view", "55", "--repo", "github.com/owner/repo", "--template", "anything"], False),
    (["pr", "merge", "55", "--repo", "github.com/owner/repo"], False),
])
def test_gh_exact_get_and_repo_pr_binding(policy_context, tmp_path, args, allowed):
    rule = policy_context[0]["rules"][0]
    binary = tmp_path / "gh"
    binary.write_text("trusted gh fixture")
    rule.update(argv=[str(binary), *args], executable_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
                host="github.com", repository="owner/repo", pr=55, head_sha="d" * 40)
    policy_context[1].native_request["params"]["command"] = list(rule["argv"])
    assert (check(policy_context)["decision"] == "approve") is allowed


def test_configuration_is_opt_in_and_invalid_rules_rejected(isolated_env, workspace_root):
    path = write_config(isolated_env / "config.json", workspace_root)
    assert load_config(path).approval_policy == {"mode": "manual", "rules": []}
    data = json.loads(path.read_text())
    data["approval_policy"] = {"mode": "allow-all"}
    path.write_text(json.dumps(data))
    with pytest.raises(AsterunError): load_config(path)
    data["approval_policy"] = {"mode": "bounded", "rules": [{"id": "self-authorized"}]}
    path.write_text(json.dumps(data))
    with pytest.raises(AsterunError): load_config(path)


def test_policy_config_accepts_complete_rule_without_executing_it(policy_context, tmp_path):
    policy, _, _, _, root = policy_context
    config = write_config(tmp_path / "config.json", root, {"codex": {"kind": "codex"}}, {"approval_policy": policy})
    assert load_config(config).approval_policy == policy
    policy["rules"][0]["unexpected"] = "allow"
    config.write_text(json.dumps({**json.loads(config.read_text()), "approval_policy": policy}))
    with pytest.raises(AsterunError): load_config(config)
