from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from asterun.errors import AsterunError
from asterun.review_recipe import prepare_review, submit_review, read_review, _read_manifest
from tests.test_review_recipe import Client, Hub


def git(root, *args):
    return subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", "-C", str(root), *args],
                                   stderr=subprocess.DEVNULL).decode().strip()


@pytest.fixture
def project_review(isolated_env):
    root = isolated_env.resolve()
    project = root / "real-project"
    project.mkdir()
    git(project, "init", "-q")
    git(project, "config", "user.name", "Offline Test")
    git(project, "config", "user.email", "offline@example.invalid")
    git(project, "remote", "add", "origin", "git@github.com:owner/repo.git")
    (project / "code.py").write_text("print('hello')\n")
    git(project, "add", "code.py")
    git(project, "commit", "-qm", "fixture")
    head = git(project, "rev-parse", "HEAD")
    worktree = root / "review-project"
    git(project, "worktree", "add", "--detach", str(worktree), head)
    # 业务 checkout 有用户未提交内容；审查必须不读取/覆盖它。
    (project / "code.py").write_text("user work in progress\n")
    (project / "private-note").write_text("private fixture\n")
    state = root / "state"
    state.mkdir(mode=0o700)
    client = Client(worktree)
    client.config["workspaces"]["project"] = {"root": str(project)}
    hub = Hub()
    hub.head = head
    return client, state, hub, project, worktree


def prepare(fixture, **kwargs):
    client, state, hub, _, _ = fixture
    return prepare_review(client, state, workspace="review", backend="codex", repo="owner/repo", pr=55,
                          attempt="one", project_workspace="project", github=hub, **kwargs)


def test_explicit_project_worktree_binding_preserves_dirty_business_checkout(project_review):
    client, state, hub, project, root = project_review
    before = git(project, "status", "--porcelain=v1")
    result = prepare(project_review)
    context = result["workspace_context"]
    assert context["mode"] == "linked_worktree"
    assert context["native_cwd"] == str(root)
    assert context["project_binding"]["project_root"] == str(project)
    assert context["project_binding"]["git_common_dir"] == str(project / ".git")
    assert prepare(project_review) == result
    assert read_review(client, state, result["id"])["review"]["workspace_context"] == context
    submitted = submit_review(client, state, result["id"], github=hub)
    assert submitted["task"]["id"] == "tsk_test"
    payload = next(p for m, p in client.calls if m == "task.submit")
    assert payload["workspace"] == "review"  # 核心及原生执行使用独立 worktree。
    assert "审查项目绑定：" in payload["text"]
    assert git(project, "status", "--porcelain=v1") == before
    assert (project / "code.py").read_text() == "user work in progress\n"
    assert (root / "code.py").read_text() == "print('hello')\n"


@pytest.mark.parametrize("problem", ["same_root", "main_checkout", "foreign_project", "wrong_origin",
                                    "wrong_head", "branch", "dirty", "untracked", "ignored"])
def test_wrong_project_or_mutable_worktree_rejected(project_review, problem):
    client, state, hub, project, root = project_review
    if problem == "same_root":
        client.config["workspaces"]["review"]["root"] = str(project)
    elif problem == "main_checkout":
        client.config["workspaces"]["project"]["root"] = str(root)
        client.config["workspaces"]["review"]["root"] = str(project)
    elif problem == "foreign_project":
        foreign = root.parent / "unrelated"
        foreign.mkdir()
        git(foreign, "init", "-q")
        client.config["workspaces"]["project"]["root"] = str(foreign)
    elif problem == "wrong_origin":
        git(project, "remote", "set-url", "origin", "https://github.com/other/repo.git")
    elif problem == "wrong_head":
        hub.head = "c" * 40
    elif problem == "branch":
        git(root, "switch", "-c", "review-branch")
    elif problem == "dirty":
        (root / "code.py").write_text("dirty")
    elif problem == "untracked":
        (root / "extra").write_text("dirty")
    elif problem == "ignored":
        (project / ".git/info/exclude").write_text("cache/\n")
        (root / "cache").mkdir()
        (root / "cache/data").write_text("dirty")
    with pytest.raises(AsterunError):
        prepare(project_review)
    assert not client.results
    assert not (root / ".asterun-reviews").exists()


def test_prepared_binding_rechecked_before_dispatch_and_in_manifest(project_review):
    client, state, hub, project, root = project_review
    result = prepare(project_review)
    (root / "code.py").write_text("changed after prepare")
    with pytest.raises(AsterunError, match="干净"):
        submit_review(client, state, result["id"], github=hub)
    assert not client.results
    path = state / "reviews" / result["id"] / "manifest.json"
    value = json.loads(path.read_text())
    assert not value.get("submission_attempted")
    value["project_binding"]["project_root"] = str(root.parent / "forged")
    path.write_text(json.dumps(value))
    with pytest.raises(AsterunError, match="原任务输入"):
        _read_manifest(path)


def test_legacy_standalone_routing_is_explicit_and_still_supported(tmp_path):
    root = tmp_path / "review"
    root.mkdir()
    client = Client(root)
    result = prepare_review(client, tmp_path / "state", workspace="review", backend="codex", repo="owner/repo",
                            pr=55, attempt="one", github=Hub())
    context = result["workspace_context"]
    assert context["mode"] == "standalone_snapshot"
    assert context["native_cwd"] == str(root)
    assert context["project_binding"] is None
    assert context["native_project_visibility"] == "not_verified_by_recipe"


def test_project_root_rebind_and_missing_opt_in_rejected(project_review):
    client, state, hub, project, root = project_review
    result = prepare(project_review)
    with pytest.raises(AsterunError, match="project-workspace"):
        prepare_review(client, state, workspace="review", backend="codex", repo="owner/repo", pr=55,
                       attempt="one", github=hub)
    client.config["workspaces"]["project"]["root"] = str(root)
    with pytest.raises(AsterunError):
        submit_review(client, state, result["id"], github=hub)
    assert not client.results


def test_worktree_native_process_records_real_linkage_and_same_cwd_on_reconcile(project_review, monkeypatch):
    from asterun.application import Application
    from tests.conftest import write_config
    from tests.test_native_lifecycle import wait_for
    client, state, hub, project, root = project_review
    binary = state.parent / "codex-stub"
    binary.write_text(f"#!{sys.executable}\n" + (Path(__file__).parent / "fixtures/codex_server.py").read_text())
    binary.chmod(0o700)
    monkeypatch.setenv("ASTERUN_RUN_CODEX", "1")
    config = write_config(state.parent / "config.json", root,
        extra_backends={"codex": {"kind": "codex", "bin": str(binary)}},
        extra={"workspaces": client.config["workspaces"]})
    app = Application.from_paths(config, state, background=True)
    try:
        review = prepare_review(app, state, workspace="review", project_workspace="project", backend="codex",
                                repo="owner/repo", pr=55, attempt="native", github=hub, read_mode="manual")
        submitted = submit_review(app, state, review["id"], github=hub)
        completed = wait_for(app, submitted["task"]["id"], lambda row: row["run"]["status"] == "succeeded")
        native = completed["run"]["native"]
        backend = app.backends["codex"]
        transport = backend.runtime._connect(root)
        try:
            thread = backend.runtime._read(transport, native, root)
            assert thread["cwd"] == str(root)
            with pytest.raises(AsterunError, match="不匹配"):
                backend.runtime._read(transport, native, project)
        finally:
            transport.close()
        assert git(root, "rev-parse", "--git-common-dir") == str(project / ".git")
        assert backend.reconcile_native(native, root)["summary"] == completed["run"]["summary"]
        assert (project / "code.py").read_text() == "user work in progress\n"
    finally:
        app.close()


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_index_flags_cannot_hide_dirty_review_files(project_review, flag):
    root = project_review[-1]
    git(root, "update-index", flag, "code.py")
    (root / "code.py").write_text("hidden dirty file")
    assert git(root, "status", "--porcelain=v1") == ""
    with pytest.raises(AsterunError, match="隐藏变更"):
        prepare(project_review)


@pytest.mark.parametrize("url", ["https://github.com/owner/repo.git", "ssh://git@github.com/owner/repo.git"])
def test_canonical_origin_formats_and_inherited_git_env(project_review, monkeypatch, url):
    project = project_review[-2]
    git(project, "remote", "set-url", "origin", url)
    monkeypatch.setenv("GIT_DIR", str(project.parent / "not-the-project"))
    monkeypatch.setenv("GIT_WORK_TREE", str(project.parent))
    assert prepare(project_review)["workspace_context"]["project_binding"]["project_root"] == str(project)


@pytest.mark.parametrize("kind", ["symlink", "submodule", "reserved"])
def test_unsupported_checkout_content_rejected(project_review, kind):
    client, state, hub, project, root = project_review
    if kind == "symlink":
        (root / "link").symlink_to(project / "private-note")
        git(root, "add", "link")
    elif kind == "submodule":
        git(root, "update-index", "--add", "--cacheinfo", "160000", hub.head, "submodule")
    else:
        (root / ".asterun-reviews").mkdir()
        (root / ".asterun-reviews/poison").write_text("tracked reserved content")
        git(root, "add", ".asterun-reviews")
    git(root, "commit", "-qm", "fixture incompatible content")
    hub.head = git(root, "rev-parse", "HEAD")
    with pytest.raises(AsterunError, match="符号链接|子模块|保留目录"):
        prepare(project_review)
    assert not client.results
