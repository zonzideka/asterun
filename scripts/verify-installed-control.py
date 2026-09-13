#!/usr/bin/env python3
"""验证独立 wheel 的常驻 CLI/MCP 路径；Grok 须显式 --live 且已独立授权。"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venv", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--backend", choices=("fake", "grok"), default="fake")
    parser.add_argument("--grok-bin", type=Path)
    parser.add_argument("--grok-home", type=Path)
    parser.add_argument("--grok-model", default="grok-4.6")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    work, venv, output = (path.absolute() for path in (args.work_dir, args.venv, args.output))
    state, restored, config = work / "state", work / "restored", work / "config.json"
    python, cli = venv / "bin/python", venv / "bin/asterun"
    report = {"status": "running", "started_at": datetime.now(timezone.utc).isoformat(),
              "backend": args.backend, "live_requested": args.live, "tasks": [],
              "scope": ["installed-wheel", "resident-cli", "resident-mcp", "idempotency", "events",
                        "artifact-integrity", "completion-evidence", "restart", "backup-restore"],
              "native_cancel_verified": False, "native_resume_verified": False,
              "native_client_verified": False, "provider_account_identity_verified": False,
              "native_process_count": "unknown", "actual_model": "unknown"}
    service, log = None, None
    checked, replays = [], []

    def save():
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def execute(argv, payload=None, timeout=30):
        result = subprocess.run([str(arg) for arg in argv], cwd=work, env=env,
                                input=payload, text=True, capture_output=True, timeout=timeout)
        if result.returncode:
            # Native diagnostics may contain account details; keep them local, outside the report.
            with (work / "commands.log").open("a", encoding="utf-8") as diagnostics:
                diagnostics.write(result.stdout + result.stderr + "\n")
            raise RuntimeError(f"命令退出码 {result.returncode}，参见工作目录 commands.log")
        return result.stdout

    def installed(code, payload=None):
        return json.loads(execute([python, "-c", code], None if payload is None else json.dumps(payload)))

    def command_line(arguments, state_dir=state, connect=True):
        return [cli, "--config", config, "--state-dir", state_dir, *(["--connect"] if connect else []), *arguments]

    def call(*arguments, state_dir=state, connect=True):
        reply = json.loads(execute(command_line(arguments, state_dir, connect)))
        if not reply.get("ok"):
            raise RuntimeError("Asterun 拒绝请求：" + str(reply.get("error", {}).get("code", "unknown")))
        return reply["data"]

    def mcp(name, arguments):
        messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "installed-control-verification", "version": "1"}}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": name, "arguments": arguments}}]
        replies = [json.loads(line) for line in execute(command_line(["mcp"]),
                    "".join(json.dumps(value) + "\n" for value in messages)).splitlines()]
        assert [value.get("id") for value in replies] == [1, 2], "MCP 响应序列错误"
        assert "result" in replies[0] and "result" in replies[1], "MCP 协议错误"
        result = replies[1]["result"]
        assert not result.get("isError"), "MCP 工具错误"
        response = json.loads(result["content"][0]["text"])
        assert response.get("ok"), "MCP 控制请求失败；不更换幂等键重试"
        return response["data"]

    def capture(value, definition):
        checked.append({"value": value, "definition": definition})
        return value

    def request(method, params, revision=0):
        return {"schema_version": "runner-control/v1", "request_id": "req_" + uuid4().hex,
                "idempotency_key": uuid4().hex, "expected_revision": revision,
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
                "method": method, "params": params}

    def submit(command, transport):
        capture(command, "Command")
        path = work / (command["request_id"] + ".json")
        path.write_text(json.dumps(command), encoding="utf-8")
        response = (mcp("control_command", {"command": command}) if transport == "mcp"
                    else call("control-command", "--request", path))
        return capture(response, "CommandResponse")

    def stop():
        nonlocal service
        if service is not None:
            if service.poll() is None:
                service.terminate()
                try:
                    service.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    service.kill()
                    service.wait(timeout=5)
            code, service = service.returncode, None
            assert code == 0, "常驻服务未正常退出"

    def start():
        nonlocal service
        service = subprocess.Popen(command_line(["serve"], connect=False), cwd=work, env=env,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        deadline = time.monotonic() + 20
        while not (state / "asterun.sock").exists():
            assert service.poll() is None and time.monotonic() < deadline, "常驻服务未就绪"
            time.sleep(0.1)
        capture(call("control-discovery"), "DiscoveryResponse")

    def snapshot(database):
        return installed("""
import json, sqlite3, sys
from pathlib import Path
from asterun.control_protocol import validate
p = json.load(sys.stdin)
for row in p['checked']:
    validate(row['value'], row['definition'])
c = sqlite3.connect(Path(p['database']).resolve().as_uri() + '?mode=ro', uri=True)
entities = [(kind, json.loads(payload)) for kind, payload in c.execute('SELECT kind,payload FROM control_entities')]
counts = dict(c.execute('SELECT kind,count(*) FROM control_entities GROUP BY kind'))
assert all(counts[k] == 2 for k in ('Task','Run','Action','Reservation')), counts
assert counts['Operation'] == 4, counts
for kind, value in entities:
    if kind == 'Reservation':
        assert value['status'] == 'SETTLED' and value['actual'] == [{'meter':'turns','amount':1,'billing_pool_ref':None}]
runs = [json.loads(row[0]) for row in c.execute('SELECT payload FROM runs')]
assert len(runs) == 2, '重复创建执行器 Run'
events = [json.loads(row[0]) for row in c.execute('SELECT payload FROM events')]
dispatches = [e for e in events if e['type'] == 'run_dispatching']
assert len(dispatches) == 2, '派发记录数量不是两条'
models = sorted({str(r.get('native', {}).get('actual_model')) for r in runs
                 if r.get('native', {}).get('actual_model')})
print(json.dumps({'entity_counts':counts, 'settled_turns':2, 'dispatch_records':2,
    'execution_run_ids':sorted(r['id'] for r in runs),
    'session_ids':sorted(r.get('native', {}).get('session_id','') for r in runs),
    'actual_models_from_receipts':models or ['unknown']}))
""", {"database": str(database), "checked": checked})

    try:
        if not __debug__:
            raise RuntimeError("验证脚本不能使用禁用断言的 Python -O 模式")
        assert not work.exists() or (work.is_dir() and not any(work.iterdir())), "--work-dir 必须不存在或为空"
        work.mkdir(parents=True, exist_ok=True)
        os.chmod(work, 0o700)
        assert len(os.fsencode(state / "asterun.sock")) < 104, "工作目录过长，请使用较短路径以支持 Unix socket"
        assert python.is_file() and cli.is_file(), "指定 venv 没有安装 Asterun"
        assert args.backend != "grok" or (args.live and args.grok_bin and args.grok_home), "Grok 必须指定 --live、--grok-bin、--grok-home"
        home, workspace = work / "home", work / "workspace"
        home.mkdir()
        workspace.mkdir()
        env = {"HOME": str(home), "ASTERUN_HOME": str(home / "asterun"), "PATH": str(venv / "bin") + ":/usr/local/bin:/usr/bin:/bin",
               "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "TZ": "UTC", "LANG": "C.UTF-8"}
        backend = {"kind": args.backend, "enabled": True}
        if args.backend == "grok":
            binary, native_home = args.grok_bin.absolute(), args.grok_home.absolute()
            assert binary.is_file() and os.access(binary, os.X_OK), "Grok 二进制不可执行"
            assert native_home.is_dir(), "Grok home 必须已由用户独立准备并授权"
            backend.update(bin=str(binary), home=str(native_home), model=args.grok_model)
            env["ASTERUN_RUN_GROK"] = "1"
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy", "SSL_CERT_FILE"):
                if key in os.environ:
                    env[key] = os.environ[key]
            report["requested_model"] = args.grok_model
        config.write_text(json.dumps({"schema_version": 1, "revision": 1,
            "workspaces": {"validation": {"root": str(workspace), "allow_non_git": True}},
            "backends": {args.backend: backend}, "default_backend": args.backend}), encoding="utf-8")
        report["installation"] = installed("""
import asterun, importlib.metadata, json, platform, sys
from pathlib import Path
from asterun.control_protocol import SCHEMA_SHA256, DRAFT_REVISION
d = importlib.metadata.distribution('asterun')
direct = json.loads(d.read_text('direct_url.json') or '{}')
assert not direct.get('dir_info', {}).get('editable'), 'editable 安装不满足验收'
print(json.dumps({'module':str(Path(asterun.__file__).resolve()), 'version':asterun.__version__,
 'python':sys.version, 'os':platform.platform(), 'schema_sha256':SCHEMA_SHA256, 'draft_revision':DRAFT_REVISION}))
""")
        assert Path(report["installation"]["module"]).is_relative_to(venv.resolve()), "Asterun 未从指定 venv 加载"
        log = (work / "service.log").open("a", encoding="utf-8")
        start()
        resources = call("control-resources")
        assert len(resources["resources"]) == 1, "验证仅允许一个隔离工作区"
        resource = resources["resources"][0]
        for transport in ("cli", "mcp"):
            token = "ASTERUN_" + uuid4().hex.upper()
            objective = "Do not use tools or read files. Reply with exactly this token and nothing else: " + token
            create = request("task.create", {"title": "独立安装验证 " + transport, "objective": objective,
                "bot_instance_id": None, "constraints": ["Only return the requested token; no tools or file access."],
                "resource_scopes": [{key: resource[key] for key in ("resource_id", "operations", "selector")}],
                "acceptance": [{"id": "acc_output", "validator_id": "val_output", "input_artifact_ids": [],
                    "required": True, "subject_revision_sha256": None}],
                "budget_limits": [{"meter": "turns", "limit": 1, "enforcement": "hard", "billing_pool_ref": None}]})
            created = submit(create, transport)
            task_id = created["result_ref"]
            task = capture(call("control-get", "Task", task_id), "Task")
            run_request = request("run.start", {"task_id": task_id, "workflow_id": "wf_execute",
                "workflow_revision": 1, "configuration_sha256": resources["configuration_sha256"]}, task["revision"])
            started = submit(run_request, transport)
            row = {"transport": transport, "task_id": task_id, "run_id": started["result_ref"], "expected_token": token}
            report["tasks"].append(row)
            save()
            deadline = time.monotonic() + (240 if args.live else 20)
            while True:
                run = capture(call("control-get", "Run", row["run_id"]), "Run")
                row["run_status"] = run["status"]
                if run["status"] in {"SUCCEEDED", "FAILED", "CANCELED", "UNKNOWN_REMOTE"}:
                    break
                assert time.monotonic() < deadline, "观察超时，保留原键且不自动重发"
                time.sleep(0.3)
            assert run["status"] == "SUCCEEDED", "运行未成功，保留原键且不自动重发"
            task = capture(call("control-get", "Task", task_id), "Task")
            assert task["status"] == "SUCCEEDED" and task["result_artifact_ids"]
            row["artifact_hashes"] = {}
            for artifact_id in task["result_artifact_ids"]:
                artifact = capture(call("control-get", "Artifact", artifact_id), "Artifact")
                content = call("control-content", artifact_id)
                assert hashlib.sha256(content["text"].encode()).hexdigest() == artifact["sha256"] == content["sha256"]
                assert content["text"].strip() == token if args.backend == "grok" else token in content["text"]
                row["artifact_hashes"][artifact_id] = artifact["sha256"]
            assert run["completion_evidence_ids"], "缺少完成证据"
            for evidence_id in run["completion_evidence_ids"]:
                assert capture(call("control-get", "Evidence", evidence_id), "Evidence")["verdict"] == "PASS"
            events, after = [], 0
            while True:
                page = call("control-events", task_id, "--after", str(after), "--limit", "3")
                if not page["events"]:
                    assert page["next_sequence"] == after
                    break
                events.extend(capture(event, "Event") for event in page["events"])
                after = page["next_sequence"]
            assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
            assert len({event["id"] for event in events}) == len(events)
            row.update(output_validated=True, artifact_hashes_validated=True, events=len(events), evidence=len(run["completion_evidence_ids"]))
            for command, response in ((create, created), (run_request, started)):
                command["request_id"] = "req_" + uuid4().hex
                replay = submit(command, transport)
                assert (replay["operation_id"], replay["result_ref"]) == (response["operation_id"], response["result_ref"])
                replays.append((command, response, transport))
            save()
        before = snapshot(state / "asterun.sqlite")
        if args.backend == "grok":
            assert all(before["session_ids"]) and len(set(before["session_ids"])) == 2, "两次 Grok 派发缺少独立原生会话回执"
        stop()
        start()
        for command, response, transport in replays:
            replay = submit(command, transport)
            assert (replay["operation_id"], replay["result_ref"]) == (response["operation_id"], response["result_ref"])
        assert snapshot(state / "asterun.sqlite") == before, "重启或幂等回读改变派发记录"
        archive = work / "state-backup.tar.gz"
        report["backup_schema"] = call("state-backup", "--output", archive)["schema_version"]
        stop()
        assert call("state-restore", "--input", archive, state_dir=restored, connect=False)["schema_version"] == report["backup_schema"]
        for row in report["tasks"]:
            for kind, key in (("Task", "task_id"), ("Run", "run_id")):
                assert call("control-get", kind, row[key], state_dir=restored, connect=False) == call("control-get", kind, row[key], connect=False)
            for artifact_id, digest in row["artifact_hashes"].items():
                content = call("control-content", artifact_id, state_dir=restored, connect=False)
                assert content["sha256"] == digest == hashlib.sha256(content["text"].encode()).hexdigest()
        assert snapshot(restored / "asterun.sqlite") == before, "恢复后的执行和预算记录不一致"
        report.update(status="passed", validation=before, schema_validated_records=len(checked),
                      restart_replay_verified=True, backup_restore_verified=True,
                      actual_model=before["actual_models_from_receipts"], live_model_response_verified=args.backend == "grok")
    except BaseException as error:
        report.update(status="failed", failure_type=type(error).__name__, failure=str(error)[:500],
                      retry_policy="保留本目录与原幂等键；未知结果不可改键自动重试")
    finally:
        try:
            stop()
        except BaseException as error:
            report.update(status="failed", cleanup_failure=str(error)[:300])
        if log:
            log.close()
        save()
    print(json.dumps({"status": report["status"], "report": str(output)}, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
