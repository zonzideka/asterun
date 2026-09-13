"""Fixed-scope protocol double: no account, network or executable tool calls."""
import json
from pathlib import Path
import sys

if sys.argv[1:] == ["--version"]:
    print("codex-cli 0.153.4")
    raise SystemExit
if "generate-json-schema" in sys.argv:
    root = Path(sys.argv[sys.argv.index("--out") + 1])
    (root / "v2").mkdir(parents=True)
    documents = {
        "v2/ThreadStartParams.json": {"properties": {"environments": {"type": ["array", "null"]}, "dynamicTools": {}}},
        "v2/ThreadResumeParams.json": {"properties": {"config": {}}},
        "v2/TurnStartParams.json": {"properties": {"environments": {"type": ["array", "null"]}}},
        "DynamicToolCallParams.json": {"required": ["arguments", "callId", "threadId", "tool", "turnId"]},
        "DynamicToolCallResponse.json": {"required": ["contentItems", "success"]},
    }
    for name, value in documents.items():
        (root / name).write_text(json.dumps(value))
    raise SystemExit

config = {"mcp_servers": {"fixture": {"command": "never-run", "env": {"SECRET": "private-credential"}}}}
for index, value in enumerate(sys.argv):
    if value != "-c":
        continue
    key, raw = sys.argv[index + 1].split("=", 1)
    target = config
    parts = key.split(".")
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = json.loads(raw)

root = Path.cwd()
store = root / "scope-native.json"
threads = json.loads(store.read_text()) if store.exists() else {}
audit = root / "scope-calls.jsonl"
pending = {}


def send(value):
    print(json.dumps(value), flush=True)


def save():
    store.write_text(json.dumps(threads))


def finish(thread, turn, status="completed"):
    turn.update(status=status, items=[{"type": "agentMessage", "phase": "final_answer", "text": "reader finished"}])
    save()
    send({"method": "turn/completed", "params": {"threadId": thread["id"], "turn": turn}})


for raw in sys.stdin:
    msg = json.loads(raw)
    with audit.open("a") as output:
        output.write(json.dumps(msg) + "\n")
    method, params = msg.get("method"), msg.get("params", {})
    if method is None:
        thread, turn = pending.pop(str(msg["id"]))
        turn["tool_result"] = msg["result"]
        finish(thread, turn)
        continue
    if "id" not in msg:
        continue
    result = {}
    if method == "initialize":
        assert params["capabilities"]["experimentalApi"] is True
    elif method == "config/read":
        result = {"config": config}
    elif method in {"thread/start", "thread/resume"}:
        if method == "thread/start":
            ident = "scope-thread-" + str(len(threads) + 1)
            threads[ident] = {"id": ident, "cwd": str(root), "turns": [], "dynamicTools": params.get("dynamicTools")}
        else:
            ident = params["threadId"]
            assert "dynamicTools" not in params
        save()
        result = {"thread": threads[ident], "approvalPolicy": params.get("approvalPolicy"),
                  "sandbox": {"type": "readOnly" if params.get("sandbox") == "read-only" else "workspaceWrite"},
                  "instructionSources": []}
        if (root / "bad-permissions").exists():
            result["sandbox"] = {"type": "workspaceWrite"}
        if (root / "bad-instructions").exists():
            result["instructionSources"] = [str(root / "AGENTS.md")]
    elif method == "mcpServerStatus/list":
        result = {"data": [{"name": "fixture", "runtimeStatus": "disabled", "tools": {}}], "nextCursor": None}
        if (root / "bad-mcp").exists():
            result["data"][0]["runtimeStatus"] = "connected"
    elif method == "thread/read":
        result = {"thread": threads[params["threadId"]]}
    elif method == "turn/start":
        thread = threads[params["threadId"]]
        turn = {"id": "scope-turn-" + str(len(thread["turns"]) + 1), "status": "inProgress", "items": []}
        thread["turns"].append(turn)
        save()
        text = params["input"][0]["text"]
        if text == "violation-lost-reply":
            send({"method": "turn/started", "params": {"threadId": thread["id"], "turn": turn}})
            send({"id": "unanswered", "method": "item/tool/call", "params": {
                "threadId": thread["id"], "turnId": turn["id"], "callId": "unknown-call",
                "tool": "execute", "arguments": {}}})
            # Deliberately inconsistent native terminal tests fail-closed reconcile.
            finish(thread, turn)
            break
        send({"id": msg["id"], "result": {"turn": turn}})
        send({"method": "turn/started", "params": {"threadId": thread["id"], "turn": turn}})
        if text == "disconnect":
            break
        if text == "slow":
            continue
        request = {"threadId": thread["id"], "turnId": turn["id"], "callId": "call-" + turn["id"],
                   "tool": "asterun_read", "arguments": {"op": "read", "name": "note.txt"}}
        request_method = "item/tool/call"
        if text == "shell":
            request_method = "item/commandExecution/requestApproval"
            request = {"threadId": thread["id"], "turnId": turn["id"], "command": "/bin/zsh -lc cat note.txt"}
        elif text == "wrong-thread":
            request["threadId"] = "foreign"
        elif text == "wrong-tool":
            request["tool"] = "execute"
        elif text == "wrong-path":
            request["arguments"]["name"] = "../secret"
        pending["request"] = (thread, turn)
        send({"id": "request", "method": request_method, "params": request})
        continue
    elif method == "turn/interrupt":
        thread = threads[params["threadId"]]
        turn = next(t for t in thread["turns"] if t["id"] == params["turnId"])
        finish(thread, turn, "interrupted")
    send({"id": msg["id"], "result": result})
