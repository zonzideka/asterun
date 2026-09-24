"""分离检查、消费者接线、独立审查、组合和部署证据；缺项保持未验。"""
from __future__ import annotations

import re

from asterun.quality import digest

STAGES = ("coding", "consumer", "independent_review", "integration", "deployment")


def diagnostics(text):
    groups = {}
    for line in text.splitlines()[:80]:
        path = re.search(r"([\w./-]+\.(?:tsx?|jsx?|py|swift|go|rs|json))(?::\d+)?", line)
        category = re.search(r"\b(TS\d+|SyntaxError|TypeError|ImportError|ModuleNotFoundError)\b", line)
        category = category.group(1) if category else "fixture" if "fixture" in line.lower() else "check"
        key = (path.group(1) if path else None, category)
        if key not in groups and len(groups) >= 12:
            continue
        group = groups.setdefault(key, {"path": key[0], "category": category, "count": 0, "sample": line[:240]})
        group["count"] += 1
    return list(groups.values())


def evidence(app, task, run, bound):
    stages = {stage: {"status": "unverified", "source": None} for stage in STAGES}
    implementation_id = task.quality.get("implementation_run_id") if run.role == "review" else run.id.value
    implementations = [item for item in app.store.list_runs(task.id) if item.id.value == implementation_id]
    jobs = implementations[0].native.get("local_checks", {}) if implementations else {}
    checks = app.config.workflow.local_checks
    for stage in ("coding", "consumer", "integration"):
        names = [name for name, spec in checks.items() if spec.get("stage", "coding") == stage]
        rows = []
        for name in names:
            matches = [job for job in jobs.values() if job["check_name"] == name]
            job = matches[-1] if matches else None
            status = "unverified"
            result = job.get("result", {}) if job else {}
            valid = (job and job["bound"]["expected_target_hash"] == bound["target_hash"]
                     and job["bound"]["expected_input_hash"] == bound["input_hash"]
                     and job["bound"]["expected_revision"] == bound["revision"]
                     and job["check_sha256"] == digest(checks[name]))
            if job and not valid:
                status = "stale"
            elif job and job["status"] == "completed":
                status = "passed" if result.get("passed") is True else "failed"
            elif job:
                status = job["status"]
            rows.append({"check_name": name, "status": status, "job_id": job["job_id"] if job else None,
                         "cache_hit": result.get("cache_hit", False),
                         "runtime_fingerprint": result.get("runtime_fingerprint"),
                         "diagnostics": diagnostics(result.get("diagnostic", "")) if status == "failed" else []})
        if rows:
            states = {row["status"] for row in rows}
            stages[stage] = {"status": "passed" if states == {"passed"} else "failed" if "failed" in states else "unverified",
                             "source": "isolated_local_check", "checks": rows}
    for review in reversed(task.quality.get("reviews", [])):
        if (review.get("target_hash") == bound["target_hash"] and review.get("input_hash") == bound["input_hash"]
                and review.get("config_revision") == bound["revision"]
                and review.get("implementation_run_id") == implementation_id):
            stages["independent_review"] = {"status": "simulated" if review.get("simulated") else
                                            "failed" if review.get("findings") else "passed",
                                            "source": "core_review", "run_id": review["run_id"],
                                            "independence": review.get("independence"),
                                            "provider_identity_verified": review.get("provider_identity_verified", False)}
            break
    # 外部自报不推导成独立审查，也不推导成部署。独立审查和现场交付仍需各自证据。
    return {"task_id": task.id.value, "run_id": run.id.value, "input_hash": bound["input_hash"],
            "target_hash": bound["target_hash"], "revision": bound["revision"], "target_paths": bound["target_paths"],
            "stages": stages, "external_source": task.external_evaluation.get("source") if task.external_evaluation else None,
            "all_stages_verified": all(row["status"] == "passed" for row in stages.values()),
            "acceptance_unchanged": True, "scope": "selected_files_only"}
