"""没有核心依赖的真实 wheel worker；所有效果限于测试运行目录。"""
import json
from pathlib import Path
import sys

VERSION = "asterun-worker/v1"


def handle(method, params):
    data = params.get("input", {})
    context = params.get("context", {})
    if method == "plugin.describe":
        return {"manifest": json.loads(Path(__file__).with_name("manifest.json").read_text())}
    if method == "connection.inspect":
        return {"authenticated": "not_required", "is_real_connection": False}
    if method == "capability.prepare":
        return {"estimate": {"meter": "requests", "amount": 1}, "side_effects": False}
    if method == "usage.observe":
        return {"remaining": None, "source_kind": "unsupported"}
    if method == "execution.cancel":
        return {"status": "requested", "terminated_verified": False}
    if method == "execution.reconcile":
        return {"status": "unknown", "provider_job_ref": context.get("provider_job_ref")}
    if method == "execution.observe":
        return {"status": "succeeded", "output": {"text": "external observed"}}
    if method == "execution.start":
        mode = data.get("mode", "success")
        if mode == "crash":
            # 模拟远端已受理而响应丢失，只记录一次受理。
            with (Path.cwd() / "accepted.txt").open("a") as log:
                log.write("accepted\n")
            raise SystemExit(17)
        if mode == "stderr":
            print("untrusted credential=" + data.get("secret", "test-secret"), file=sys.stderr)
        if mode == "overflow":
            print("x" * (2 * 1024 * 1024), flush=True)
            raise SystemExit(0)
        if mode == "hang":
            import time
            time.sleep(10)
        return {"status": "succeeded", "output": {"text": data.get("text", "external fake")},
                "provider_job_ref": None, "events": [],
                "usage": {"meter": "requests", "amount": 1, "scope": "request"}}
    return {"unsupported": True}


def main():
    for line in sys.stdin.buffer:
        request = json.loads(line)
        if request.get("params", {}).get("protocol_version") != VERSION:
            response = {"jsonrpc": "2.0", "id": request.get("id"),
                        "error": {"code": -32602, "message": "incompatible protocol"}}
        else:
            result = handle(request["method"], request["params"])
            response = {"jsonrpc": "2.0", "id": request["id"],
                        "result": {"protocol_version": VERSION, **result}}
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
