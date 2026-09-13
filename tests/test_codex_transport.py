import json
from io import StringIO
from types import SimpleNamespace

from asterun.transport import LineJsonRpcTransport


def test_transport_accepts_response_without_jsonrpc_field():
    transport = LineJsonRpcTransport()

    transport._handle_message({"id": 1, "result": {"ok": True}})

    assert transport._responses[1]["result"] == {"ok": True}


def test_transport_routes_requests_notifications_and_messages_once():
    transport = LineJsonRpcTransport()
    requests = []
    notifications = []
    messages = []
    transport.on_request(requests.append)
    transport.on_notification(notifications.append)
    transport.on_message(messages.append)

    request = {"id": "req-1", "method": "item/tool/requestUserInput", "params": {"questions": []}}
    notification = {"method": "thread/started", "params": {"threadId": "t1"}}
    transport._handle_message(request)
    transport._handle_message(notification)

    assert requests == [request]
    assert notifications == [notification]
    assert messages == [request, notification]


def test_transport_writes_notification_with_jsonrpc_field():
    transport = LineJsonRpcTransport()
    stdin = StringIO()
    transport.proc = SimpleNamespace(stdin=stdin)

    transport.notify("initialized", {})

    assert json.loads(stdin.getvalue()) == {
        "jsonrpc": "2.0",
        "method": "initialized",
        "params": {},
    }
