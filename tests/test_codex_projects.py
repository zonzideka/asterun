from __future__ import annotations

import copy
import json
import os
import subprocess
from pathlib import Path

import pytest

from asterun.backends.codex_projects import CodexProjects
from asterun.transport import JsonRpcError


class ProjectsTransport:
    def __init__(self, cwd, *, projects=(), project_id=None):
        self.thread = {"id": "thread-1", "cwd": str(cwd), "projectId": project_id,
                       "environments": [{"environmentId": "local", "cwd": str(cwd),
                                         "runtimeWorkspaceRoots": [str(cwd)]}]}
        self.projects = {p["id"]: copy.deepcopy(p) for p in projects}
        self.calls = []
        self.keys = {}
        self.page_size = 100
        self.hook = None

    def request(self, method, params, *, timeout):
        self.calls.append((method, copy.deepcopy(params), timeout))
        if self.hook:
            response = self.hook(method, params)
            if response is not None:
                return copy.deepcopy(response)
        if method == "thread/read":
            return {"thread": copy.deepcopy(self.thread)}
        if method == "project/list":
            offset = int(params.get("cursor", "0"))
            data = list(self.projects.values())
            end = offset + self.page_size
            return {"data": copy.deepcopy(data[offset:end]),
                    "nextCursor": str(end) if end < len(data) else None}
        if method == "project/create":
            key = params["idempotencyKey"]
            if key not in self.keys:
                project_id = "project-" + str(len(self.projects) + 1)
                self.keys[key] = project_id
                self.projects[project_id] = {"id": project_id, **copy.deepcopy(params)}
            return {"project": copy.deepcopy(self.projects[self.keys[key]])}
        if method == "project/read":
            return {"project": copy.deepcopy(self.projects[params["projectId"]])}
        if method == "thread/metadata/update":
            assert set(params) == {"threadId", "projectId"}
            self.thread["projectId"] = params["projectId"]
            return {"thread": copy.deepcopy(self.thread)}
        pytest.fail("Unexpected RPC: " + method)

    @property
    def methods(self):
        return [method for method, _, _ in self.calls]


def project(root, project_id="existing", *, roots=None):
    return {"id": project_id, "name": "Same name is not identity",
            "roots": [{"path": str(item)} for item in (roots or [root])]}


@pytest.fixture
def fixture(isolated_env):
    cwd = isolated_env / "repository"
    home = isolated_env / "native-home"
    cwd.mkdir()
    home.mkdir()
    return cwd.resolve(), home.resolve()


def present(transport, fixture, **kwargs):
    cwd, home = fixture
    return CodexProjects(transport, cwd, native_home=home, **kwargs).present("thread-1")


def assert_display_only(transport):
    assert set(transport.methods) <= {"thread/read", "project/list", "project/read",
                                      "project/create", "thread/metadata/update"}


def test_create_associate_readback_keeps_execution_context(fixture):
    cwd, _ = fixture
    transport = ProjectsTransport(cwd)
    original = copy.deepcopy(transport.thread)
    result = present(transport, fixture)
    assert result == {"registration": "created", "association": "associated",
                      "client_visibility": "not_verified", "project_id": "project-1",
                      "project_root": str(cwd), "native_cwd": str(cwd),
                      "thread_id": "thread-1", "reason": None}
    assert transport.thread == original | {"projectId": "project-1"}
    assert_display_only(transport)
    assert all(0 < timeout <= 10 for _, _, timeout in transport.calls)
    assert transport.methods[-2:] == ["thread/read", "project/read"]


def test_full_pagination_finds_last_page_without_creating(fixture):
    cwd, home = fixture
    transport = ProjectsTransport(cwd, projects=[project(home, "other"), project(cwd)])
    transport.page_size = 1
    result = present(transport, fixture)
    assert result["registration"] == "existing"
    assert result["project_id"] == "existing"
    assert transport.methods.count("project/list") == 2
    assert "project/create" not in transport.methods


def test_existing_assignment_is_verified_and_preserved(fixture):
    cwd, _ = fixture
    transport = ProjectsTransport(cwd, projects=[project(cwd)], project_id="existing")
    result = present(transport, fixture)
    assert result["association"] == "existing"
    assert "project/read" in transport.methods
    assert "project/create" not in transport.methods
    assert "thread/metadata/update" not in transport.methods


def test_existing_assignment_to_other_root_is_never_overwritten(fixture):
    cwd, home = fixture
    transport = ProjectsTransport(cwd, projects=[project(home)], project_id="existing")
    result = present(transport, fixture)
    assert result["association"] == "conflict"
    assert result["reason"]["code"] == "project_roots_conflict"
    assert transport.thread["projectId"] == "existing"
    assert "thread/metadata/update" not in transport.methods


@pytest.mark.parametrize("assigned", [None, "existing"])
def test_multiroot_project_refused_without_changing_permissions(fixture, assigned):
    cwd, home = fixture
    transport = ProjectsTransport(cwd, projects=[project(cwd, roots=[cwd, home])], project_id=assigned)
    result = present(transport, fixture)
    assert result["reason"]["code"] == "project_roots_conflict"
    assert "thread/metadata/update" not in transport.methods
    assert "project/create" not in transport.methods


def test_same_name_does_not_merge_different_directories(fixture):
    cwd, home = fixture
    transport = ProjectsTransport(cwd, projects=[project(home)])
    result = present(transport, fixture)
    assert result["registration"] == "created"
    assert result["project_id"] != "existing"


def test_symlink_root_matches_canonical_path(fixture):
    cwd, home = fixture
    alias = home / "alias"
    alias.symlink_to(cwd, target_is_directory=True)
    transport = ProjectsTransport(alias, projects=[project(alias)])
    result = present(transport, fixture)
    assert result["registration"] == "existing"
    assert result["native_cwd"] == str(cwd)
    assert "project/create" not in transport.methods


def test_duplicate_roots_are_ambiguous(fixture):
    cwd, _ = fixture
    transport = ProjectsTransport(cwd, projects=[project(cwd), project(cwd, "duplicate")])
    result = present(transport, fixture)
    assert result["reason"]["code"] == "ambiguous_project_roots"
    assert "project/create" not in transport.methods
    assert "thread/metadata/update" not in transport.methods


@pytest.mark.parametrize("change", [{"id": "other"}, {"cwd": "/"}, {"projectId": 42}])
def test_thread_identity_mismatch_precedes_project_mutation(fixture, change):
    transport = ProjectsTransport(fixture[0])
    transport.thread.update(change)
    result = present(transport, fixture)
    assert result["reason"] is not None
    assert transport.methods == ["thread/read"]


def test_method_unsupported_is_reported_without_diagnostic_secrets(fixture):
    transport = ProjectsTransport(fixture[0])
    def hook(method, params):
        if method == "project/list":
            raise JsonRpcError(-32601, "not found private-token")
    transport.hook = hook
    result = present(transport, fixture)
    assert result["registration"] == "unsupported"
    assert result["reason"]["code"] == "native_project_api_unsupported"
    assert "private-token" not in json.dumps(result)
    assert "project/create" not in transport.methods


@pytest.mark.parametrize("mode", ["limit", "repeated", "duplicate_id", "missing_data"])
def test_incomplete_or_invalid_pagination_never_creates(fixture, mode):
    cwd, _ = fixture
    transport = ProjectsTransport(cwd)
    def hook(method, params):
        if method == "project/list":
            if mode == "missing_data":
                return {"nextCursor": None}
            return {"data": [project(cwd)] if mode == "duplicate_id" else [],
                    "nextCursor": (params.get("cursor", "") + "x") if mode == "limit" else "again"}
    transport.hook = hook
    result = present(transport, fixture, max_pages=2)
    assert result["reason"] is not None
    assert "project/create" not in transport.methods
    assert "thread/metadata/update" not in transport.methods


@pytest.mark.parametrize("failure", [TimeoutError, ConnectionError, lambda: JsonRpcError(-32603, "uncertain")])
def test_unknown_create_reconciles_without_second_write(fixture, failure):
    cwd, _ = fixture
    transport = ProjectsTransport(cwd)
    def hook(method, params):
        if method == "project/create":
            transport.projects["committed"] = project(cwd, "committed")
            raise failure()
    transport.hook = hook
    result = present(transport, fixture)
    assert result["registration"] == "reconciled"
    assert result["association"] == "associated"
    assert result["project_id"] == "committed"
    assert transport.methods.count("project/create") == 1


def test_unknown_create_uses_same_key_on_future_retry(fixture):
    cwd, home = fixture
    transport = ProjectsTransport(cwd)
    def hook(method, params):
        if method == "project/create":
            raise TimeoutError()
    transport.hook = hook
    first = present(transport, fixture)
    second = present(transport, fixture)
    keys = [params["idempotencyKey"] for method, params, _ in transport.calls if method == "project/create"]
    assert first["registration"] == second["registration"] == "unknown"
    assert len(keys) == 2 and keys[0] == keys[1]
    assert "thread/metadata/update" not in transport.methods
    other_home = home / "other"
    other_home.mkdir()
    present(transport, (cwd, other_home))
    third_key = [params["idempotencyKey"] for method, params, _ in transport.calls if method == "project/create"][-1]
    assert third_key != keys[0]


def test_unknown_metadata_write_reconciles_without_second_write(fixture):
    cwd, _ = fixture
    transport = ProjectsTransport(cwd, projects=[project(cwd)])
    def hook(method, params):
        if method == "thread/metadata/update":
            transport.thread["projectId"] = params["projectId"]
            raise TimeoutError()
    transport.hook = hook
    result = present(transport, fixture)
    assert result["association"] == "associated"
    assert transport.methods.count("thread/metadata/update") == 1


def test_assignment_change_before_write_is_not_overwritten(fixture):
    cwd, _ = fixture
    transport = ProjectsTransport(cwd, projects=[project(cwd)])
    def hook(method, params):
        if method == "thread/read" and transport.methods.count("thread/read") == 2:
            transport.thread["projectId"] = "someone-elses-project"
    transport.hook = hook
    result = present(transport, fixture)
    assert result["reason"]["code"] == "thread_project_changed"
    assert "thread/metadata/update" not in transport.methods


@pytest.mark.parametrize("key", ["environments", "runtimeWorkspaceRoots", "approvalPolicy", "sandboxPolicy"])
def test_execution_context_change_is_not_reported_as_association_success(fixture, key):
    cwd, _ = fixture
    transport = ProjectsTransport(cwd, projects=[project(cwd)])
    def hook(method, params):
        if method == "thread/metadata/update":
            transport.thread[key] = ["unexpected broader permissions"]
    transport.hook = hook
    result = present(transport, fixture)
    assert result["reason"]["code"] == "thread_execution_context_changed"
    assert result["association"] == "conflict"


def test_post_update_readback_required(fixture):
    cwd, _ = fixture
    transport = ProjectsTransport(cwd, projects=[project(cwd)])
    def hook(method, params):
        if method == "thread/metadata/update":
            return {"thread": transport.thread | {"projectId": "existing"}}
    transport.hook = hook
    result = present(transport, fixture)
    assert result["association"] == "unknown"
    assert result["reason"]["code"] == "thread_association_unconfirmed"


def test_project_changed_after_list_is_not_assigned(fixture):
    cwd, home = fixture
    transport = ProjectsTransport(cwd, projects=[project(cwd)])
    def hook(method, params):
        if method == "project/read":
            transport.projects["existing"]["roots"].append({"path": str(home)})
    transport.hook = hook
    result = present(transport, fixture)
    assert result["reason"]["code"] == "project_roots_conflict"
    assert "thread/metadata/update" not in transport.methods


def test_global_deadline_does_not_reset_for_each_page(fixture, monkeypatch):
    from asterun.backends import codex_projects
    now = [0.0]
    monkeypatch.setattr(codex_projects.time, "monotonic", lambda: now[0])
    transport = ProjectsTransport(fixture[0])
    def hook(method, params):
        if method == "project/list":
            now[0] += 0.6
            return {"data": [], "nextCursor": params.get("cursor", "") + "x"}
    transport.hook = hook
    result = present(transport, fixture, timeout_seconds=1)
    assert result["registration"] == "unknown"
    assert transport.methods.count("project/list") == 2
    assert "project/create" not in transport.methods


@pytest.mark.parametrize("seconds", [0, -1, True, float("inf"), float("nan")])
def test_invalid_deadline_fails_without_requests(fixture, seconds):
    transport = ProjectsTransport(fixture[0])
    result = present(transport, fixture, timeout_seconds=seconds)
    assert result["reason"] is not None
    assert not transport.calls


def git(cwd, *args):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull, "-C", str(cwd), *args],
                   env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def init_repo(cwd):
    git(cwd, "init")
    git(cwd, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "--allow-empty", "-m", "fixture")


def test_linked_worktree_resolves_main_root_without_changing_cwd(fixture):
    cwd, home = fixture
    init_repo(cwd)
    worktree = home / "detached"
    git(cwd, "worktree", "add", "--detach", str(worktree))
    transport = ProjectsTransport(worktree, projects=[project(cwd)])
    result = present(transport, (worktree, home))
    assert result["registration"] == "existing"
    assert result["association"] == "associated"
    assert result["project_root"] == str(cwd)
    assert result["native_cwd"] == str(worktree)
    assert transport.thread["cwd"] == str(worktree)
    assert transport.thread["environments"][0]["runtimeWorkspaceRoots"] == [str(worktree)]


def test_nested_repo_stays_separate_and_subdirectory_uses_true_root(fixture):
    cwd, home = fixture
    init_repo(cwd)
    nested = cwd / "nested"
    nested.mkdir()
    init_repo(nested)
    sub = nested / "src"
    sub.mkdir()
    transport = ProjectsTransport(sub, projects=[project(cwd)])
    result = present(transport, (sub, home))
    assert result["registration"] == "created"
    assert result["project_root"] == str(nested)


def test_same_remote_in_different_clone_does_not_merge(fixture):
    cwd, home = fixture
    init_repo(cwd)
    git(cwd, "remote", "add", "origin", "https://example.invalid/same.git")
    other = home / "same-name"
    other.mkdir()
    init_repo(other)
    git(other, "remote", "add", "origin", "https://example.invalid/same.git")
    transport = ProjectsTransport(cwd, projects=[project(other)])
    result = present(transport, fixture)
    assert result["registration"] == "created"


def test_forged_git_marker_does_not_create_project(fixture):
    cwd, _ = fixture
    (cwd / ".git").write_text("")
    transport = ProjectsTransport(cwd)
    result = present(transport, fixture)
    assert result["reason"]["code"] == "repository_identity_unverified"
    assert "project/create" not in transport.methods


def test_borrowed_worktree_pointer_does_not_claim_parent_project(fixture):
    cwd, home = fixture
    init_repo(cwd)
    worktree = home / "detached"
    git(cwd, "worktree", "add", "--detach", str(worktree))
    forged = home / "forged"
    forged.mkdir()
    (forged / ".git").write_bytes((worktree / ".git").read_bytes())
    transport = ProjectsTransport(forged, projects=[project(cwd)])
    result = present(transport, (forged, home))
    assert result["reason"]["code"] == "worktree_identity_unverified"
    assert "thread/metadata/update" not in transport.methods
    assert "project/create" not in transport.methods


def test_git_environment_cannot_redirect_identity(fixture, monkeypatch):
    cwd, home = fixture
    init_repo(cwd)
    other = home / "other"
    other.mkdir()
    init_repo(other)
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    transport = ProjectsTransport(cwd, projects=[project(other)])
    result = present(transport, fixture)
    assert result["project_root"] == str(cwd)
    assert result["registration"] == "created"


def test_prunable_unrelated_worktree_does_not_hide_current_root(fixture):
    cwd, home = fixture
    init_repo(cwd)
    stale = home / "stale"
    git(cwd, "worktree", "add", "--detach", str(stale))
    (stale / ".git").unlink()
    stale.rmdir()
    transport = ProjectsTransport(cwd, projects=[project(cwd)])
    result = present(transport, fixture)
    assert result["association"] == "associated"
    assert result["project_root"] == str(cwd)


def test_project_is_rechecked_when_another_client_associates_same_project(fixture):
    cwd, home = fixture
    transport = ProjectsTransport(cwd, projects=[project(cwd)])
    def hook(method, params):
        if method == "thread/read" and transport.methods.count("thread/read") == 2:
            transport.thread["projectId"] = "existing"
            transport.projects["existing"]["roots"].append({"path": str(home)})
    transport.hook = hook
    result = present(transport, fixture)
    assert result["reason"]["code"] == "project_roots_conflict"
    assert "thread/metadata/update" not in transport.methods


def desktop_receipt(project_id="ui-native"):
    return {"registration": "registered", "project_id": project_id,
            "desktop_project_id": "ui-legacy", "client_visibility": "not_verified"}


def test_desktop_callback_selects_exact_id_even_with_duplicate_native_roots(fixture):
    cwd, _ = fixture
    transport = ProjectsTransport(cwd, projects=[project(cwd, "orphan"), project(cwd, "ui-native")])
    calls = []
    def prepare(root, remaining_seconds):
        calls.append((root, remaining_seconds, list(transport.methods)))
        return desktop_receipt()
    result = present(transport, fixture, prepare_project=prepare)
    assert result["project_id"] == "ui-native"
    assert result["association"] == "associated"
    assert result["desktop"] == desktop_receipt()
    assert calls[0][0] == cwd and 0 < calls[0][1] <= 10
    assert calls[0][2] == ["thread/read"]
    assert "project/list" not in transport.methods
    assert "project/create" not in transport.methods
    assert all(params["projectId"] == "ui-native" for method, params, _ in transport.calls
               if method in {"project/read", "thread/metadata/update"})


def test_desktop_callback_cannot_migrate_existing_assignment(fixture):
    cwd, _ = fixture
    transport = ProjectsTransport(cwd, projects=[project(cwd, "orphan"), project(cwd, "ui-native")],
                                  project_id="orphan")
    result = present(transport, fixture, prepare_project=lambda *args: desktop_receipt())
    assert result["association"] == "conflict"
    assert result["reason"]["code"] == "thread_project_conflict"
    assert transport.thread["projectId"] == "orphan"
    assert "thread/metadata/update" not in transport.methods
    assert "project/create" not in transport.methods


@pytest.mark.parametrize("status", ["unsupported", "unknown", "conflict", "invalid"])
def test_unconfirmed_desktop_callback_never_falls_back_to_native_create(fixture, status):
    transport = ProjectsTransport(fixture[0])
    receipt = {"registration": status, "reason": {"code": "desktop_unavailable"}}
    result = present(transport, fixture, prepare_project=lambda *args: receipt)
    assert result["registration"] == (status if status != "invalid" else "unknown")
    assert result["desktop"] == receipt
    assert transport.methods == ["thread/read"]


def test_desktop_callback_exception_is_sanitized_without_native_write(fixture):
    transport = ProjectsTransport(fixture[0])
    def prepare(*args):
        raise RuntimeError("secret-native-config")
    result = present(transport, fixture, prepare_project=prepare)
    assert result["reason"]["code"] == "desktop_registration_failed"
    assert "secret-native-config" not in json.dumps(result)
    assert transport.methods == ["thread/read"]


@pytest.mark.parametrize("receipt", [None, {}, {"registration": "registered"},
                                    {"registration": "registered", "project_id": "ui-native"}])
def test_missing_desktop_receipt_never_causes_native_create(fixture, receipt):
    transport = ProjectsTransport(fixture[0])
    result = present(transport, fixture, prepare_project=lambda *args: receipt)
    assert result["reason"] is not None
    assert transport.methods == ["thread/read"]


def test_desktop_native_id_must_have_exact_verified_root(fixture):
    cwd, home = fixture
    transport = ProjectsTransport(cwd, projects=[project(home, "ui-native")])
    result = present(transport, fixture, prepare_project=lambda *args: desktop_receipt())
    assert result["reason"]["code"] == "project_roots_conflict"
    assert "project/create" not in transport.methods
    assert "thread/metadata/update" not in transport.methods


def test_callback_is_not_called_before_thread_identity_is_verified(fixture):
    transport = ProjectsTransport(fixture[0])
    transport.thread["id"] = "another-thread"
    def prepare(*args):
        pytest.fail("Identity must be checked first")
    result = present(transport, fixture, prepare_project=prepare)
    assert result["reason"]["code"] == "thread_identity_mismatch"


def test_callback_consumes_same_total_deadline(fixture, monkeypatch):
    from asterun.backends import codex_projects
    now = [0.0]
    monkeypatch.setattr(codex_projects.time, "monotonic", lambda: now[0])
    transport = ProjectsTransport(fixture[0])
    def prepare(*args):
        now[0] = 11.0
        return desktop_receipt()
    result = present(transport, fixture, prepare_project=prepare)
    assert result["registration"] == "unknown"
    assert result["desktop"] == desktop_receipt()
    assert transport.methods == ["thread/read"]


def test_desktop_timeout_receipt_survives_exhausted_parent_deadline(fixture, monkeypatch):
    from asterun.backends import codex_projects
    now = [0.0]
    monkeypatch.setattr(codex_projects.time, "monotonic", lambda: now[0])
    transport = ProjectsTransport(fixture[0])
    receipt = {"registration": "unknown", "reason": "desktop_registration_unconfirmed"}
    def prepare(*args):
        now[0] = 11.0
        return receipt
    result = present(transport, fixture, prepare_project=prepare)
    assert result["desktop"] == receipt
    assert result["registration"] == "unknown"
    assert result["association"] == "not_attempted"
    assert transport.methods == ["thread/read"]
