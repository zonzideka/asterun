"""本地协议测试进程；不导入 Asterun、不访问账户或网络。"""
import json
import os
import sys
import threading
import time
from pathlib import Path

state = Path.cwd() / "native.json"
audit = Path.cwd() / "native-calls.jsonl"
threads = json.loads(state.read_text()) if state.exists() else {}
lock = threading.RLock()
pending = {}


def emit(message):
    with lock:
        print(json.dumps(message), flush=True)


def save():
    with lock:
        state.write_text(json.dumps(threads))


def finish(thread_id, turn, status="completed"):
    with lock:
        if turn["status"] != "inProgress":
            return
        turn["status"] = status
        turn["items"] = [{"type": "agentMessage", "text": "隔离进程已完成"}]
        if turn.get("phased"):
            turn["items"] = [
                {"id": "progress", "type": "agentMessage", "phase": "commentary", "text": "正在核对代码"},
                {"id": "final", "type": "agentMessage", "phase": "final_answer", "text": '{"answer":42}'},
            ]
        save()
        emit({"method": "item/agentMessage/delta", "params": {"threadId": thread_id, "turnId": turn["id"], "delta": "隔离进程已完成"}})
        emit({"method": "turn/completed", "params": {"threadId": thread_id, "turn": turn}})


for line in sys.stdin:
    message = json.loads(line)
    method, params = message.get("method"), message.get("params") or {}
    with audit.open("a") as stream:
        stream.write(json.dumps(message) + "\n")
    if not method:
        thread_id, turn = pending.pop(str(message["id"]))
        finish(thread_id, turn, "completed" if message["result"]["decision"] == "accept" else "interrupted")
        continue
    if "id" not in message:
        continue
    result = {}
    if method == "initialize":
        result = {"userAgent": "isolated-test-server"}
    elif method == "thread/start":
        thread_id = f"native-thread-{len(threads) + 1}"
        threads[thread_id] = {"id": thread_id, "cwd": str(Path.cwd()), "turns": []}
        save()
        result = {"thread": threads[thread_id]}
    elif method in {"thread/resume", "thread/read"}:
        result = {"thread": threads[params["threadId"]]}
    elif method == "turn/start":
        thread_id = params["threadId"]
        turns = threads[thread_id]["turns"]
        turn = {"id": f"native-turn-{len(turns) + 1}", "status": "inProgress", "items": []}
        turn["phased"] = "phase-output" in params["input"][0]["text"]
        turns.append(turn)
        save()
        text = params["input"][0]["text"]
        if text.startswith("修复以下") and "quality-fixture" in text:
            (Path.cwd() / "note.txt").write_text("answer = 42\n")
        if "withheld-id" in text:
            break
        emit({"id": message["id"], "result": {"turn": turn}})
        emit({"method": "turn/started", "params": {"threadId": thread_id, "turn": turn}})
        if "with-token-usage" in text:
            emit({"method": "thread/tokenUsage/updated", "params": {
                "threadId": thread_id, "turnId": turn["id"], "tokenUsage": {
                    "total": {"inputTokens": 100, "cachedInputTokens": 80, "outputTokens": 20,
                              "reasoningOutputTokens": 8, "totalTokens": 120},
                    "last": {"inputTokens": 10, "cachedInputTokens": 0, "outputTokens": 2,
                             "reasoningOutputTokens": 1, "totalTokens": 12}}}})
        if "disconnect" in text:
            break
        if "approval" in text:
            pending["native-approval"] = (thread_id, turn)
            emit({"id": "native-approval", "method": "item/commandExecution/requestApproval", "params": {
                "threadId": thread_id, "turnId": turn["id"], "itemId": "test-command", "startedAtMs": 0,
                "command": "printf test", "cwd": str(Path.cwd())}})
        else:
            timer = threading.Timer(10 if "slow" in text else 0.15, finish, args=(thread_id, turn))
            timer.daemon = True
            timer.start()
        continue
    elif method == "turn/interrupt":
        turn = next(t for t in threads[params["threadId"]]["turns"] if t["id"] == params["turnId"])
        finish(params["threadId"], turn, "interrupted")
    emit({"id": message["id"], "result": result})
