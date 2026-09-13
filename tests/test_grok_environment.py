from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from asterun.application import Application
from asterun.backends.grok import (
    DEFAULT_GROK_BIN,
    DISPATCH_RECEIPT_KEY,
    LIVE_ENV,
    GrokAcpTransport,
    GrokBackend,
    discover_grok_bin,
    live_grok_enabled,
)
from asterun.config import BackendConfig, load_config
from asterun.errors import AsterunError, CONFIG_REQUIRED
from asterun.grok_profile import prepare_home
from asterun.ids import RunId, TaskId
from asterun.policy import Principal
from tests.conftest import write_config


def test_default_bin_is_unset_and_product_has_no_author_paths() -> None:
    assert DEFAULT_GROK_BIN is None
    source = Path(__file__).resolve().parents[1] / "src" / "asterun" / "backends" / "grok.py"
    text = source.read_text(encoding="utf-8")
    assert "/Users/" not in text
    assert "~/.grok/models_cache.json" not in text
    assert "novel-toolkit" not in text
    assert "keyworld" not in text
    assert "agent-runner__" not in text
    assert "asterun__*" in text


def test_import_does_not_read_home_grok_cache(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    cache_dir = home / ".grok"
    cache_dir.mkdir(parents=True)
    cache = cache_dir / "models_cache.json"
    cache.write_text('{"models": {"should-not-be-read": {}}}', encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GROK_MODELS_CACHE", raising=False)
    from asterun.backends import grok as grok_mod

    assert grok_mod.models_cache_path() is None
    assert grok_mod.known_grok_models() == set(grok_mod.FALLBACK_GROK_MODELS)


def test_live_flag_and_discover_bin_order(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(LIVE_ENV, raising=False)
    assert live_grok_enabled() is False
    monkeypatch.setenv(LIVE_ENV, "1")
    assert live_grok_enabled() is True
    explicit = tmp_path / "explicit-grok"
    env_bin = tmp_path / "env-grok"
    path_dir = tmp_path / "bin"
    empty_path = tmp_path / "empty-path"
    path_dir.mkdir()
    empty_path.mkdir()
    path_bin = path_dir / "grok"
    for item in (explicit, env_bin, path_bin):
        item.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        item.chmod(0o755)
    monkeypatch.setenv("GROK_BIN", str(env_bin))
    monkeypatch.setenv("PATH", str(path_dir))
    assert discover_grok_bin(str(explicit)) == explicit
    assert discover_grok_bin() == env_bin
    monkeypatch.delenv("GROK_BIN")
    assert discover_grok_bin() == path_bin
    monkeypatch.setenv("PATH", str(empty_path))
    assert discover_grok_bin() is None


def test_discovered_relative_binary_is_fixed_before_workspace_chdir(tmp_path, monkeypatch):
    binary = tmp_path / "grok"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    assert discover_grok_bin("./grok") == binary.resolve()


def test_inspect_and_default_verify_do_not_spawn(
    isolated_env: Path, workspace_root: Path, monkeypatch
) -> None:
    def boom(*_args, **_kwargs):
        raise AssertionError("inspect/verify 默认不得启动进程")

    monkeypatch.setattr("asterun.transport.subprocess.Popen", boom)
    path = write_config(
        isolated_env / "config.json",
        workspace_root,
        extra_backends={"grok": {"kind": "grok", "enabled": True}},
    )
    app = Application(load_config(path), principal=Principal("test:user", "test"))
    inspected = app.handle("backend.inspect", {"backend": "grok"})
    row = inspected.data["backends"][0]
    assert row["handshake_is_not_resume"] is True
    assert row["native_resume_verified"] is False
    capabilities = {item["name"]: item for item in row["capabilities"]}
    assert capabilities["resume_session"]["adapter_support"] == "unsupported"
    verified = app.handle("connection.verify", {"backend": "grok"})
    assert verified.ok
    assert verified.data["verified"] is False
    assert verified.data["handshake_is_not_resume"] is True


def test_binary_without_live_flag_is_auth_required(
    isolated_env: Path, workspace_root: Path, monkeypatch
) -> None:
    dummy = isolated_env / "dummy-grok"
    dummy.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    dummy.chmod(0o755)
    monkeypatch.setenv("GROK_BIN", str(dummy))
    path = write_config(
        isolated_env / "config.json",
        workspace_root,
        extra_backends={"grok": {"kind": "grok", "enabled": True}},
    )
    app = Application(load_config(path), principal=Principal("test:user", "test"))
    backend = app.backends["grok"]
    assert isinstance(backend, GrokBackend)
    assert backend.can_dispatch() is False
    assert backend.unavailable_error().code == "AUTH_REQUIRED"
    submitted = app.handle("task.submit", {"workspace": "demo", "backend": "grok", "text": "ping"})
    assert submitted.error["code"] == "AUTH_REQUIRED"
    assert backend.dispatch_count == 0
    assert app.store.tasks == {}


@pytest.mark.parametrize("home_kind", ["missing", "unprepared", "relative"])
def test_dispatch_rejects_unprepared_home_before_spawn(isolated_env, workspace_root, monkeypatch, home_kind):
    binary = isolated_env / "unused-grok"
    binary.write_text("#!/bin/sh\nexit 90\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv(LIVE_ENV, "1")
    home = isolated_env / "not-prepared"
    home.mkdir()
    home_value = {"missing": None, "unprepared": str(home), "relative": "not-prepared"}[home_kind]
    backend = GrokBackend(BackendConfig(name="grok", kind="grok", bin=str(binary), home=home_value))

    def forbidden(*args, **kwargs):
        raise AssertionError("未准备独立 HOME 时不能启动子进程")

    monkeypatch.setattr("asterun.transport.subprocess.Popen", forbidden)
    assert not backend.can_dispatch()
    result = backend.dispatch(TaskId("task-test"), RunId("run-test"), "", "ping", cwd=workspace_root)
    assert result["status"] == "failed" and result["terminated"] is True
    assert result["native"][DISPATCH_RECEIPT_KEY]["delivery"] == "not_sent"


def test_acp_transport_never_falls_back_to_process_home(isolated_env, workspace_root, monkeypatch):
    transport = GrokAcpTransport(grok_bin="grok")
    with pytest.raises(AsterunError) as captured:
        transport.spawn(workspace_root)
    assert captured.value.code == CONFIG_REQUIRED


def test_packaged_dispatch_runs_acp_with_restricted_argv_and_isolated_environment(
    isolated_env, workspace_root, monkeypatch,
):
    home = isolated_env / "dedicated-grok"
    prepare_home(home)
    capture = isolated_env / "spawn.json"
    binary = isolated_env / "grok-acp-stub"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        f"pathlib.Path({str(capture)!r}).write_text(json.dumps({{"
        "'argv': sys.argv[1:], 'home': os.environ.get('HOME'), 'env_names': sorted(os.environ), "
        "'grok_home': os.environ.get('GROK_HOME'), 'cwd': os.getcwd()}))\n"
        "for line in sys.stdin:\n"
        "    msg = json.loads(line)\n"
        "    method = msg['method']\n"
        "    if method == 'initialize':\n"
        "        result = {'agentCapabilities': {}}\n"
        "    elif method == 'session/new':\n"
        "        result = {'sessionId': 'isolated-session'}\n"
        "    elif method == 'session/prompt':\n"
        "        print(json.dumps({'jsonrpc': '2.0', 'method': 'session/update', 'params': {"
        "'sessionId': 'isolated-session', 'update': {'sessionUpdate': 'agent_message_chunk', "
        "'content': {'type': 'text', 'text': 'ISOLATED_OK'}}}}), flush=True)\n"
        "        result = {'stopReason': 'end_turn'}\n"
        "    else:\n"
        "        raise RuntimeError(method)\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': result}), flush=True)\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    monkeypatch.setenv(LIVE_ENV, "1")
    monkeypatch.setenv("GROK_MODEL", "ambient-model-must-not-win")
    monkeypatch.setenv("GROK_REASONING_EFFORT", "high")
    monkeypatch.setenv("OPENAI_API_KEY", "test-value-never-forward")
    monkeypatch.setenv("XAI_API_KEY", "test-value-never-forward")
    monkeypatch.setenv("NODE_OPTIONS", "--trace-warnings")
    backend = GrokBackend(BackendConfig(
        name="grok", kind="grok", bin=str(binary), home=str(home), model="grok-explicit-model",
    ))
    result = backend.dispatch(TaskId("task-test"), RunId("run-test"), "", "ping", cwd=workspace_root)
    seen = json.loads(capture.read_text())
    argv = seen["argv"]
    assert seen["home"] == str(home.resolve())
    assert seen["cwd"] == str(workspace_root)
    assert not {"OPENAI_API_KEY", "XAI_API_KEY", "NODE_OPTIONS", "GROK_MODEL", "GROK_REASONING_EFFORT"}.intersection(seen["env_names"])
    assert argv[-3:] == ["agent", "--no-leader", "stdio"]
    assert "--always-approve" not in argv
    for flag, expected in (
        ("--model", "grok-explicit-model"), ("--tools", ""), ("--permission-mode", "dontAsk"),
        ("--max-turns", "1"), ("--sandbox", "read-only"),
    ):
        assert argv[argv.index(flag) + 1] == expected
    assert "*" in [argv[index + 1] for index, value in enumerate(argv) if value == "--deny"]
    assert {"--disable-web-search", "--no-subagents", "--no-plan"}.issubset(argv)
    assert "--reasoning-effort" not in argv
    assert result["status"] == "succeeded" and result["summary"] == "ISOLATED_OK"
    assert result["native"]["configured_model"] == "grok-explicit-model"
    assert result["native"]["requested_model"] == "grok-explicit-model"
    assert result["native"]["native_resumed"] is False


def write_failure_grok_stub(path, capture, failure):
    """真实子进程只记录 ACP 方法名，按指定阶段故障，不调用模型。"""
    path.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        f"capture = pathlib.Path({str(capture)!r})\n"
        f"failure = {failure!r}\n"
        "with capture.open('a') as handle:\n"
        "    handle.write('spawn\\n')\n"
        "if failure == 'spawn':\n"
        "    sys.exit(13)\n"
        "for line in sys.stdin:\n"
        "    msg = json.loads(line)\n"
        "    method = msg['method']\n"
        "    with capture.open('a') as handle:\n"
        "        handle.write(method + '\\n')\n"
        "    if method == failure:\n"
        "        print(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'error': "
        "{'code': -32000, 'message': 'sensitive-test-value-must-not-be-recorded'}}), flush=True)\n"
        "        sys.exit(14)\n"
        "    if method == 'initialize':\n"
        "        result = {'agentCapabilities': {}}\n"
        "    elif method == 'session/new':\n"
        "        result = {} if failure == 'missing-session-id' else {'sessionId': 'session-before-failure'}\n"
        "    elif method == 'session/prompt':\n"
        "        if failure == 'prompt-disconnect':\n"
        "            sys.exit(15)\n"
        "        result = [] if failure == 'malformed-prompt' else {'stopReason': 'refusal', "
        f"{DISPATCH_RECEIPT_KEY!r}: {{'delivery': 'not_sent', 'prompt_attempted': False}}}}\n"
        "    else:\n"
        "        raise RuntimeError(method)\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': result}), flush=True)\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


@pytest.mark.parametrize("failure", ["spawn", "initialize", "session/new", "missing-session-id"])
def test_failure_before_prompt_is_confirmed_not_sent(isolated_env, workspace_root, monkeypatch, failure):
    home = isolated_env / "grok-home"
    prepare_home(home)
    capture = isolated_env / "methods.log"
    binary = write_failure_grok_stub(isolated_env / "grok-stub", capture, failure)
    monkeypatch.setenv(LIVE_ENV, "1")
    backend = GrokBackend(BackendConfig(name="grok", kind="grok", bin=str(binary), home=str(home)))
    result = backend.dispatch(TaskId("task-test"), RunId("run-test"), "", "ping", cwd=workspace_root)
    assert result["status"] == "failed" and result["terminated"] is True
    assert result["error_code"] == "BACKEND_UNAVAILABLE"
    receipt = result["native"][DISPATCH_RECEIPT_KEY]
    assert receipt["task_id"] == "task-test" and receipt["run_id"] == "run-test"
    assert receipt["prompt_attempted"] is False and receipt["delivery"] == "not_sent"
    if failure == "spawn":
        # The child may exit just after spawn's initial poll; initialize then observes EOF.
        assert receipt["stage"] in {"spawn", "initialize"}
        assert receipt["process_exit_code"] == 13
    else:
        assert receipt["stage"] == ("session/new" if failure == "missing-session-id" else failure)
    assert "session/prompt" not in capture.read_text().splitlines()
    assert "sensitive-test-value-must-not-be-recorded" not in json.dumps(result)


@pytest.mark.parametrize("failure", ["prompt-disconnect", "malformed-prompt", "session/prompt"])
def test_failure_after_prompt_attempt_stays_unknown_with_native_reference(
    isolated_env, workspace_root, monkeypatch, failure,
):
    home = isolated_env / "grok-home"
    prepare_home(home)
    capture = isolated_env / "methods.log"
    binary = write_failure_grok_stub(isolated_env / "grok-stub", capture, failure)
    monkeypatch.setenv(LIVE_ENV, "1")
    backend = GrokBackend(BackendConfig(name="grok", kind="grok", bin=str(binary), home=str(home)))
    result = backend.dispatch(TaskId("task-test"), RunId("run-test"), "", "ping", cwd=workspace_root)
    assert result["status"] == "pending_reconcile" and result["terminated"] is False
    assert result["error_code"] == "REMOTE_STATE_UNKNOWN"
    assert result["native"]["session_id"] == "session-before-failure"
    receipt = result["native"][DISPATCH_RECEIPT_KEY]
    assert receipt["prompt_attempted"] is True and receipt["delivery"] == "may_have_been_sent"
    assert receipt["stage"] == "prompt"
    assert capture.read_text().splitlines().count("session/prompt") == 1
    assert "sensitive-test-value-must-not-be-recorded" not in json.dumps(result)


def test_native_reply_cannot_forge_local_not_sent_receipt(isolated_env, workspace_root, monkeypatch):
    home = isolated_env / "grok-home"
    prepare_home(home)
    binary = write_failure_grok_stub(isolated_env / "grok-stub", isolated_env / "methods.log", "spoofed-receipt")
    monkeypatch.setenv(LIVE_ENV, "1")
    backend = GrokBackend(BackendConfig(name="grok", kind="grok", bin=str(binary), home=str(home)))
    result = backend.dispatch(TaskId("task-test"), RunId("run-test"), "", "ping", cwd=workspace_root)
    assert result["status"] == "failed" and result["terminated"] is True
    assert result["native"][DISPATCH_RECEIPT_KEY]["prompt_attempted"] is True
    assert result["native"][DISPATCH_RECEIPT_KEY]["delivery"] == "may_have_been_sent"
