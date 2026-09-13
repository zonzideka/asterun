from __future__ import annotations

import io
import json
from pathlib import Path
import tarfile
import sys

import pytest

from asterun.errors import AsterunError, REMOTE_STATE_UNKNOWN
from asterun.review_recipe import (prepare_review, submit_review, prepare_publication,
                                  publish_review, read_review, extract_snapshot, _read)

HEAD = "a" * 40
BASE = "b" * 40


def archive(name="root/code.py", *, link=None):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as tar:
        row = tarfile.TarInfo(name)
        content = b"print('hello')\n"
        if link:
            row.type = tarfile.SYMTYPE
            row.linkname = link
            tar.addfile(row)
        else:
            row.size = len(content)
            tar.addfile(row, io.BytesIO(content))
    return output.getvalue()


class Hub:
    def __init__(self):
        self.head = HEAD
        self.base = BASE
        self.posts = []
        self.reviews = []
        self.actor = "reviewer"
        self.timeout = False

    def pull(self, repo, pr):
        assert repo == "owner/repo" and pr == 55
        return {"number": pr, "head": {"sha": self.head}, "base": {"sha": self.base},
                "user": {"login": "owner"}, "state": "open"}

    def api(self, endpoint, *, method="GET", data=None, accept=None, pages=False):
        if endpoint == "user":
            return {"login": self.actor}
        if "/tarball/" in endpoint:
            return archive()
        if "/compare/" in endpoint:
            return b"diff --git a/code.py b/code.py\n"
        if pages:
            return [self.reviews]
        assert method == "POST"
        self.posts.append(data)
        if self.timeout:
            raise AsterunError(REMOTE_STATE_UNKNOWN, "test timeout")
        result = {"id": 100, "html_url": "https://github.com/owner/repo/pull/55#review100",
                  "state": {"COMMENT": "COMMENTED", "REQUEST_CHANGES": "CHANGES_REQUESTED", "APPROVE": "APPROVED"}[data["event"]],
                  "commit_id": data["commit_id"], "body": data["body"], "user": {"login": self.actor}}
        self.reviews.append(result)
        return result


class Client:
    def __init__(self, root):
        self.config = {"revision": 1, "workspaces": {"review": {"root": str(root)}},
                       "backends": {"codex": {"enabled": True}}}
        self.calls = []
        self.results = {}
        self.reply_lost = False
        self.input_hash = None
        self.summary = ""

    def handle(self, method, payload):
        self.calls.append((method, payload))
        if method == "config.validate":
            return {"ok": True, "data": {"config": self.config, "config_apply_required": False}}
        if method == "task.submit":
            key = payload["idempotency_key"]
            self.results.setdefault(key, {"task": {"id": "tsk_test"}, "run": {"id": "run_test"}})
            if self.reply_lost:
                self.reply_lost = False
                return {"ok": False, "error": {"code": "BACKEND_UNAVAILABLE"}}
            return {"ok": True, "data": self.results[key]}
        assert method == "task.get"
        return {"ok": True, "data": {"task": {"id": "tsk_test", "input_hash": self.input_hash},
                "run": {"id": "run_test", "status": "succeeded", "terminated": True, "summary": self.summary}}}


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    return Client(root), state, Hub()


def prepare(setup, attempt="one"):
    client, state, hub = setup
    return prepare_review(client, state, workspace="review", backend="codex", repo="owner/repo", pr=55,
                          attempt=attempt, github=hub)


def complete(setup):
    client, state, hub = setup
    result = prepare(setup)
    submit_review(client, state, result["id"], github=hub)
    manifest = _read(state / "reviews" / result["id"] / "manifest.json")
    client.input_hash = result["task_input_hash"]
    client.summary = json.dumps({"target_hash": manifest["target"]["hash"], "findings": [
        {"summary": "[P1] 代码问题", "path": "code.py", "line": 1}]})
    return result, prepare_publication(client, state, result["id"])


def test_prepare_reuses_snapshot_and_does_not_submit(setup):
    first = prepare(setup)
    assert prepare(setup) == first
    assert not any(method == "task.submit" for method, _ in setup[0].calls)
    assert first["standard_control_budget"] == "not_applicable"
    assert (Path(first["snapshot_path"]) / "code.py").read_text() == "print('hello')\n"
    assert read_review(setup[0], setup[1], first["id"])["submission_status"] == "not_submitted"


def test_reconnect_reuses_original_idempotency_key(setup):
    client, state, hub = setup
    result = prepare(setup)
    client.reply_lost = True
    with pytest.raises(AsterunError):
        submit_review(client, state, result["id"], github=hub)
    assert read_review(client, state, result["id"])["submission_status"] == "unknown"
    submit_review(client, state, result["id"], github=hub)
    assert len(client.results) == 1
    count = len([row for row in client.calls if row[0] == "task.submit"])
    submit_review(client, state, result["id"], github=hub)
    assert len([row for row in client.calls if row[0] == "task.submit"]) == count


@pytest.mark.parametrize("name,link", [("../../escape", None), ("root/../../escape", None),
                                      ("/root/escape", None), ("root/link", "/etc/passwd")])
def test_archive_rejects_escaping_and_links(tmp_path, name, link):
    with pytest.raises(AsterunError):
        extract_snapshot(archive(name, link=link), tmp_path / "target")
    assert not (tmp_path / "escape").exists()


def test_business_workspace_rejected(setup):
    root = Path(setup[0].config["workspaces"]["review"]["root"])
    (root / "business.txt").write_text("untouched")
    with pytest.raises(AsterunError):
        prepare(setup)
    assert (root / "business.txt").read_text() == "untouched"


def test_head_or_snapshot_changed_cannot_submit(setup):
    result = prepare(setup)
    setup[2].head = "c" * 40
    with pytest.raises(AsterunError):
        submit_review(setup[0], setup[1], result["id"], github=setup[2])
    setup[2].head = HEAD
    snapshot = Path(result["snapshot_path"]) / "code.py"
    snapshot.chmod(0o600)
    snapshot.write_text("modified")
    with pytest.raises(AsterunError):
        submit_review(setup[0], setup[1], result["id"], github=setup[2])
    assert not setup[0].results


def test_config_rebind_only_before_submission(setup):
    result = prepare(setup)
    setup[0].config["revision"] = 2
    again = prepare(setup)
    assert again["id"] == result["id"] and again["config_revision"] == 2
    assert again["task_input_hash"] == result["task_input_hash"]
    submit_review(setup[0], setup[1], result["id"], github=setup[2])
    setup[0].config["revision"] = 3
    with pytest.raises(AsterunError):
        prepare(setup)


def test_publication_requires_exact_approved_plan(setup):
    result, plan = complete(setup)
    with pytest.raises(AsterunError):
        publish_review(setup[1], result["id"], approved_plan_sha256="wrong", github=setup[2])
    assert setup[2].posts == []
    published = publish_review(setup[1], result["id"], approved_plan_sha256=plan["sha256"], github=setup[2])
    assert published["state"] == "CHANGES_REQUESTED"
    replay = publish_review(setup[1], result["id"], approved_plan_sha256=plan["sha256"], github=setup[2])
    assert replay["replayed"] and len(setup[2].posts) == 1


def test_self_review_maps_platform_state_preserving_gate(setup):
    result, plan = complete(setup)
    setup[2].actor = "owner"
    published = publish_review(setup[1], result["id"], approved_plan_sha256=plan["sha256"], github=setup[2])
    assert published["state"] == "COMMENTED" and published["gate_verdict"] == "REQUEST_CHANGES"


def test_unknown_write_never_reposts(setup):
    result, plan = complete(setup)
    setup[2].timeout = True
    with pytest.raises(AsterunError):
        publish_review(setup[1], result["id"], approved_plan_sha256=plan["sha256"], github=setup[2])
    setup[2].timeout = False
    with pytest.raises(AsterunError):
        publish_review(setup[1], result["id"], approved_plan_sha256=plan["sha256"], github=setup[2])
    assert len(setup[2].posts) == 1


def test_invalid_model_output_is_not_approval(setup):
    result, _ = complete(setup)
    setup[0].summary = "Looks good, approve!"
    with pytest.raises(AsterunError):
        prepare_publication(setup[0], setup[1], result["id"])


def test_pax_metadata_included_in_expansion_limit(tmp_path, monkeypatch):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        row = tarfile.TarInfo("root/file")
        row.size = 1
        row.pax_headers = {"comment": "x" * 65536}
        tar.addfile(row, io.BytesIO(b"x"))
    monkeypatch.setattr("asterun.review_recipe.MAX_EXPANDED", 32768)
    with pytest.raises(AsterunError, match="元数据"):
        extract_snapshot(output.getvalue(), tmp_path / "pax")


def test_download_limit_applies_during_read(monkeypatch):
    from asterun.review_recipe import _bounded_run
    monkeypatch.setattr("asterun.review_recipe.MAX_ARCHIVE", 128)
    with pytest.raises(AsterunError, match="读取上限"):
        _bounded_run([sys.executable, "-c", "import sys;sys.stdout.write('x'*1024)"])


def test_base_retarget_rejects_old_review(setup):
    result, plan = complete(setup)
    setup[2].base = "c" * 40
    with pytest.raises(AsterunError, match="head/base"):
        publish_review(setup[1], result["id"], approved_plan_sha256=plan["sha256"], github=setup[2])
    assert not setup[2].posts


def test_diff_download_failure_allows_same_attempt_retry(setup, monkeypatch):
    original = setup[2].api
    def fail_diff(endpoint, **kwargs):
        if "/compare/" in endpoint:
            raise AsterunError(REMOTE_STATE_UNKNOWN, "download timeout")
        return original(endpoint, **kwargs)
    monkeypatch.setattr(setup[2], "api", fail_diff)
    with pytest.raises(AsterunError):
        prepare(setup)
    monkeypatch.setattr(setup[2], "api", original)
    assert prepare(setup)["task_id"] is None
    assert not setup[0].results


@pytest.mark.parametrize("field", ["snapshot_path", "diff_path"])
def test_manifest_cannot_rebind_snapshot_paths(setup, tmp_path, field):
    result = prepare(setup)
    path = setup[1] / "reviews" / result["id"] / "manifest.json"
    value = _read(path)
    value[field] = str(tmp_path / "outside")
    path.write_text(json.dumps(value))
    with pytest.raises(AsterunError, match="脱离固定工作区"):
        submit_review(setup[0], setup[1], result["id"], github=setup[2])
    assert not setup[0].results


def test_corrupt_manifest_identity_cannot_retarget_publication(setup):
    result, plan = complete(setup)
    path = setup[1] / "reviews" / result["id"] / "manifest.json"
    value = _read(path)
    value["identity"]["pr"] = 56
    path.write_text(json.dumps(value))
    with pytest.raises(AsterunError, match="身份摘要"):
        publish_review(setup[1], result["id"], approved_plan_sha256=plan["sha256"], github=setup[2])
    assert not setup[2].posts


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b'\xff'])
def test_private_record_rejects_ambiguous_json(tmp_path, raw):
    path = tmp_path / "record.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    with pytest.raises(AsterunError):
        _read(path)


def test_core_timeout_preserves_attempt_without_retry(setup, monkeypatch):
    result = prepare(setup)
    original = setup[0].handle
    submits = []
    def handle(method, payload):
        if method == "task.submit":
            submits.append(payload["idempotency_key"])
            raise TimeoutError("SENTINEL_PRIVATE_RESPONSE")
        return original(method, payload)
    monkeypatch.setattr(setup[0], "handle", handle)
    with pytest.raises(AsterunError) as error:
        submit_review(setup[0], setup[1], result["id"], github=setup[2])
    assert error.value.code == REMOTE_STATE_UNKNOWN
    assert "SENTINEL_PRIVATE_RESPONSE" not in str(error.value)
    assert submits == [result["idempotency_key"]]
    assert read_review(setup[0], setup[1], result["id"])["submission_status"] == "unknown"


def test_malformed_write_reply_is_unknown_and_never_reposted(setup, monkeypatch):
    result, plan = complete(setup)
    original = setup[2].api
    posts = []
    def api(endpoint, **kwargs):
        if kwargs.get("method") == "POST":
            posts.append(endpoint)
            return ["unexpected"]
        return original(endpoint, **kwargs)
    monkeypatch.setattr(setup[2], "api", api)
    for _ in range(2):
        with pytest.raises(AsterunError):
            publish_review(setup[1], result["id"], approved_plan_sha256=plan["sha256"], github=setup[2])
    assert len(posts) == 1


def test_truncated_gzip_is_a_structured_failure(tmp_path):
    with pytest.raises(AsterunError):
        extract_snapshot(archive()[:12], tmp_path / "source")
