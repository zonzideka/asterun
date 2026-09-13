from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
import tempfile
import sqlite3
import time
from pathlib import Path

from asterun.service import LocalClient
from tests.test_native_lifecycle import native_config  # noqa: F401


def wait_persisted(state, run_id):
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        with sqlite3.connect(f"file:{state / 'asterun.sqlite'}?mode=ro", uri=True) as conn:
            row = conn.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()
        conn.close()
        if row and json.loads(row[0])["status"] == "succeeded":
            return
        time.sleep(0.03)
    raise AssertionError(row)


def test_cli_and_mcp_share_resident_core_and_persist_idle_completion(native_config, isolated_env):
    with tempfile.TemporaryDirectory(prefix="asterun-ipc-") as tmp:
        state = Path(tmp)
        command = [sys.executable, "-m", "asterun", "--config", str(native_config), "--state-dir", str(state)]
        process = subprocess.Popen([*command, "serve"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            client = LocalClient(state / "asterun.sock")
            deadline = time.monotonic() + 5
            while not client.path.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert client.path.exists()
            assert client.path.stat().st_mode & 0o777 == 0o600
            admitted = subprocess.run([*command, "--connect", "task-submit", "--workspace", "demo", "--backend", "codex",
                                       "--text", "finish"], capture_output=True, text=True, timeout=5)
            assert admitted.returncode == 0, admitted.stderr
            task = json.loads(admitted.stdout)
            assert task["data"]["run"]["status"] == "dispatching"
            # 不发查询，让服务自己的循环落盘。
            wait_persisted(state, task["ids"]["run_id"])
            rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "task_get", "arguments": {"task_id": task["ids"]["task_id"]}}}
            mcp = subprocess.run([*command, "--connect", "mcp"], input=json.dumps(rpc) + "\n",
                                 text=True, capture_output=True, timeout=5)
            assert mcp.returncode == 0, mcp.stderr
            report = json.loads(json.loads(mcp.stdout)["result"]["content"][0]["text"])
            assert report["data"]["run"]["status"] == "succeeded"
            duplicate = subprocess.run([*command, "serve"], capture_output=True, text=True, timeout=5)
            assert duplicate.returncode != 0 and "INSTANCE_LOCKED" in duplicate.stdout
        finally:
            process.terminate()
            out, err = process.communicate(timeout=5)
            assert process.returncode == 0, (out, err)
        assert not client.path.exists()
        reread = subprocess.run([*command, "task-get", task["ids"]["task_id"]], capture_output=True, text=True, timeout=5)
        assert json.loads(reread.stdout)["data"]["run"]["status"] == "succeeded"


def test_standalone_mcp_keeps_persisting_with_stdin_open(native_config, isolated_env):
    process = subprocess.Popen([sys.executable, "-m", "asterun", "--config", str(native_config),
        "--state-dir", str(isolated_env / "mcp"), "mcp"], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "task_submit", "arguments": {"workspace": "demo", "backend": "codex", "text": "finish"}}}) + "\n")
        process.stdin.flush()
        assert selector.select(5)
        admission = json.loads(json.loads(process.stdout.readline())["result"]["content"][0]["text"])
        wait_persisted(isolated_env / "mcp", admission["ids"]["run_id"])
        process.stdin.close()
        process.stdin = None
        out, err = process.communicate(timeout=5)
        assert process.returncode == 0, (out, err)
        from asterun.application import Application
        app = Application.from_paths(native_config, isolated_env / "mcp")
        try:
            assert app.handle("task.get", {"task_id": admission["ids"]["task_id"]}).data["run"]["status"] == "succeeded"
        finally:
            app.close()
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def test_fresh_resident_state_is_private_with_group_writable_umask(config_path):
    with tempfile.TemporaryDirectory(prefix="asterun-umask-") as tmp:
        state = Path(tmp) / "new-state"
        command = [sys.executable, "-m", "asterun", "--config", str(config_path),
                   "--state-dir", str(state), "serve"]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, umask=0o002)
        client = LocalClient(state / "asterun.sock")
        try:
            deadline = time.monotonic() + 5
            while not client.path.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            if process.poll() is not None:
                out, err = process.communicate(timeout=5)
                raise AssertionError((process.returncode, out, err))
            assert client.path.exists()
            assert state.stat().st_mode & 0o777 == 0o700
            assert client.path.stat().st_mode & 0o777 == 0o600
            assert client.handle("backend.inspect", {"backend": "fake"}).ok
        finally:
            if process.poll() is None:
                process.terminate()
            out, err = process.communicate(timeout=5)
        assert process.returncode == 0, (out, err)
        assert not client.path.exists()


def test_existing_group_writable_state_is_rejected_without_chmod(config_path):
    with tempfile.TemporaryDirectory(prefix="asterun-umask-") as tmp:
        state = Path(tmp) / "existing-state"
        state.mkdir()
        state.chmod(0o775)
        result = subprocess.run(
            [sys.executable, "-m", "asterun", "--config", str(config_path),
             "--state-dir", str(state), "serve"],
            capture_output=True, text=True, timeout=5, umask=0o002,
        )
        assert result.returncode != 0
        assert "INVALID_REQUEST" in result.stdout
        assert state.stat().st_mode & 0o777 == 0o775
        assert not (state / "asterun.sock").exists()
