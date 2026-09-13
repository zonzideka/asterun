from pathlib import Path
import json

import pytest

from asterun.cli import main, build_parser


def test_discovery_cli_without_starting_runtime(isolated_env, capsys):
    assert main(["codex-discover", "--applications-dir", str(isolated_env / "apps")]) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["model_called"] is False
    assert not (isolated_env / "asterun-home" / "state").exists()


@pytest.mark.parametrize("arguments", [["--connect", "service-plan", "--plist", "/unused", "--plan-dir", "/unused", "--codex-bin", "/unused"],
                                       ["review-submit", "rev_" + "a" * 32],
                                       ["--connect", "review-publish", "rev_" + "a" * 32, "--plan-sha256", "x"]])
def test_management_and_core_entry_separation(isolated_env, capsys, arguments):
    assert main(arguments) != 0
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert not (isolated_env / "asterun-home" / "state").exists()


def test_service_apply_passes_exact_plan_hash(isolated_env, monkeypatch, capsys):
    calls = []
    def apply(path, **kwargs):
        calls.append((path, kwargs))
        return {"status": "applied"}
    monkeypatch.setattr("asterun.service_management.apply_service_plan", apply)
    assert main(["service-apply", "/reviewed/plan.json", "--plan-sha256", "a" * 64]) == 0
    assert calls == [(Path("/reviewed/plan.json"), {"expected_plan_sha256": "a" * 64, "verify_connection": False})]
    assert json.loads(capsys.readouterr().out)["data"]["status"] == "applied"


@pytest.mark.parametrize("command", ["approval-assess", "approval-apply-policy"])
def test_policy_cli_requires_all_bindings(command):
    with pytest.raises(SystemExit):
        build_parser().parse_args([command, "ap_test"])


def test_approval_batch_cli_shared_contract(config_path, capsys):
    request = config_path.parent / "request.json"
    request.write_text(json.dumps({"items": []}))
    assert main(["--config", str(config_path), "--state-dir", str(config_path.parent / "state"),
                 "approval-batch", "--request", str(request)]) != 0
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_batch_partial_results_exit_nonzero(config_path, capsys):
    request = config_path.parent / "batch.json"
    request.write_text(json.dumps({"items": [{"approval_id": "apr_missing", "expected_task_id": "tsk_missing",
        "expected_run_id": "run_missing", "expected_target_hash": "hash", "decision": "approve"}]}))
    assert main(["--config", str(config_path), "--state-dir", str(config_path.parent / "state"),
                 "approval-batch", "--request", str(request)]) == 1
    reply = json.loads(capsys.readouterr().out)
    assert reply["ok"] and not reply["data"]["all_succeeded"]


@pytest.mark.parametrize("command", ["review-submit", "review-status", "review-publish-plan", "review-publish"])
@pytest.mark.parametrize("identifier", ["../outside", "/absolute", "rev_" + "a" * 32 + "/../../outside"])
def test_review_id_rejected_before_creating_state(isolated_env, capsys, command, identifier):
    state = isolated_env / "uncreated"
    args = ["--state-dir", str(state)] + ([] if command == "review-publish" else ["--connect"])
    args += [command, identifier]
    if command == "review-publish":
        args += ["--plan-sha256", "a" * 64]
    assert main(args) == 1
    output = capsys.readouterr()
    assert not output.err
    assert json.loads(output.out)["error"]["code"] == "INVALID_REQUEST"
    assert not state.exists()


@pytest.mark.parametrize("raw", [b"[]", b"{}", b"null", b'{"schema":', b'{"schema":1,"schema":2}', b'"\\ud800"'])
def test_corrupt_manifest_returns_json_without_traceback(isolated_env, capsys, raw):
    identifier = "rev_" + "a" * 32
    state = isolated_env / "state"
    directory = state / "reviews" / identifier
    directory.mkdir(parents=True, mode=0o700)
    directory.parent.chmod(0o700)
    record = directory / "manifest.json"
    record.write_bytes(raw)
    record.chmod(0o600)
    assert main(["--state-dir", str(state), "--connect", "review-status", identifier]) == 1
    output = capsys.readouterr()
    assert not output.err
    assert json.loads(output.out)["error"]["code"] == "INVALID_REQUEST"


def test_invalid_external_shape_is_sanitized_and_client_is_closed(isolated_env, monkeypatch, capsys):
    closed = []
    class Client:
        def __init__(self, *args):
            pass
        def handle(self, method, payload):
            return {"ok": True, "data": {"config": None, "secret": "SENTINEL_PRIVATE_RESPONSE"}}
        def close(self):
            closed.append(True)
    monkeypatch.setattr("asterun.service.LocalClient", Client)
    assert main(["--connect", "review-prepare", "--workspace", "review", "--backend", "codex",
                 "--repo", "owner/repo", "--pr", "1", "--attempt", "one"]) == 1
    output = capsys.readouterr()
    assert not output.err
    assert "SENTINEL_PRIVATE_RESPONSE" not in output.out
    assert json.loads(output.out)["error"]["code"] == "INVALID_REQUEST"
    assert closed == [True]
