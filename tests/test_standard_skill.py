from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile

import pytest

from asterun.service import LocalClient

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "integrations/skills/asterun"


def load_script(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


skill = load_script(SOURCE / "scripts/run.py", "asterun_standard_skill")
packager = load_script(ROOT / "scripts/package-standard-skill.py", "asterun_skill_packager")


@pytest.fixture
def resident(config_path, isolated_env):
    # UNIX socket 的路径长度受系统限制，状态目录独立于 pytest 的深层目录。
    with tempfile.TemporaryDirectory(prefix="asterun-skill-") as directory:
        state = Path(directory) / "state"
        command = [sys.executable, "-m", "asterun", "--config", str(config_path), "--state-dir", str(state)]
        process = subprocess.Popen([*command, "serve"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        client = LocalClient(state / "asterun.sock")
        deadline = time.monotonic() + 5
        while not client.path.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert client.path.exists(), process.communicate(timeout=1) if process.poll() is not None else "socket missing"
        executable = isolated_env / "asterun-cli"
        executable.write_text(f"#!{sys.executable}\nfrom asterun.cli import main\nraise SystemExit(main())\n")
        executable.chmod(0o700)
        # 在仓库外运行独立复制的 skill，防止依赖技能源码导入路径。
        installed = isolated_env / "copied-skill"
        shutil.copytree(SOURCE, installed, ignore=shutil.ignore_patterns("__pycache__"))
        common = ["--asterun-bin", str(executable), "--state-dir", str(state),
                  "--artifacts-dir", str(isolated_env / "calls")]

        def call(*args):
            result = subprocess.run([sys.executable, str(installed / "scripts/run.py"), *common, *args],
                                    cwd=isolated_env, capture_output=True, timeout=8)
            assert len(result.stdout) <= skill.MAX_REPLY_BYTES
            data = json.loads(result.stdout)
            return data, result.returncode

        try:
            yield call, client, state
        finally:
            process.terminate()
            out, err = process.communicate(timeout=5)
            assert process.returncode == 0, (out, err)


def submit(call, workspace_root):
    (workspace_root / "task.md").write_text("完整原目标与权限限制：只处理 note.txt。\n")
    return call("submit", "--workspace", "demo", "--backend", "fake", "--input", "task.md",
                "--idempotency-key", "stable-submit-key")


def snapshot(call, task_id, run_id):
    value, code = call("snapshot", "--task-id", task_id, "--run-id", run_id, "--target", "note.txt")
    assert code == 0, value
    raw = Path(value["artifacts"]["response"])
    return raw, json.loads(raw.read_text())["data"]


def write_report(root, binding, passed):
    report = {key: binding[key] for key in ("task_id", "run_id", "input_hash", "target_hash")}
    report["checks"] = [{"name": "固定消费者检查", "passed": passed, "detail": "离线测试夹具的真实判断"}]
    (root / "report.json").write_text(json.dumps(report, ensure_ascii=False))


def test_copied_skill_fake_roundtrip_and_no_prompt_echo(resident, workspace_root):
    call, client, _ = resident
    discovered, code = call("discover")
    assert code == 0 and discovered["ok"]
    first, code = submit(call, workspace_root)
    assert code == 0 and first["ok"]
    repeated, code = submit(call, workspace_root)
    assert code == 0 and first["ids"] == repeated["ids"]
    (workspace_root / "task.md").write_text("同键不同目标，不能被再次执行")
    conflict, code = call("submit", "--workspace", "demo", "--backend", "fake", "--input", "task.md",
                          "--idempotency-key", "stable-submit-key")
    assert code != 0 and conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    (workspace_root / "task.md").write_text("完整原目标与权限限制：只处理 note.txt。\n")
    task_id, run_id = first["ids"]["task_id"], first["ids"]["run_id"]
    waited, code = call("wait", "--task-id", task_id, "--run-id", run_id, "--timeout", "1")
    assert code == 0 and waited["reason"] == "terminal"
    assert waited["run"]["status"] == "succeeded" and waited["task"]["acceptance"] != "passed"
    intent = json.loads(Path(waited["artifacts"]["intent"]).read_text())
    assert "--compact" in intent["argv"] and "--no-events" in intent["argv"]
    full, code = call("inspect", "--task-id", task_id)
    assert code == 0 and "完整原目标" not in json.dumps(full, ensure_ascii=False)
    response = json.loads(Path(full["artifacts"]["response"]).read_text())
    assert response["data"]["task"]["text"] == (workspace_root / "task.md").read_text()
    assert client.handle("task.get", {"task_id": task_id}).data["task"]["run_count"] == 1
    for path in Path(full["artifacts"]["directory"]).iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(Path(full["artifacts"]["directory"]).stat().st_mode) == 0o700


def test_binding_evaluation_repair_replay_and_drift(resident, workspace_root, isolated_env):
    call, client, _ = resident
    submitted, _ = submit(call, workspace_root)
    task_id, run_id = submitted["ids"]["task_id"], submitted["ids"]["run_id"]
    snap_file, binding = snapshot(call, task_id, run_id)
    write_report(workspace_root, binding, False)
    evaluated, code = call("evaluate", "--snapshot-file", str(snap_file), "--workspace-root", str(workspace_root),
                           "--report", "report.json")
    assert code == 0 and evaluated["acceptance"] == "failed"
    assert evaluated["check_sources"] == ["external_reported"]
    instructions = isolated_env / "repair.md"
    instructions.write_text("修复 note.txt 的已确认缺陷，保留原限制。")
    argv = ["repair", "--snapshot-file", str(snap_file), "--instructions-file", str(instructions),
            "--max-repairs", "2", "--idempotency-key", "repair-once"]
    repaired, code = call(*argv)
    assert code == 0, repaired
    repeated, code = call(*argv)
    assert code == 0
    for key in ("task_id", "run_id", "operation_id"):
        assert repaired["ids"][key] == repeated["ids"][key]
    current = client.handle("task.get", {"task_id": task_id}).data
    assert current["task"]["repair_count"] == 1 and current["task"]["run_count"] == 2
    snap_file, binding = snapshot(call, task_id, current["run"]["id"])
    write_report(workspace_root, binding, True)
    evaluated, code = call("evaluate", "--snapshot-file", str(snap_file), "--workspace-root", str(workspace_root),
                           "--report", "report.json")
    assert code == 0 and evaluated["acceptance"] == "passed"
    (workspace_root / "note.txt").write_text("改变验收目标")
    stale, code = call("status", "--task-id", task_id)
    assert code == 0 and stale["task"]["evidence_stale"] and stale["task"]["acceptance"] == "pending"
    rejected, code = call("evaluate", "--snapshot-file", str(snap_file), "--workspace-root", str(workspace_root),
                          "--report", "report.json")
    assert code != 0 and not rejected["ok"]


def test_external_report_keeps_required_review(resident, workspace_root):
    call, _, _ = resident
    submitted, _ = submit(call, workspace_root)
    snap_file, binding = snapshot(call, submitted["ids"]["task_id"], submitted["ids"]["run_id"])
    write_report(workspace_root, binding, True)
    argv = ["evaluate", "--snapshot-file", str(snap_file), "--workspace-root", str(workspace_root),
            "--report", "report.json"]
    first, code = call(*argv, "--require-review")
    assert code == 0 and first["acceptance"] == "pending" and first["task"]["paused"]
    second, code = call(*argv)
    assert code == 0 and second["acceptance"] == "pending" and second["task"]["external_require_review"]


@pytest.mark.parametrize("script,reason,status", [("failure", "terminal", "failed"),
    ("wait_input", "waiting_input", "waiting_input"), ("lost_reply", "unknown", "pending_reconcile"),
    ("cancel_accept_only", "deadline", "running")])
def test_observation_boundaries_preserved(resident, script, reason, status):
    call, client, _ = resident
    admitted = client.handle("task.submit", {"workspace": "demo", "backend": "fake", "script": script, "text": "测试"})
    assert admitted.ok, admitted.error
    task_id = admitted.ids["task_id"]
    value, code = call("wait", "--task-id", task_id, "--timeout", "0.15")
    assert value["reason"] == reason and value["run"]["status"] == status
    if reason == "deadline":
        assert code == 124 and not value["run"]["cancel_requested"]
    elif reason == "unknown":
        assert code != 0 and not value["ok"]
    else:
        assert code == 0
    persisted = client.handle("task.get", {"task_id": task_id}).data
    assert persisted["task"]["run_count"] == 1
    if script == "wait_input":
        assert persisted["approval"]["state"] == "pending"
        assert "主控" in value["next_action"]


def test_cancel_request_is_not_confirmed_termination(resident):
    call, client, _ = resident
    admitted = client.handle("task.submit", {"workspace": "demo", "backend": "fake", "script": "cancel_accept_only"})
    value, code = call("cancel", "--task-id", admitted.ids["task_id"])
    assert code == 0 and value["run"]["cancel_requested"] and value["run"]["terminated"] is False


@pytest.mark.parametrize("mode", ["malformed", "timeout"])
def test_uncertain_mutation_is_called_once_and_private_evidence_retained(tmp_path, mode):
    executable = tmp_path / "fake-cli"
    counter = tmp_path / "calls"
    executable.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nimport time\n"
                          f"p=Path({str(counter)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x')\n"
                          + ("print('{broken', flush=True)\n" if mode == "malformed" else "time.sleep(10)\n"))
    executable.chmod(0o700)
    args = skill.parser().parse_args(["--asterun-bin", str(executable), "--state-dir", str(tmp_path / "state"),
        "--artifacts-dir", str(tmp_path / "calls-data"), "--cli-timeout", "2", "submit",
        "--workspace", "demo", "--backend", "fake", "--input", "task.md", "--idempotency-key", "stable"])
    value, code = skill.execute(args)
    assert code != 0 and not value["ok"] and value["outcome"] == "unknown"
    assert counter.read_text() == "x"
    assert Path(value["artifacts"]["intent"]).is_file()
    assert Path(value["artifacts"]["response"]).is_file()


def test_large_backend_output_stays_in_private_file(resident):
    call, client, _ = resident
    prompt = "大段输出" * 10000
    admitted = client.handle("task.submit", {"workspace": "demo", "backend": "fake", "text": prompt})
    value, code = call("inspect", "--task-id", admitted.ids["task_id"])
    assert code == 0 and len(skill.dump(value)) < 4096
    assert value["metrics"]["response_bytes"] > 100000
    assert prompt not in json.dumps(value, ensure_ascii=False)
    print("SKILL_PAYLOAD_SAMPLE " + json.dumps({"fixture_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "full_response_bytes": value["metrics"]["response_bytes"], "visible_summary_bytes": len(skill.dump(value)),
        "native_tokens": "not_measured", "subscription_quota_effect": "unknown"}))


@pytest.mark.parametrize("target", ["../report.json", "/report.json", "a//b.json", "./report.json"])
def test_report_scope_rejects_noncanonical_paths(target):
    with pytest.raises(skill.InputError):
        skill.relative_file(target)


def test_symbolic_report_parent_and_fifo_rejected_without_blocking(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(skill.InputError):
        skill.scoped_file(root, "linked/report.json")
    fifo = root / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(skill.InputError):
        skill.regular_bytes(fifo)


@pytest.mark.parametrize("raw", [b'{"ok":true,"ok":false}', b'{"x":NaN}', b'\xff'])
def test_invalid_json_is_not_silently_normalized(raw):
    with pytest.raises(skill.InputError):
        skill.decode(raw)


def test_packaged_skill_has_only_reviewed_files_and_matching_hashes(tmp_path):
    output = tmp_path / "skill.zip"
    result = packager.build(SOURCE, output, "a" * 40)
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {"asterun/" + name for name in (*packager.MEMBERS, "manifest.json")}
        manifest = json.loads(archive.read("asterun/manifest.json"))
        for name, item in manifest["files"].items():
            raw = archive.read("asterun/" + name)
            assert item["sha256"] == hashlib.sha256(raw).hexdigest() and item["bytes"] == len(raw)
    assert result["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    with pytest.raises(FileExistsError):
        packager.build(SOURCE, output, "a" * 40)


def test_package_rejects_symlink_and_private_content(tmp_path):
    copied = tmp_path / "skill"
    shutil.copytree(SOURCE, copied, ignore=shutil.ignore_patterns("__pycache__"))
    script = copied / "scripts/run.py"
    script.unlink()
    script.symlink_to(SOURCE / "scripts/run.py")
    with pytest.raises(ValueError):
        packager.build(copied, tmp_path / "bad.zip", "a" * 40)
    script.unlink()
    script.write_text("Bearer " + "x" * 30)
    with pytest.raises(ValueError):
        packager.build(copied, tmp_path / "bad.zip", "a" * 40)


def test_old_core_rejection_is_preserved_without_fallback(tmp_path):
    executable = tmp_path / "old-cli"
    counter = tmp_path / "invocations"
    envelope = json.dumps({"ok": False, "data": None, "error": {"code": "UNKNOWN_FIELD"}, "ids": {}})
    executable.write_text(f"#!{sys.executable}\nfrom pathlib import Path\n"
        f"p=Path({str(counter)!r});p.write_text(p.read_text()+'x' if p.exists() else 'x')\n"
        f"print({envelope!r})\nraise SystemExit(2)\n")
    executable.chmod(0o700)
    args = skill.parser().parse_args(["--asterun-bin", str(executable), "--state-dir", str(tmp_path / "state"),
        "--artifacts-dir", str(tmp_path / "data"), "status", "--task-id", "tsk_old"])
    value, code = skill.execute(args)
    assert code == 2 and value["error"]["code"] == "UNKNOWN_FIELD" and counter.read_text() == "x"


def test_unrecognized_native_status_cannot_be_reported_finished():
    result = skill.summarize({"ok": True, "data": {"run": {"status": "future_unknown"}}}, "status")
    assert "停止自动推进" in result["next_action"]


def test_artifacts_cannot_write_core_state_or_follow_directory_symlink(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(state, target_is_directory=True)
    for output in (state, state / "calls", linked):
        args = skill.parser().parse_args(["--state-dir", str(state), "--artifacts-dir", str(output), "discover"])
        with pytest.raises(skill.InputError):
            skill.operation_directory(args)
    assert list(state.iterdir()) == []


@pytest.mark.parametrize("timeout", ["0", "56", "nan", "inf"])
def test_invalid_wait_budget_is_rejected_before_cli_call(tmp_path, timeout):
    args = skill.parser().parse_args(["--state-dir", str(tmp_path / "state"), "--artifacts-dir", str(tmp_path / "calls"),
                                     "wait", "--task-id", "tsk_test", "--timeout", timeout])
    with pytest.raises(skill.InputError):
        skill.request_args(args, tmp_path)
