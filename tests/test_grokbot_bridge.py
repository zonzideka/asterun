from __future__ import annotations

import concurrent.futures
import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time

import pytest

from tests.conftest import write_config


SCRIPT = Path(__file__).resolve().parents[1] / "integrations/grokbot/scripts/bridge.py"
spec = importlib.util.spec_from_file_location("grokbot_bridge", SCRIPT)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

SRC = Path(__file__).resolve().parents[1] / "src"
FAKE_ASTERUN = textwrap.dedent(
    r"""
    #!{python}
    import hashlib, json, os, sys, time
    from pathlib import Path

    state_path = Path(__file__).with_name("fake-state.json")
    state = json.loads(state_path.read_text())
    args = sys.argv[1:]
    state.setdefault("calls", []).append(args)
    command = None
    for name in ("version", "config-validate", "backend-inspect", "task-submit", "task-get", "task-events"):
        if name in args:
            command = name
            break

    def save():
        state_path.write_text(json.dumps(state))

    def envelope(ok, data=None, error=None, ids=None):
        print(json.dumps({{"ok": ok, "data": data, "error": error, "ids": ids or {{}}, "evidence_ref": None}}, ensure_ascii=False, indent=2))
        raise SystemExit(0 if ok else 1)

    def flag(name):
        if name not in args:
            return None
        index = args.index(name)
        return args[index + 1] if index + 1 < len(args) else None

    mode = state.get("submit_mode", "success")
    if command == "version":
        save()
        envelope(True, {{"version": "0.1.0a13", "package": "asterun"}})
    if command == "config-validate":
        workspaces = {{name: {{"alias": name, "root": "/tmp/ws", "allow_non_git": True}} for name in state.get("workspaces", ["demo"])}}
        backends = {{item["backend"]: {{"name": item["backend"], "kind": item.get("kind", "fake"), "enabled": True}} for item in state.get("backends", [])}}
        save()
        envelope(True, {{"valid": True, "config": {{"workspaces": workspaces, "backends": backends}}, "loaded_config_revision": 1, "applied_config_revision": 1}})
    if command == "backend-inspect":
        save()
        envelope(True, {{"backends": state.get("backends", [])}})
    if command == "task-submit":
        state["submit_calls"] = state.get("submit_calls", 0) + 1
        text = sys.stdin.read()
        request = flag("--idempotency-key") or ""
        workspace = flag("--workspace")
        backend = flag("--backend")
        conversation = flag("--conversation-id") or ""
        digest = hashlib.sha256(json.dumps({{"workspace": workspace, "backend": backend, "text": text, "conversation_id": conversation}}, sort_keys=True).encode()).hexdigest()
        prior = state.setdefault("submissions", {{}}).get(request)
        if prior and prior["digest"] != digest:
            save()
            envelope(False, error={{"code": "IDEMPOTENCY_CONFLICT", "message": "冲突", "retryable": False}})
        if mode == "invalid_json":
            save()
            sys.stdout.write("not-json traceback SECRET_VALUE_XYZ\\n")
            raise SystemExit(1)
        if mode == "timeout":
            save()
            time.sleep(30)
        if mode == "fail_once":
            if state["submit_calls"] == 1:
                save()
                sys.stdout.write("truncated")
                raise SystemExit(1)
            mode = "success"
        if prior and prior.get("task_id"):
            save()
            envelope(True, {{"task": {{"id": prior["task_id"], "workspace": workspace, "backend": backend, "acceptance": "pending"}}, "run": {{"id": prior["run_id"], "status": prior.get("status", "succeeded"), "summary": "reused"}}}}, ids={{"task_id": prior["task_id"], "run_id": prior["run_id"], "conversation_id": prior.get("conversation_id", "conv_1")}})
        task_id = "tsk_" + hashlib.sha256(request.encode()).hexdigest()[:24]
        run_id = "run_" + hashlib.sha256((request + ":1").encode()).hexdigest()[:24]
        status = {{"success": "succeeded", "wait_input": "waiting_input", "failed": "failed", "unknown": "pending_reconcile", "paused": "paused"}}.get(mode, "succeeded")
        summary = {{"success": "fake 成功", "wait_input": "fake 等待输入", "failed": "fake 失败", "unknown": "结果未知", "paused": "已暂停"}}.get(mode, "fake 成功")
        approval = None
        if status == "waiting_input":
            approval = {{"id": "appr_" + task_id[-8:], "state": "pending", "target_hash": "ab" * 32, "summary": "fake 等待输入", "task_id": task_id, "run_id": run_id}}
        rec = {{"digest": digest, "task_id": task_id, "run_id": run_id, "conversation_id": "conv_" + task_id[-8:], "status": status, "summary": summary, "approval": approval, "gets": 0, "text": text}}
        state["submissions"][request] = rec
        state.setdefault("tasks", {{}})[task_id] = rec
        save()
        envelope(True, {{"task": {{"id": task_id, "workspace": workspace, "backend": backend, "acceptance": "pending", "paused": status == "paused"}}, "run": {{"id": run_id, "status": status, "summary": summary, "error_code": None}}, "approval": approval}}, ids={{"task_id": task_id, "run_id": run_id, "conversation_id": rec["conversation_id"]}})
    if command == "task-get":
        task_id = args[-1]
        rec = state.get("tasks", {{}}).get(task_id)
        if rec is None:
            save()
            envelope(False, error={{"code": "NOT_FOUND", "message": "missing"}})
        rec["gets"] = rec.get("gets", 0) + 1
        run_id = rec["run_id"]
        status = rec["status"]
        approval = rec.get("approval")
        if state.get("get_mode") == "run_switch" and rec["gets"] >= 2:
            run_id = rec["run_id"] + "_b"
            status = "succeeded"
            approval = None
            rec["run_id"] = run_id
            rec["status"] = status
            rec["summary"] = "第二轮成功"
            rec["approval"] = None
        if state.get("get_fail_task") == task_id:
            save()
            envelope(False, error={{"code": "BACKEND_UNAVAILABLE", "message": "transient"}})
        save()
        envelope(True, {{"task": {{"id": task_id, "workspace": "demo", "backend": "fake", "acceptance": "pending", "paused": status == "paused"}}, "run": {{"id": run_id, "status": status, "summary": rec.get("summary"), "error_code": rec.get("error_code")}}, "approval": approval}})
    if command == "task-events":
        task_id = args[args.index("task-events") + 1]
        rec = state.get("tasks", {{}}).get(task_id, {{}})
        cursor = int(flag("--cursor") or 0)
        save()
        envelope(True, {{"events": [{{"type": "message", "payload": {{"text": "公开事件"}}}}] if cursor == 0 else [], "next_cursor": max(cursor, 1), "run_id": rec.get("run_id"), "task_id": task_id}})
    save()
    envelope(False, error={{"code": "INVALID_REQUEST", "message": "unknown command"}})
    """
).lstrip()


def private_dir(tmp_path: Path) -> Path:
    path = tmp_path / "private"
    path.mkdir(mode=0o700)
    return path


def write_fake(tmp_path: Path, **updates) -> Path:
    directory = tmp_path / "bin"
    directory.mkdir()
    state = {
        "workspaces": ["demo", "other"],
        "backends": [
            {"backend": "fake", "kind": "fake", "enabled_in_config": True,
             "is_real_connection": False, "authentication": "not_applicable"},
            {"backend": "other", "kind": "fake", "enabled_in_config": True,
             "is_real_connection": False, "authentication": "not_applicable"},
        ],
        "submit_mode": "success",
    }
    state.update(updates)
    (directory / "fake-state.json").write_text(json.dumps(state), encoding="utf-8")
    script = directory / "asterun"
    script.write_text(FAKE_ASTERUN.format(python=sys.executable), encoding="utf-8")
    script.chmod(0o700)
    (directory / "config.json").write_text("{}", encoding="utf-8")
    (directory / "core-state").mkdir()
    return directory


def load_fake_state(directory: Path) -> dict:
    return json.loads((directory / "fake-state.json").read_text(encoding="utf-8"))


def save_fake_state(directory: Path, state: dict) -> None:
    (directory / "fake-state.json").write_text(json.dumps(state), encoding="utf-8")


def bridge_home_env(home: Path) -> dict[str, str]:
    home.mkdir(exist_ok=True)
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "LANG": "C",
        "LC_ALL": "C",
    }


def run_bridge(ledger: Path, args: list[str], *, home: Path, stdin: str | None = None,
               timeout: float = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--ledger", str(ledger), *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=bridge_home_env(home),
    )


def envelope(result: subprocess.CompletedProcess[str]) -> dict:
    assert result.stdout.strip(), result.stderr
    body = json.loads(result.stdout)
    assert set(body) == {"ok", "data", "error"}
    return body


def init_args(directory: Path, destination: str = "chat-1") -> list[str]:
    return [
        "init", "--binding", "primary", "--asterun", str(directory / "asterun"),
        "--config", str(directory / "config.json"), "--core-state", str(directory / "core-state"),
        "--location", "local", "--workspace", "demo", "--backend", "fake",
        "--destination", destination,
    ]


def test_process_reopen_persists_binding_and_task(tmp_path: Path) -> None:
    directory = write_fake(tmp_path)
    home = tmp_path / "home"
    ledger = private_dir(tmp_path) / "ledger.sqlite"
    first = run_bridge(ledger, init_args(directory), home=home)
    body = envelope(first)
    assert first.returncode == 0 and body["ok"] is True
    assert body["data"]["authenticated"] is False and body["data"]["callable"] is False
    assert ledger.stat().st_mode & 0o777 == 0o600
    assert ledger.parent.stat().st_mode & 0o777 == 0o700
    submitted = run_bridge(
        ledger,
        ["submit", "--binding", "primary", "--request-id", "req-1", "--workspace", "demo",
         "--backend", "fake", "--input", "-"],
        home=home, stdin="跟踪持久化",
    )
    created = envelope(submitted)
    assert submitted.returncode == 0
    task_id = created["data"]["task_id"]
    reopened = run_bridge(ledger, ["status", "--binding", "primary"], home=home)
    status = envelope(reopened)["data"]
    assert status["tasks"]["total"] == 1
    assert status["destination"] == "chat-1"
    reused = envelope(run_bridge(ledger, init_args(directory), home=home))
    assert reused["ok"] is True and reused["data"]["reused"] is True
    again = envelope(run_bridge(
        ledger,
        ["submit", "--binding", "primary", "--request-id", "req-1", "--workspace", "demo",
         "--backend", "fake", "--input", "-"],
        home=home, stdin="跟踪持久化",
    ))
    assert again["data"]["reused"] is True and again["data"]["task_id"] == task_id


def test_submit_same_key_conflict_on_different_input(tmp_path: Path) -> None:
    directory = write_fake(tmp_path)
    home = tmp_path / "home"
    ledger = private_dir(tmp_path) / "ledger.sqlite"
    assert run_bridge(ledger, init_args(directory), home=home).returncode == 0
    first = envelope(run_bridge(
        ledger, ["submit", "--binding", "primary", "--request-id", "same",
                 "--workspace", "demo", "--backend", "fake", "--input", "-"],
        home=home, stdin="原文",
    ))
    assert first["ok"] is True
    conflict = envelope(run_bridge(
        ledger, ["submit", "--binding", "primary", "--request-id", "same",
                 "--workspace", "demo", "--backend", "fake", "--input", "-"],
        home=home, stdin="不同输入",
    ))
    assert conflict["ok"] is False and conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    denied = envelope(run_bridge(
        ledger, ["submit", "--binding", "primary", "--request-id", "other",
                 "--workspace", "other", "--backend", "fake", "--input", "-"],
        home=home, stdin="越界",
    ))
    assert denied["error"]["code"] == "SCOPE_DENIED"


def test_lost_submit_response_retries_original_key(tmp_path: Path) -> None:
    directory = write_fake(tmp_path, submit_mode="fail_once")
    home = tmp_path / "home"
    ledger = private_dir(tmp_path) / "ledger.sqlite"
    assert run_bridge(ledger, init_args(directory), home=home).returncode == 0
    lost = envelope(run_bridge(
        ledger, ["--timeout", "5", "submit", "--binding", "primary", "--request-id", "lost",
                 "--workspace", "demo", "--backend", "fake", "--input", "-"],
        home=home, stdin="同一请求",
    ))
    assert lost["ok"] is False and lost["error"]["code"] == "CORE_RESULT_UNKNOWN"
    assert lost["error"]["retryable"] is True
    retry = envelope(run_bridge(
        ledger, ["submit", "--binding", "primary", "--request-id", "lost",
                 "--workspace", "demo", "--backend", "fake", "--input", "-"],
        home=home, stdin="同一请求",
    ))
    assert retry["ok"] is True
    assert retry["data"]["task_id"]
    assert load_fake_state(directory)["submit_calls"] == 2


def test_concurrent_submit_same_key(tmp_path: Path) -> None:
    directory = write_fake(tmp_path)
    home = tmp_path / "home"
    ledger = private_dir(tmp_path) / "ledger.sqlite"
    assert run_bridge(ledger, init_args(directory), home=home).returncode == 0

    def once() -> dict:
        result = run_bridge(
            ledger, ["submit", "--binding", "primary", "--request-id", "race",
                     "--workspace", "demo", "--backend", "fake", "--input", "-"],
            home=home, stdin="并发同一键",
        )
        return envelope(result)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: once(), range(2)))
    assert all(item["ok"] for item in results)
    ids = {item["data"]["task_id"] for item in results}
    assert len(ids) == 1
    conn = __import__("sqlite3").connect(ledger)
    count = conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
    conn.close()
    assert count == 1


def test_poll_terminal_and_approval_notifications_dedup(tmp_path: Path) -> None:
    directory = write_fake(tmp_path, submit_mode="wait_input")
    home = tmp_path / "home"
    ledger = private_dir(tmp_path) / "ledger.sqlite"
    assert run_bridge(ledger, init_args(directory), home=home).returncode == 0
    envelope(run_bridge(
        ledger, ["submit", "--binding", "primary", "--request-id", "need-appr",
                 "--workspace", "demo", "--backend", "fake", "--input", "-"],
        home=home, stdin="需要审批的任务",
    ))
    first = envelope(run_bridge(ledger, ["poll", "--binding", "primary"], home=home))
    second = envelope(run_bridge(ledger, ["poll", "--binding", "primary"], home=home))
    assert first["data"]["observed"][0]["status"] == "waiting_input"
    assert first["data"]["observed"][0]["notification_created"] is True
    assert second["data"]["observed"][0]["notification_created"] is False
    assert first["data"]["observed"][0]["delivery_key"] == second["data"]["observed"][0]["delivery_key"]
    inbox = envelope(run_bridge(ledger, ["inbox", "--binding", "primary"], home=home))["data"]["items"]
    assert len(inbox) == 1
    assert inbox[0]["evidence"]["approval"]["target_hash"]
    assert "需要审批的任务" not in json.dumps(inbox, ensure_ascii=False)

    state = load_fake_state(directory)
    state["submit_mode"] = "success"
    save_fake_state(directory, state)
    done = envelope(run_bridge(
        ledger, ["submit", "--binding", "primary", "--request-id", "done",
                 "--workspace", "demo", "--backend", "fake", "--input", "-"],
        home=home, stdin="完成任务",
    ))
    polled = envelope(run_bridge(ledger, ["poll", "--binding", "primary", "--limit", "20"], home=home))
    statuses = {item["status"] for item in polled["data"]["observed"]}
    assert "succeeded" in statuses and "waiting_input" in statuses
    inbox2 = envelope(run_bridge(ledger, ["inbox", "--binding", "primary"], home=home))["data"]["items"]
    assert len(inbox2) == 2
    assert all(item["run_success_is_not_acceptance"] is True for item in inbox2)
    assert "完成任务" not in json.dumps(inbox2, ensure_ascii=False)
    assert done["data"]["task_id"]


def test_poll_run_switch_creates_new_key(tmp_path: Path) -> None:
    directory = write_fake(tmp_path, submit_mode="wait_input", get_mode="run_switch")
    home = tmp_path / "home"
    ledger = private_dir(tmp_path) / "ledger.sqlite"
    assert run_bridge(ledger, init_args(directory), home=home).returncode == 0
    envelope(run_bridge(
        ledger, ["submit", "--binding", "primary", "--request-id", "switch",
                 "--workspace", "demo", "--backend", "fake", "--input", "-"],
        home=home, stdin="切换运行",
    ))
    first = envelope(run_bridge(ledger, ["poll", "--binding", "primary"], home=home))
    second = envelope(run_bridge(ledger, ["poll", "--binding", "primary"], home=home))
    assert first["data"]["observed"][0]["status"] == "waiting_input"
    assert second["data"]["observed"][0]["status"] == "succeeded"
    assert first["data"]["observed"][0]["delivery_key"] != second["data"]["observed"][0]["delivery_key"]
    keys = {item["delivery_key"] for item in envelope(run_bridge(ledger, ["inbox", "--binding", "primary"], home=home))["data"]["items"]}
    assert keys == {second["data"]["observed"][0]["delivery_key"]}
    with sqlite3.connect(ledger) as connection:
        assert connection.execute("SELECT delivery_state FROM deliveries WHERE delivery_key=?",
                                  (first["data"]["observed"][0]["delivery_key"],)).fetchone()[0] == "superseded"


def test_poll_continues_after_one_task_error(tmp_path: Path) -> None:
    directory = write_fake(tmp_path)
    home = tmp_path / "home"
    ledger = private_dir(tmp_path) / "ledger.sqlite"
    assert run_bridge(ledger, init_args(directory), home=home).returncode == 0
    first = envelope(run_bridge(
        ledger, ["submit", "--binding", "primary", "--request-id", "a",
                 "--workspace", "demo", "--backend", "fake", "--input", "-"],
        home=home, stdin="A",
    ))
    second = envelope(run_bridge(
        ledger, ["submit", "--binding", "primary", "--request-id", "b",
                 "--workspace", "demo", "--backend", "fake", "--input", "-"],
        home=home, stdin="B",
    ))
    state = load_fake_state(directory)
    state["get_fail_task"] = first["data"]["task_id"]
    save_fake_state(directory, state)
    polled = envelope(run_bridge(ledger, ["poll", "--binding", "primary"], home=home))
    assert polled["ok"] is True
    assert any(item["task_id"] == second["data"]["task_id"] for item in polled["data"]["observed"])
    assert any(item["code"] == "BACKEND_UNAVAILABLE" for item in polled["data"]["errors"])


def test_cli_invalid_json_and_timeout_do_not_leak(tmp_path: Path) -> None:
    directory = write_fake(tmp_path, submit_mode="invalid_json")
    home = tmp_path / "home"
    ledger = private_dir(tmp_path) / "ledger.sqlite"
    assert run_bridge(ledger, init_args(directory), home=home).returncode == 0
    leaked = envelope(run_bridge(
        ledger, ["submit", "--binding", "primary", "--request-id", "leak",
                 "--workspace", "demo", "--backend", "fake", "--input", "-"],
        home=home, stdin="秘密提示词不应出现",
    ))
    raw = json.dumps(leaked, ensure_ascii=False)
    assert leaked["ok"] is False
    assert leaked["error"]["code"] == "CORE_RESULT_UNKNOWN"
    assert "SECRET_VALUE_XYZ" not in raw
    assert "traceback" not in raw.lower()
    assert "秘密提示词不应出现" not in raw

    state = load_fake_state(directory)
    state["submit_mode"] = "timeout"
    save_fake_state(directory, state)
    timed = envelope(run_bridge(
        ledger, ["--timeout", "0.3", "submit", "--binding", "primary", "--request-id", "slow",
                 "--workspace", "demo", "--backend", "fake", "--input", "-"],
        home=home, stdin="超时提示", timeout=10,
    ))
    assert timed["error"]["code"] == "CORE_RESULT_UNKNOWN"
    assert "超时提示" not in json.dumps(timed, ensure_ascii=False)
    assert timed["error"].get("retryable") is True


def test_claim_ack_and_unconfirmed_resolve(tmp_path: Path) -> None:
    directory = write_fake(tmp_path)
    home = tmp_path / "home"
    ledger = private_dir(tmp_path) / "ledger.sqlite"
    assert run_bridge(ledger, init_args(directory), home=home).returncode == 0
    envelope(run_bridge(
        ledger, ["submit", "--binding", "primary", "--request-id", "deliver",
                 "--workspace", "demo", "--backend", "fake", "--input", "-"],
        home=home, stdin="交付样例",
    ))
    polled = envelope(run_bridge(ledger, ["poll", "--binding", "primary"], home=home))
    key = polled["data"]["observed"][0]["delivery_key"]
    claimed = envelope(run_bridge(ledger, ["claim", "--binding", "primary", "--delivery-key", key], home=home))
    assert claimed["data"]["delivery_state"] == "sending"
    assert claimed["data"]["already_claimed"] is False
    assert claimed["data"]["destination"] == "chat-1"
    digest = claimed["data"]["content_sha256"]
    assert digest == hashlib.sha256(claimed["data"]["content"].encode()).hexdigest()
    again = envelope(run_bridge(ledger, ["claim", "--binding", "primary", "--delivery-key", key], home=home))
    assert again["data"]["already_claimed"] is True
    assert again["data"]["delivery_state"] == "sending"
    restored = envelope(run_bridge(
        ledger, ["resolve", "--binding", "primary", "--delivery-key", key, "--not-sent"], home=home,
    ))
    assert restored["data"]["delivery_state"] == "pending"
    claimed2 = envelope(run_bridge(ledger, ["claim", "--binding", "primary", "--delivery-key", key], home=home))
    acked = envelope(run_bridge(
        ledger, ["ack", "--binding", "primary", "--delivery-key", key,
                 "--message-id", "msg-1", "--content-sha256", digest],
        home=home,
    ))
    assert acked["data"]["host_reported_delivered"] is True
    assert acked["data"]["core_acknowledged"] is False
    assert acked["data"]["chat_visibility"] == "not_verified"
    assert acked["data"]["exactly_once"] is False
    replay = envelope(run_bridge(
        ledger, ["ack", "--binding", "primary", "--delivery-key", key,
                 "--message-id", "msg-1", "--content-sha256", digest],
        home=home,
    ))
    assert replay["ok"] is True and replay["data"]["reused"] is True
    status = envelope(run_bridge(ledger, ["status", "--binding", "primary"], home=home))["data"]
    assert status["deliveries"]["host_reported_delivered"] == 1
    assert status["needs_followup"] is True
    observed = envelope(run_bridge(ledger, ["poll", "--binding", "primary"], home=home))["data"]
    assert observed["needs_followup"] is False
    assert claimed2["data"]["summary"]
    inbox = envelope(run_bridge(ledger, ["inbox", "--binding", "primary"], home=home))
    assert inbox["data"]["items"] == []


def test_wrong_destination_key_hash_rejected(tmp_path: Path) -> None:
    directory = write_fake(tmp_path)
    home = tmp_path / "home"
    ledger = private_dir(tmp_path) / "ledger.sqlite"
    assert run_bridge(ledger, init_args(directory), home=home).returncode == 0
    conflict = envelope(run_bridge(ledger, init_args(directory, destination="chat-2"), home=home))
    assert conflict["error"]["code"] == "BINDING_CONFLICT"
    envelope(run_bridge(
        ledger, ["submit", "--binding", "primary", "--request-id", "n1",
                 "--workspace", "demo", "--backend", "fake", "--input", "-"],
        home=home, stdin="拒绝样例",
    ))
    key = envelope(run_bridge(ledger, ["poll", "--binding", "primary"], home=home))["data"]["observed"][0]["delivery_key"]
    missing = envelope(run_bridge(
        ledger, ["claim", "--binding", "primary", "--delivery-key", "ab" * 32], home=home,
    ))
    assert missing["error"]["code"] == "NOT_FOUND"
    claimed = envelope(run_bridge(ledger, ["claim", "--binding", "primary", "--delivery-key", key], home=home))
    bad_hash = envelope(run_bridge(
        ledger, ["ack", "--binding", "primary", "--delivery-key", key,
                 "--message-id", "msg", "--content-sha256", "cd" * 32],
        home=home,
    ))
    assert bad_hash["error"]["code"] == "HASH_MISMATCH"
    other = envelope(run_bridge(
        ledger, ["ack", "--binding", "missing", "--delivery-key", key,
                 "--message-id", "msg", "--content-sha256", claimed["data"]["content_sha256"]],
        home=home,
    ))
    assert other["error"]["code"] == "NOT_FOUND"
    extra = envelope(run_bridge(
        ledger, ["init", "--binding", "second", "--asterun", str(directory / "asterun"),
                 "--config", str(directory / "config.json"), "--core-state", str(directory / "core-state"),
                 "--location", "host-cloud", "--workspace", "demo", "--backend", "fake",
                 "--destination", "chat-9"],
        home=home,
    ))
    assert extra["ok"] is True
    crossed = envelope(run_bridge(
        ledger, ["claim", "--binding", "second", "--delivery-key", key], home=home,
    ))
    assert crossed["error"]["code"] == "NOT_FOUND"


def test_private_files_and_symlink_rejected(tmp_path: Path) -> None:
    directory = write_fake(tmp_path)
    home = tmp_path / "home"
    private = private_dir(tmp_path)
    linked = private / "ledger.link"
    target = private / "elsewhere.sqlite"
    target.write_bytes(b"keep")
    linked.symlink_to(target)
    linked_result = envelope(run_bridge(linked, init_args(directory), home=home))
    assert linked_result["error"]["code"] == "PATH_INVALID"
    assert target.read_bytes() == b"keep"

    asterun_link = private / "asterun"
    asterun_link.symlink_to(directory / "asterun")
    ledger = private / "ledger.sqlite"
    bad_bin = envelope(run_bridge(
        ledger,
        ["init", "--binding", "primary", "--asterun", str(asterun_link),
         "--config", str(directory / "config.json"), "--core-state", str(directory / "core-state"),
         "--location", "local", "--workspace", "demo", "--backend", "fake",
         "--destination", "chat-1"],
        home=home,
    ))
    assert bad_bin["error"]["code"] == "PATH_INVALID"
    with sqlite3.connect(ledger) as connection:
        assert connection.execute("SELECT COUNT(*) FROM bindings").fetchone()[0] == 0

    open_dir = tmp_path / "open"
    open_dir.mkdir(mode=0o755)
    if open_dir.stat().st_mode & 0o077:
        open_ledger = envelope(run_bridge(open_dir / "ledger.sqlite", init_args(directory), home=home))
        assert open_ledger["error"]["code"] == "PATH_INVALID"


def test_help_and_invalid_args_are_envelopes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True, text=True, timeout=5, env=bridge_home_env(home),
    )
    body = json.loads(result.stdout)
    assert body["ok"] is True and body["data"]["help"] is True
    bad = subprocess.run(
        [sys.executable, str(SCRIPT), "--ledger", str(private_dir(tmp_path) / "x.sqlite")],
        capture_output=True, text=True, timeout=5, env=bridge_home_env(home),
    )
    payload = json.loads(bad.stdout)
    assert payload["ok"] is False and payload["error"]["code"] == "INVALID_REQUEST"


def write_real_asterun(path: Path) -> Path:
    script = path / "asterun"
    script.write_text(
        f"#!{sys.executable}\n"
        "import runpy, sys\n"
        f"sys.path.insert(0, {str(SRC)!r})\n"
        "sys.argv[0] = 'asterun'\n"
        "runpy.run_module('asterun', run_name='__main__')\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    return script


def wait_socket(path: Path, process: subprocess.Popen, seconds: float = 5) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            out, err = process.communicate(timeout=5)
            raise AssertionError((process.returncode, out, err))
        time.sleep(0.02)
    raise AssertionError("resident core socket was not created")


def test_resident_fake_core_cli_integration() -> None:
    # Unix socket paths must fit the platform limit, including on macOS.
    with tempfile.TemporaryDirectory(prefix="gb-core-", dir=Path("/tmp").resolve()) as directory:
        isolated_env = Path(directory)
        workspace_root = isolated_env / "workspace"
        workspace_root.mkdir()
        exercise_resident_fake_core(isolated_env, workspace_root)


def exercise_resident_fake_core(isolated_env: Path, workspace_root: Path) -> None:
    config = write_config(isolated_env / "config.json", workspace_root)
    state = isolated_env / "core-state"
    binary = write_real_asterun(isolated_env)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(isolated_env / "home"),
        "ASTERUN_HOME": str(isolated_env / "asterun-home"),
        "LANG": "C",
        "LC_ALL": "C",
        "ASTERUN_ACCOUNT_REF": "account:test",
        "ASTERUN_RUNTIME_REF": "host:test",
    }
    (isolated_env / "home").mkdir(exist_ok=True)
    (isolated_env / "asterun-home").mkdir(exist_ok=True)
    process = subprocess.Popen(
        [str(binary), "--config", str(config), "--state-dir", str(state), "serve"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    try:
        wait_socket(state / "asterun.sock", process)
        ledger = isolated_env / "private"
        ledger.mkdir(mode=0o700)
        db = ledger / "ledger.sqlite"
        init = subprocess.run(
            [sys.executable, str(SCRIPT), "--ledger", str(db),
             "init", "--binding", "primary", "--asterun", str(binary),
             "--config", str(config), "--core-state", str(state),
             "--location", "local", "--workspace", "demo", "--backend", "fake",
             "--destination", "chat-live"],
            capture_output=True, text=True, timeout=20, env=isolated_env_env(isolated_env),
        )
        init_body = json.loads(init.stdout)
        assert init.returncode == 0, init.stderr + init.stdout
        assert init_body["ok"] is True
        assert init_body["data"]["configuration"]["valid"] is True
        assert init_body["data"]["callable"] is False
        prompt = "resident fake 集成"
        submitted = subprocess.run(
            [sys.executable, str(SCRIPT), "--ledger", str(db),
             "submit", "--binding", "primary", "--request-id", "live-1",
             "--workspace", "demo", "--backend", "fake", "--input", "-"],
            input=prompt, capture_output=True, text=True, timeout=20,
            env=isolated_env_env(isolated_env),
        )
        submit_body = json.loads(submitted.stdout)
        assert submitted.returncode == 0, submitted.stderr + submitted.stdout
        assert submit_body["ok"] is True
        assert submit_body["data"]["task_id"]
        polled = subprocess.run(
            [sys.executable, str(SCRIPT), "--ledger", str(db), "poll", "--binding", "primary"],
            capture_output=True, text=True, timeout=20, env=isolated_env_env(isolated_env),
        )
        poll_body = json.loads(polled.stdout)
        assert polled.returncode == 0, polled.stderr + polled.stdout
        assert poll_body["ok"] is True
        assert poll_body["data"]["observed"]
        assert poll_body["data"]["observed"][0]["status"] in {"succeeded", "running", "dispatching"}
        inbox = subprocess.run(
            [sys.executable, str(SCRIPT), "--ledger", str(db), "inbox", "--binding", "primary"],
            capture_output=True, text=True, timeout=20, env=isolated_env_env(isolated_env),
        )
        inbox_body = json.loads(inbox.stdout)
        assert inbox_body["ok"] is True
        deadline = time.monotonic() + 5
        while not inbox_body["data"]["items"] and time.monotonic() < deadline:
            time.sleep(0.05)
            envelope(run_bridge(db, ["poll", "--binding", "primary"], home=isolated_env / "home"))
            inbox_body = envelope(run_bridge(db, ["inbox", "--binding", "primary"], home=isolated_env / "home"))
        item = inbox_body["data"]["items"][0]
        assert item["status"] == "succeeded"
        # The fake backend intentionally echoes its input as the final answer.
        # The bridge exports that answer, without the original task/prompt object.
        assert item["summary"] == prompt
        assert "task" not in item["evidence"] and "prompt" not in item["evidence"]
        assert hashlib.sha256(item["content"].encode()).hexdigest() == item["content_sha256"]
        claimed = envelope(run_bridge(db, ["claim", "--binding", "primary", "--delivery-key", item["delivery_key"]], home=isolated_env / "home"))
        assert claimed["data"]["already_claimed"] is False
        acknowledged = envelope(run_bridge(db, ["ack", "--binding", "primary", "--delivery-key", item["delivery_key"],
                                               "--message-id", "fake-host-receipt", "--content-sha256", item["content_sha256"]], home=isolated_env / "home"))
        assert acknowledged["data"]["delivery_state"] == "host_reported_delivered"
        completed = envelope(run_bridge(db, ["poll", "--binding", "primary"], home=isolated_env / "home"))
        assert completed["data"]["needs_followup"] is False
    finally:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=5)


def isolated_env_env(root: Path) -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(root / "unused-home"),
        "LANG": "C",
        "LC_ALL": "C",
    }
