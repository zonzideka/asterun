import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from asterun.transport import LineJsonRpcTransport


def test_concurrent_rpc_requests_and_large_stderr_do_not_block(tmp_path):
    script = tmp_path / "peer.py"
    script.write_text('''import sys, json
sys.stderr.write("x" * 200000)
sys.stderr.flush()
for line in sys.stdin:
    message = json.loads(line)
    print(json.dumps({"id": message["id"], "result": message["params"]}), flush=True)
''')
    transport = LineJsonRpcTransport()
    transport.spawn([sys.executable, str(script)], cwd=tmp_path)
    process = transport.proc
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda i: transport.request("echo", {"number": i}, timeout=5), range(20)))
        assert results == [{"number": i} for i in range(20)]
    finally:
        transport.close()
    assert process.poll() is not None
    assert process.stdin.closed and process.stdout.closed


def test_eof_wakes_pending_request_without_waiting_full_timeout(tmp_path):
    transport = LineJsonRpcTransport()
    transport.spawn([sys.executable, "-c", "import sys; sys.stdin.readline()"], cwd=tmp_path)
    try:
        with pytest.raises(ConnectionError):
            transport.request("lost", timeout=60)
    finally:
        transport.close()


def test_closed_transport_cannot_spawn(tmp_path):
    transport = LineJsonRpcTransport()
    transport.close()
    with pytest.raises(ConnectionError):
        transport.spawn([sys.executable, "-c", "raise RuntimeError('must not start')"], cwd=tmp_path)
