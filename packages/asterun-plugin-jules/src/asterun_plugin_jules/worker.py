"""一次 NDJSON RPC；静态方法不导入HTTP 客户端。"""
import json
from pathlib import Path
import sys
from .runtime import execute, Rejected
PROTOCOL = "asterun-worker/v1"
MAX_FRAME = 256 * 1024
_METHODS = {"plugin.describe", "connection.inspect", "capability.prepare", "execution.start",
            "execution.observe", "execution.cancel", "execution.reconcile", "usage.observe"}
def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Rejected("invalid_request")
        result[key] = value
    return result

def handle(method, input, context):
    if method == "plugin.describe":
        return {"manifest": json.loads(Path(__file__).with_name("manifest.json").read_text())}
    if method == "connection.inspect":
        return {"authenticated": "not_verified", "billing_verified": False, "runtime": "remote"}
    if method == "usage.observe":
        return {"supported": False, "quota": "unsupported", "remaining": None}
    if method == "capability.prepare":
        return {"prepared": False, "reason": "core_preparation_required"}
    return execute(method, input, context)

def main():
    request_id = None
    try:
        line = sys.stdin.buffer.readline(MAX_FRAME + 1)
        if len(line) > MAX_FRAME or not line.endswith(b"\n"):
            raise Rejected("invalid_request")
        request = json.loads(line.decode("utf-8"), object_pairs_hook=_unique,
                             parse_constant=lambda _: (_ for _ in ()).throw(Rejected("invalid_request")))
        if not isinstance(request, dict) or set(request) != {"jsonrpc", "id", "method", "params"}:
            raise Rejected("invalid_request")
        request_id = request["id"]
        if (type(request_id) not in {str, int} or request["jsonrpc"] != "2.0"
                or not isinstance(request["method"], str) or request["method"] not in _METHODS):
            raise Rejected("invalid_request")
        params = request["params"]
        if (not isinstance(params, dict) or set(params) - {"protocol_version", "input", "context"}
                or params.get("protocol_version") != PROTOCOL or not isinstance(params.get("input", {}), dict)
                or not isinstance(params.get("context", {}), dict)):
            raise Rejected("invalid_request")
        result = handle(request["method"], params.get("input", {}), params.get("context", {}))
        response = {"jsonrpc": "2.0", "id": request_id, "result": {"protocol_version": PROTOCOL, **result}}
    except (Rejected, ValueError, TypeError, KeyError, RecursionError, UnicodeError):
        response = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32600, "message": "invalid_request"}}
    except Exception:
        # Unexpected errors do not claim failed-before-submit: host retains unknown.
        response = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": "worker_error"}}
    print(json.dumps(response, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
