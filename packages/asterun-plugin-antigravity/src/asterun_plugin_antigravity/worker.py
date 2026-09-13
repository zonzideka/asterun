"""单次受控 RPC 的 CLI 外壳；不登录、不回退、不创建第二个 job host。"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import sys
from threading import Event
import time

from ._vendor.command import build_command
from ._vendor.compat import AsterunError, RunId
from ._vendor.profile import build_environment, validate_home, validate_workspace_binding
from ._vendor.runtime import AntigravityRuntime

PROTOCOL = "asterun-worker/v1"
VERSION = "1.2.0"
MAX_FRAME = 256 * 1024
_OPTIONS = {"bin", "binary_sha256", "home", "model", "execution_enabled", "timeout_seconds"}
_METHODS = {"plugin.describe", "connection.inspect", "capability.prepare", "execution.start",
            "execution.observe", "execution.cancel", "execution.reconcile", "usage.observe"}
_UNSUPPORTED = {"native_resume": "unsupported", "approval": "unsupported", "quota": "unsupported",
                "cancel": "unsupported", "reconcile": "unsupported"}


class Rejected(Exception):
    """只包含固定分类，不能携带凭据、stderr 或底层错误文本。"""


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Rejected("invalid_request")
        result[key] = value
    return result


def _stamp():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _manifest():
    return json.loads(Path(__file__).with_name("manifest.json").read_text(encoding="utf-8"))


def _text_input(value):
    if (not isinstance(value, dict) or set(value) != {"text"} or not isinstance(value["text"], str)
            or not value["text"] or len(value["text"]) > 65536):
        raise Rejected("invalid_input")
    return value["text"]


def _binary(path, expected):
    if not isinstance(path, str) or not Path(path).is_absolute() or not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise Rejected("binary_pin_required")
    target = Path(path)
    try:
        if target.is_symlink() or target.resolve(strict=True) != target:
            raise Rejected("binary_path_invalid")
        info = target.stat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.getuid()} or info.st_mode & 0o022
                or not os.access(target, os.X_OK) or info.st_size > 1024 * 1024 * 1024):
            raise Rejected("binary_path_invalid")
        with target.open("rb") as handle:
            actual = hashlib.file_digest(handle, "sha256").hexdigest()
        if actual != expected:
            raise Rejected("binary_digest_mismatch")
    except OSError:
        raise Rejected("binary_unavailable") from None
    return target


def _version(binary, *, cwd, env):
    """只运行版本入口；有界读取，CLI 与当前 worker 保持同一进程组。"""
    proc = None
    output = bytearray()
    total = 0
    deadline = time.monotonic() + 3
    try:
        proc = subprocess.Popen([str(binary), "--version"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, env=env)
        with selectors.DefaultSelector() as selector:
            for stream in (proc.stdout, proc.stderr):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise Rejected("version_unavailable")
                for key, _ in selector.select(.05):
                    data = os.read(key.fileobj.fileno(), 4096)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(data)
                    if total > 4096:
                        raise Rejected("version_unavailable")
                    if key.fileobj is proc.stdout:
                        output.extend(data)
            proc.wait(timeout=max(.01, deadline - time.monotonic()))
        if proc.returncode != 0 or not re.fullmatch(r"(?:(?:Antigravity(?: CLI)?|agy)\s+)?v?1\.2\.0", output.decode("utf-8").strip(), re.IGNORECASE):
            raise Rejected("upstream_version_mismatch")
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
        raise Rejected("version_unavailable") from None
    finally:
        if proc is not None:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=.3)
            except (OSError, subprocess.TimeoutExpired):
                pass
            for stream in (proc.stdout, proc.stderr):
                try:
                    stream.close()
                except OSError:
                    pass


def _preflight(context):
    options = context.get("options")
    if not isinstance(options, dict) or set(options) - _OPTIONS:
        raise Rejected("invalid_options")
    if options.get("execution_enabled") is not True:
        raise Rejected("execution_not_enabled")
    if context.get("auth_mode") != "native_account" or not context.get("provider_account_ref"):
        raise Rejected("auth_mode_mismatch")
    if context.get("upstream_version") != VERSION:
        raise Rejected("upstream_version_mismatch")
    if context.get("capability") != "agent.execute":
        raise Rejected("capability_unsupported")
    if any(context.get(key) for key in ("provider_session_id", "provider_job_ref", "provider_turn_id")):
        raise Rejected("native_resume_unsupported")
    if any(not isinstance(context.get(key), str) or not context[key] for key in ("run_id", "task_id", "binding_id", "plan_id")):
        raise Rejected("execution_binding_required")
    timeout = options.get("timeout_seconds", 20)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 20:
        raise Rejected("invalid_timeout")
    home = options.get("home")
    if not isinstance(home, str) or not Path(home).is_absolute() or os.environ.get("HOME") != home:
        raise Rejected("native_home_mismatch")
    model = options.get("model")
    if not isinstance(model, str) or not model or any(char.isspace() or ord(char) < 32 for char in model):
        raise Rejected("model_required")
    try:
        home_path = validate_home(home)
        workspace = validate_workspace_binding(home_path, context.get("workspace_root"))
        if workspace != Path.cwd().resolve():
            raise Rejected("workspace_mismatch")
        # Exact read permission is required; a prompt is never the read-only boundary.
        settings = json.loads((home_path / ".gemini/antigravity-cli/settings.json").read_text())
        if settings["permissions"]["allow"] != [f"read_file({workspace})"]:
            raise Rejected("workspace_read_binding_required")
        env = build_environment(home_path, {key: value for key, value in os.environ.items()
                                           if key in {"PATH", "TMPDIR", "LANG", "LC_ALL"}})
        binary = _binary(options.get("bin"), options.get("binary_sha256"))
        command = [*build_command(binary, model), "--add-dir", str(workspace)]
        _version(binary, cwd=workspace, env=env)
        _binary(options["bin"], options["binary_sha256"])
    except AsterunError:
        raise Rejected("permission_profile_rejected") from None
    except (OSError, ValueError, TypeError, KeyError):
        raise Rejected("profile_unavailable") from None
    return command, env, workspace, model, timeout


def _mapped(native_result, context):
    native = native_result.get("native", {})
    session = native.get("backend_session_id")
    result = {"status": native_result["status"], "output": {"text": native_result.get("summary", "")},
              "support": dict(_UNSUPPORTED), "usage": [], "events": []}
    if session:
        result["provider_session_id"] = session
    if result["status"] == "cancelled":
        # A native process terminal is observed; no separate cancellation API is claimed.
        result["terminated_verified"] = True
    now = _stamp()
    if session:
        for meter, used in (("tokens", native.get("usage", {}).get("total_tokens")), ("turns", native.get("num_turns"))):
            if type(used) is int and used >= 0:
                result["usage"].append({"meter": meter, "used": used, "scope": "session_cumulative",
                    "scope_ref": session, "cumulative": True, "sequence": 0,
                    "cursor": f'{context["run_id"]}:{meter}:final', "observed_at": now})
    code = ("tool_permission_denied" if native.get("soft_denied") else
            "native_tool_failed" if native.get("tool_error_count") or native.get("step_error_count") else
            "native_run_failed" if result["status"] == "failed" else
            "native_result_unknown" if result["status"] == "pending_reconcile" else "native_terminal")
    result["events"] = [{"provider_event_id": f'{context["run_id"]}:final',
        "category": f"antigravity.{code}", "summary": code, "provider_sequence": 0,
        "observed_at": now, **({"provider_session_id": session} if session else {})}]
    return result


def handle(method, input, context):
    if method == "plugin.describe":
        return {"manifest": _manifest(), "support": dict(_UNSUPPORTED)}
    if method == "connection.inspect":
        return {"authenticated": "not_verified", "upstream_version": VERSION,
                "support": dict(_UNSUPPORTED), "billing_verified": False, "native_client_visible": "not_verified"}
    if method == "usage.observe":
        return {"supported": False, "quota": "unsupported", "remaining": None, "usage": []}
    if method in {"execution.observe", "execution.cancel", "execution.reconcile"}:
        return {"status": "pending_reconcile", "supported": False, "terminated_verified": False,
                "reason": "native_observation_unsupported", "support": dict(_UNSUPPORTED),
                **{key: context[key] for key in ("provider_session_id", "provider_job_ref", "provider_turn_id") if context.get(key)}}
    try:
        text = _text_input(input)
        if method == "capability.prepare":
            if context.get("capability") != "agent.execute":
                raise Rejected("capability_unsupported")
            return {"prepared": True, "side_effects": "native_session_and_subscription_usage",
                    "support": dict(_UNSUPPORTED)}
        command, env, cwd, model, timeout = _preflight(context)
        runtime = AntigravityRuntime(timeout=timeout)
        result = runtime.run(RunId(context["run_id"]), text, command=command, env=env, cwd=cwd,
                             publish=lambda item: None, stopping=Event(), expected_model=model)
        return _mapped(result, context)
    except Rejected as error:
        code = str(error)
        return {"status": "failed", "reason": str(error), "output": {"text": str(error)},
                "support": dict(_UNSUPPORTED), "usage": [], "events": [{
                    "provider_event_id": f'{context.get("run_id", "prepare")}:preflight',
                    "category": f"antigravity.preflight.{code}", "summary": code,
                    "provider_sequence": 0, "observed_at": _stamp()}]}


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
