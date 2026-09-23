"""离线 CLI 范例：固定报告、同任务修复、重放、漂移失效和复验。"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    with tempfile.TemporaryDirectory(prefix="asterun-caller-demo-") as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        source = workspace / "answer.txt"
        source.write_text("wrong\n")
        config = root / "config.json"
        config.write_text(json.dumps({"schema_version": 1, "revision": 1,
            "workspaces": {"demo": {"root": str(workspace), "allow_non_git": True}},
            "backends": {"fake": {"kind": "fake"}}, "default_backend": "fake"}))

        def cli(*args):
            result = subprocess.run([sys.executable, "-m", "asterun", "--config", str(config),
                                     "--state-dir", str(root / "state"), *args], capture_output=True, text=True)
            envelope = json.loads(result.stdout)
            assert envelope["ok"], envelope.get("error")
            return envelope["data"]

        submitted = cli("task-submit", "--workspace", "demo", "--text", "answer.txt 应等于 correct", "--idempotency-key", "submit-once")
        task = submitted["task"]["id"]

        def bound():
            snap = cli("workflow-snapshot", task, "--target", "answer.txt")
            return {"task_id": task, "expected_run_id": snap["run_id"], "expected_revision": snap["revision"],
                    "expected_input_hash": snap["input_hash"], "expected_target_hash": snap["target_hash"],
                    "target_paths": snap["target_paths"]}

        def evaluate(binding):
            # Deterministic external check, explicitly self-reported to the core.
            passed = source.read_text().strip() == "correct"
            report = {"task_id": task, "run_id": binding["expected_run_id"], "input_hash": binding["expected_input_hash"],
                      "target_hash": binding["expected_target_hash"], "checks": [{"name": "exact answer", "passed": passed,
                      "detail": "answer.txt expected correct; actual " + source.read_text().strip()}]}
            raw = json.dumps(report).encode()
            report_path = f"report-{binding['expected_run_id']}-{binding['expected_target_hash'][:12]}.json"
            (workspace / report_path).write_bytes(raw)
            request = root / "evaluate.json"
            request.write_text(json.dumps({**binding, "checks": [{"kind": "external_report", "path": report_path,
                                                                 "sha256": hashlib.sha256(raw).hexdigest()}]}))
            return cli("workflow-evaluate", task, "--request", str(request))

        initial = bound()
        assert evaluate(initial)["acceptance"] == "failed"
        request = root / "repair.json"
        request.write_text(json.dumps({**initial, "idempotency_key": "repair-once"}))
        repaired = cli("workflow-repair", task, "--request", str(request))
        replayed = cli("workflow-repair", task, "--request", str(request))
        assert repaired["run"]["id"] == replayed["run"]["id"]
        assert repaired["task"]["repair_count"] == 1 and repaired["task"]["run_count"] == 2
        assert "expected correct" in repaired["run"]["prompt"]
        # The fake backend does not edit files. This fixture stands in for its
        # implementation output; it is not a claim of real agent execution.
        source.write_text("correct\n")
        assert evaluate(bound())["acceptance"] == "passed"
        source.write_text("drift\n")
        stale = cli("task-get", task)
        assert stale["task"]["evidence_stale"] and stale["task"]["acceptance"] != "passed"
        source.write_text("correct\n")
        assert evaluate(bound())["acceptance"] == "passed"
        print(json.dumps({"fake_cli_workflow": "passed", "same_task": True, "runs": 2,
                          "idempotent_repair": True, "drift_invalidated": True, "real_model_called": False}))


if __name__ == "__main__":
    main()
