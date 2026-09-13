import socket
import threading
import time

from asterun.application import Application
from asterun.service import LocalClient
from tests.test_native_lifecycle import native_config, wait_for


def test_native_process_final_phase_survives_reconciliation(native_config, isolated_env, workspace_root):
    app = Application.from_paths(native_config, isolated_env/'state', background=True)
    try:
        result = app.handle('task.submit', {'workspace':'demo','backend':'codex','text':'phase-output'})
        assert result.ok
        task_id = result.data['task']['id']
        completed = wait_for(app, task_id, lambda row: row['run']['status']=='succeeded')
        assert completed['run']['summary']=='{"answer":42}'
        native = completed['run']['native']
        reread = app.backends['codex'].reconcile_native(native, cwd=workspace_root)
        assert reread['summary']==completed['run']['summary']
        assert reread['native']==native
    finally:app.close()


def test_local_client_slow_fragments_share_one_deadline(tmp_path):
    # UNIX socket path 限长；pytest 的 tmp_path 在 macOS 可能超过上限。
    from tempfile import TemporaryDirectory
    from pathlib import Path
    with TemporaryDirectory(prefix='asterun-deadline-') as temp:
        path=Path(temp)/'s';ready=threading.Event()
        def server():
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as listener:
                listener.bind(str(path));path.chmod(0o600);listener.listen();ready.set()
                conn,_=listener.accept()
                with conn:
                    conn.recv(4096)
                    for byte in b'{"ok": true, "data": {}}\n':
                        try:conn.send(bytes([byte]));time.sleep(.03)
                        except OSError:break
        thread=threading.Thread(target=server);thread.start();assert ready.wait(2)
        try:
            start=time.monotonic();result=LocalClient(path,timeout=.15).handle('task.get',{'task_id':'task'})
            assert not result.ok and time.monotonic()-start < .5
        finally:thread.join(2)
