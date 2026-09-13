from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from asterun.cli import main


def _run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {
        "HOME": str((cwd or Path.cwd()) / "unused-home"),
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
    }
    return subprocess.run(
        [sys.executable, "-m", "asterun", *args],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def test_cli_submit_get_and_cancel(config_path: Path, isolated_env: Path) -> None:
    state = isolated_env / "state"
    submit = _run(
        [
            "--config",
            str(config_path),
            "--state-dir",
            str(state),
            "task-submit",
            "--workspace",
            "demo",
            "--script",
            "cancel_accept_only",
            "--text",
            "cli",
        ]
    )
    assert submit.returncode == 0, submit.stderr + submit.stdout
    body = json.loads(submit.stdout)
    task_id = body["ids"]["task_id"]
    conversation_id = body["ids"]["conversation_id"]
    resumed = _run(
        [
            "--config",
            str(config_path),
            "--state-dir",
            str(state),
            "session-resume",
            "--conversation-id",
            conversation_id,
        ]
    )
    assert resumed.returncode == 0, resumed.stderr + resumed.stdout
    assert json.loads(resumed.stdout)["data"]["native_resumed"] is False
    fetched = _run(
        ["--config", str(config_path), "--state-dir", str(state), "task-get", task_id]
    )
    assert json.loads(fetched.stdout)["data"]["run"]["status"] == "pending_reconcile"
    cancelled = _run(
        ["--config", str(config_path), "--state-dir", str(state), "task-cancel", task_id]
    )
    payload = json.loads(cancelled.stdout)
    assert payload["data"]["cancel_requested"] is True
    assert payload["data"]["terminated"] is False


def test_cli_rejects_self_reported_request_file(config_path: Path, isolated_env: Path) -> None:
    request = isolated_env / "req.json"
    request.write_text(
        json.dumps({"workspace": "demo", "script": "success", "granted": True}),
        encoding="utf-8",
    )
    result = _run(
        [
            "--config",
            str(config_path),
            "--state-dir",
            str(isolated_env / "state"),
            "task-submit",
            "--request",
            str(request),
        ]
    )
    body = json.loads(result.stdout)
    assert result.returncode == 3
    assert body["error"]["code"] == "SCOPE_DENIED"


def test_cli_version_and_missing_config(isolated_env: Path) -> None:
    version = _run(["version"])
    assert json.loads(version.stdout)["data"]["version"] == "0.1.0a13"
    missing = _run(["--home", str(isolated_env / "empty-home"), "config-validate"])
    assert json.loads(missing.stdout)["error"]["code"] == "CONFIG_REQUIRED"


def test_cli_workflow_evaluate_and_scheduler_status(config_path: Path, isolated_env: Path) -> None:
    state = isolated_env / "state"
    submit = _run(
        [
            "--config",
            str(config_path),
            "--state-dir",
            str(state),
            "task-submit",
            "--workspace",
            "demo",
            "--script",
            "success",
            "--text",
            "cli 验收",
        ]
    )
    assert submit.returncode == 0, submit.stderr + submit.stdout
    body = json.loads(submit.stdout)
    task_id = body["ids"]["task_id"]
    digest = body["data"]["task"]["input_hash"]
    evaluated = _run(
        [
            "--config",
            str(config_path),
            "--state-dir",
            str(state),
            "workflow-evaluate",
            task_id,
            "--revision",
            "1",
            "--input-hash",
            digest,
            "--checks",
            json.dumps([{"kind": "contains", "text": "cli 验收"}]),
        ]
    )
    assert evaluated.returncode == 0, evaluated.stderr + evaluated.stdout
    result = json.loads(evaluated.stdout)
    assert result["data"]["acceptance"] == "passed"
    status = _run(
        ["--config", str(config_path), "--state-dir", str(state), "scheduler-status"]
    )
    assert status.returncode == 0, status.stderr + status.stdout
    snapshot = json.loads(status.stdout)
    assert snapshot["data"]["notes"]["tuning_defaults_are_not_measured_slo"] is True


def test_cli_diagnose_is_redacted(config_path: Path, isolated_env: Path) -> None:
    result = _run(
        ["--config", str(config_path), "--state-dir", str(isolated_env / "state"), "diagnose"]
    )
    assert result.returncode == 0, result.stderr + result.stdout
    body = json.loads(result.stdout)
    dumped = json.dumps(body, ensure_ascii=False)
    assert body["data"]["notes"]["does_not_include_credentials"] is True
    assert "account:test" not in dumped


def test_main_helper_returns_zero_for_version() -> None:
    assert main(["version"]) == 0
