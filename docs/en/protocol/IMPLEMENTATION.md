# Local runner-control/v1 implementation

[中文](../../protocol/IMPLEMENTATION.md) | [English](IMPLEMENTATION.md)

Asterun exposes a local runner-control/v1 profile through the CLI, stdio MCP, and a same-user Unix socket. Local tasks use `bot_instance_id: null`; the core runs independently. This page describes implemented behavior. The [original specification](../../protocol/runner-control-v1/SPEC.zh-CN.md) and [ADR 0011](../../adr/0011-runner-control-profile.md) are Chinese references for the full target contract and design decisions.

## Version and discovery

`schema_version` is `runner-control/v1`, and `draft_revision` is `2026-09-10.draft1`. These files retain their original bytes; their hashes are checked when read.

| File | SHA-256 |
|---|---|
| [JSON Schema](../../../src/asterun/schemas/runner-control-v1.schema.json) | `462f0956ca7f7997cc472ffb73e619111c66f25318cb383b677213880f23fe76` |
| [State machine](../../protocol/runner-control-v1/state-machines.json) | `99ceb7f0b8a30160d06085d0c2b645e618506a393941a7ebf78ff5c3bfa5ebb6` |

The package paths are `asterun/schemas/runner-control-v1.schema.json` and `asterun/schemas/state-machines.json`. Clients check both the draft revision and digest. `feature_profiles` includes `asterun-local-v1` and `schema-sha256:<digest>`; the auxiliary `control.resources` endpoint also returns `schema_sha256`. This representation preserves the original `DiscoveryResponse`, which has no digest field and rejects extensions.

`backend_ids` lists configured local backends. Backend status and live records provide installation, sign-in, invocation, and client visibility results.

## Supported behavior

| Capability | Local implementation |
|---|---|
| `task.create`, `task.amend` | Create a separate Task with at least one required acceptance condition. amend updates the objective, constraints, and acceptance of a READY task while preserving resource scope |
| `run.start`, `run.retry` | Use `wf_execute` revision 1 and the default backend; check Task revision and configuration, account, and host digests. retry creates a new Run for the latest failed Run |
| `run.pause`, `run.resume`, `run.cancel` | Persist desired state and wait for a safe boundary. Capsule resume remains unimplemented; cancellation requires backend termination evidence |
| `run.takeover`, `run.return_control` | Support human takeover and return; external orchestrator takeover remains unimplemented |
| `state.put` | Write `domain.*` after identity, CAS, and cross-object evidence checks; update the value digest, revision, and event atomically |
| `artifact.register` | Register an authorized staged upload after checking Task/Run/Assignment ownership, SHA, and byte count. Input uses a staged reference; the result is unverified |
| `grant.revoke` | Check Grant revision, revoke it, and recheck before dispatch |
| Resources | Bind one complete configured workspace per task, with `operations: ["agent.execute"]`, `selector: null`, and context `enforcement: advisory` |
| Budgets | Accept `turns`, `hard`, and `billing_pool_ref: null`; other hard meters return unsupported |
| Acceptance | `val_execution` checks normal execution completion; `val_output` also requires nonempty public output. Existing code quality workflows use their dedicated entry points |
| Identity | Use fixed `ns_local`; establish identity from the process, then check workspace policy and Grants |
| Events | Persist increasing per-Task sequence numbers, with at most 100 entries per page; return explicit errors for expired or future cursors |

The Schema defines `message.send`, `action.propose`, `approval.request`, `setup.answer`, and `setup.verify`. Their runtime support remains unimplemented, so discovery omits them. Remote tenant identity, HTTP routes, and SSE also belong to the full specification's scope.

Human takeover coordinates local orchestration ownership and waits for managed turns to end. Full workspace Capsules, resource hashes, and detection of external native client activity remain unimplemented. The complete live return-of-control conditions in C26 have not been validated.

A `cap_submit_turn` Action records one backend turn submission. Native permissions govern the provider's built-in tools; individual tool actions have not been connected to this layer's Action/outbox. Execution success and public-output checks supply execution evidence. Task checks and independent review establish business quality.

## Encoding, idempotency, and storage

The entry point uses strict UTF-8 JSON and validates duplicate keys, Unicode, finite numbers, JavaScript-safe integers, unknown fields, enums, UTC timestamps, and field limits. Floating-point numbers follow IEEE 754 and RFC 8785; micro-dollar amounts use integers.

Commands are limited to 1 MiB. Complete MCP/IPC messages have the same 1 MiB limit, including wrapper overhead. Text uploads and direct content reads are limited to 512 KiB.

The idempotency scope consists of namespace, authenticated identity, method, and `idempotency_key`. The SHA-256 input is the RFC 8785 JCS encoding of `schema_version`, `method`, `params`, `expected_revision`, and `expires_at`. A retry preserves the original semantics and expiry; its transport `request_id` may change. Authorization is checked before an idempotent readback. Expiry and CAS apply only to new commands. After a communication failure, read the original Operation or idempotency record first.

Standard entities, Operations, events, budgets, Grants, leases, the outbox, content blobs, and legacy records share SQLite. Admission and result persistence use separate transactions, with no transaction held while waiting on an external call. Unknown outcomes retain `UNKNOWN_REMOTE` and unresolved budget reservations. Recoverable tasks use an explicit state directory or a resident core.

The current database version is schema 6. Upgrading schema 1–5 first creates a consistent `asterun.schema-<old-version>.*.sqlite` snapshot, then initializes the current control and plugin tables. Legacy Task/Run records retain their meaning. `state-backup` includes state tables and blobs; restore preflight checks structure and content hashes. Workspace files, credentials, and native session directories need their own backups. For rollback, retain the newer database for reconciliation and run the older release against a separate copy of its pre-upgrade snapshot.

Discovery's `event_retention_seconds: 86400` declares a minimum retention period. Events currently remain in the database; automatic cleanup is unimplemented. Internal trimming boundaries identify expired cursors and return `CURSOR_EXPIRED`.

## Entry points

| Purpose | CLI | MCP tools |
|---|---|---|
| Discovery and resources | `control-discovery`, `control-resources` | `control_discovery`, `control_resources` |
| Standard command | `control-command --request <JSON-file>` | `control_command`, with `{"command": <standard-envelope>}` |
| Entities and events | `control-get <Kind> <id>`, `control-events <task_id> --after <sequence>` | `control_get`, `control_events` |
| Upload and content | `control-upload <task_id> --request <JSON-file-with-text>`, `control-content <artifact_id>` | `control_upload`, `control_content` |

Standard data is returned in Asterun's outer `Envelope.data`; errors appear in `Envelope.error`. MCP wraps the Envelope as JSON text inside the JSON-RPC tools/call response's `result.content`. stdio uses newline-delimited JSON. After a command returns, read Run/Task to confirm execution state.

For long-running work, start `asterun --config <config> --state-dir <directory> serve` and add `--connect` to caller commands. stdio MCP can also own a resident executor. A one-shot CLI advances only the phases it can execute during that invocation.

## Offline example

Create an environment from the repository root, then save the Python code below as `/tmp/asterun-control-demo.py`. Each run creates a temporary workspace and state directory, uses the fake backend, and cleans up on exit. The example retains its fixed Chinese task text for reproducibility.

```sh
python3 -m venv /tmp/asterun-demo-venv
/tmp/asterun-demo-venv/bin/pip install -e '.[test]'
```

```python
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

transport = os.environ.get("ASTERUN_DEMO_TRANSPORT", "cli")
assert transport in {"cli", "mcp"}
with tempfile.TemporaryDirectory(prefix="asterun-control-demo-") as directory:
    root = Path(directory)
    workspace = root / "workspace"
    workspace.mkdir()
    config = root / "config.json"
    config.write_text(json.dumps({
        "schema_version": 1, "revision": 1,
        "workspaces": {"demo": {"root": str(workspace), "allow_non_git": True}},
        "backends": {"fake": {"kind": "fake", "enabled": True}},
        "default_backend": "fake",
    }), encoding="utf-8")
    env = dict(os.environ, ASTERUN_ACCOUNT_REF="account:demo", ASTERUN_RUNTIME_REF="host:demo")
    base = [sys.executable, "-m", "asterun", "--config", str(config),
            "--state-dir", str(root / "state")]
    process = None
    rpc_sequence = 0

    def rpc(method, params):
        global rpc_sequence
        rpc_sequence += 1
        message = {"jsonrpc": "2.0", "id": rpc_sequence, "method": method, "params": params}
        process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        process.stdin.flush()
        response = json.loads(process.stdout.readline())
        assert "error" not in response, response
        return response["result"]

    def call(method, arguments=None):
        arguments = arguments or {}
        if transport == "mcp":
            result = rpc("tools/call", {"name": method.replace(".", "_"), "arguments": arguments})
            envelope = json.loads(result["content"][0]["text"])
        else:
            args = [method.replace(".", "-")]
            if method == "control.command":
                request = root / "command.json"
                request.write_text(json.dumps(arguments["command"], ensure_ascii=False), encoding="utf-8")
                args += ["--request", str(request)]
            elif method == "control.get":
                args += [arguments["kind"], arguments["id"]]
            elif method == "control.events":
                args += [arguments["task_id"]]
            completed = subprocess.run(base + args, env=env, text=True, capture_output=True)
            assert completed.returncode == 0, completed.stdout + completed.stderr
            envelope = json.loads(completed.stdout)
        assert envelope["ok"], envelope.get("error")
        return envelope["data"]

    def command(method, params, revision=0):
        expires = datetime.now(timezone.utc) + timedelta(minutes=10)
        return call("control.command", {"command": {
            "schema_version": "runner-control/v1", "request_id": "req_" + uuid4().hex,
            "idempotency_key": uuid4().hex, "expected_revision": revision,
            "expires_at": expires.isoformat().replace("+00:00", "Z"), "method": method, "params": params,
        }})

    try:
        if transport == "mcp":
            process = subprocess.Popen(base + ["mcp"], env=env, text=True,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "asterun-control-demo", "version": "1"}})
            process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
            process.stdin.flush()
        found = call("control.discovery")
        assert found["draft_revision"] == "2026-09-10.draft1"
        expected_sha = "462f0956ca7f7997cc472ffb73e619111c66f25318cb383b677213880f23fe76"
        assert "schema-sha256:" + expected_sha in found["feature_profiles"]
        resource = call("control.resources")["resources"][0]
        created = command("task.create", {
            "title": "协议离线示例", "objective": "输出一段 fake 验证结果", "bot_instance_id": None,
            "resource_scopes": [{key: resource[key] for key in ("resource_id", "operations", "selector")}],
            "constraints": [],
            "acceptance": [{"id": "acc_demo", "validator_id": "val_output", "input_artifact_ids": [],
                            "required": True, "subject_revision_sha256": None}],
            "budget_limits": [{"meter": "turns", "limit": 1, "enforcement": "hard", "billing_pool_ref": None}],
        })
        # Read the current Task revision and configuration digest before starting.
        task = call("control.get", {"kind": "Task", "id": created["result_ref"]})
        resources = call("control.resources")
        started = command("run.start", {"task_id": task["id"], "workflow_id": "wf_execute",
                          "workflow_revision": 1, "configuration_sha256": resources["configuration_sha256"]},
                          revision=task["revision"])
        deadline = time.monotonic() + 10
        while True:
            run = call("control.get", {"kind": "Run", "id": started["result_ref"]})
            if run["status"] in {"SUCCEEDED", "FAILED", "CANCELED"}:
                break
            assert time.monotonic() < deadline, run
            time.sleep(0.05)
        assert run["status"] == "SUCCEEDED", run
        for evidence_id in run["completion_evidence_ids"]:
            evidence = call("control.get", {"kind": "Evidence", "id": evidence_id})
            assert evidence["validator_id"] == "val_output" and evidence["verdict"] == "PASS"
        events = call("control.events", {"task_id": task["id"]})
        print(json.dumps({"transport": transport, "run_status": run["status"],
                          "evidence_ids": run["completion_evidence_ids"],
                          "last_event_sequence": events["next_sequence"], "backend": "fake"}, ensure_ascii=False))
    finally:
        if process is not None:
            process.stdin.close()
            process.wait(timeout=10)
            process.stdout.close()
```

Run both the CLI and MCP paths:

```sh
ASTERUN_DEMO_TRANSPORT=cli /tmp/asterun-demo-venv/bin/python /tmp/asterun-control-demo.py
ASTERUN_DEMO_TRANSPORT=mcp /tmp/asterun-demo-venv/bin/python /tmp/asterun-control-demo.py
```

This example checks local configuration, standard admission, persistence, fake dispatch, and execution Evidence. Real accounts, effective permissions, native resume, model quality, and client visibility are validated separately for each backend.
