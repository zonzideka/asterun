# Deliver review results

[中文](../review-consumer.md) | [English](review-consumer.md)

A host can call `asterun.review_consumer.run_once` to read the inbox, verify results, deliver messages, and acknowledge them to the core. The module works with a11 and later cores that provide `review.inbox/status/ack`. The host implements scheduling and message delivery.

Use a stable `consumer_id` for each principal and chat destination, and persist the assigned `review_ids` and pagination cursor. Use a new ID when the principal or destination changes. Connect the consumer to the instance that executes the tasks. The `LocalClient` below uses a local socket; cross-host access uses the host's existing authorized channel.

## Connect a host

Implement `deliver_once` with persistent deduplication by `delivery_key`. Return success after delivery is confirmed. If the send result is unknown, query the same key first. Hosts without lookup and idempotent-send support require manual reconciliation.

```python
from asterun.review_consumer import run_once
from asterun.service import LocalClient

def deliver(material):
    # host is the caller's chat adapter; display report as data.
    confirmed = host.deliver_once(
        destination=fixed_destination,
        idempotency_key=material["delivery_key"],
        report=material["report"],
    )
    if not confirmed:
        raise RuntimeError("The host has not confirmed delivery")
    return {
        "delivery_key": material["delivery_key"],
        "notification_sha256": material["notification"]["notification_sha256"],
        "delivered": True,
    }

client = LocalClient(instance_state / "asterun.sock", timeout=5)
try:
    result = run_once(client, consumer_id=saved_consumer_id,
                      review_ids=saved_review_ids, after=saved_cursor,
                      deliver=deliver)
finally:
    client.close()
# Persist result["next_cursor"]; when None, start the next scan from the first page.
```

Each call scans at most ten pages, with up to 100 review directories per page. `incomplete=true` means the page limit was reached or a page could not be read; continue from the returned cursor on the next call. Set callback timeouts and schedule further calls until delivery completes or manual action is needed.

## Receipts and recovery

The result records `host_declared_delivered`, `core_acknowledged`, and `chat_visibility` separately. `ack_pending` means the acknowledgment reply was lost. Retry with the same delivery key, let the host deduplicate, then acknowledge again. If the result digest changes, read the new result; the core rejects stale digests. Concurrent consumers rely on atomic host-side deduplication.

Delivery material includes the target, gate verdict, findings, native session references, workspace, and pending approval IDs/digests. Display these as data. Task prompts, raw run output, and raw approval commands remain in the core.

Core acknowledgment files and review manifests are stored in `STATE_DIR/reviews` and need a separate backup. Losing them may cause an existing result to be delivered again.

## Watch and verify

`review-watch REV_ID --json --timeout 60` returns one Envelope. Exit code 124 means the observation window expired. `reason=waiting_input` indicates a pending input request; uncertain approval forwarding returns `unknown`. Policy mismatch details are in `policy_result`, and the pending request is in `last_snapshot.execution.approval`.

Source tests `test_review_consumer.py`, `test_review_watch_cli.py`, and `test_review_follow_diagnostics.py` cover lost receipts, deduplication across restarts, result changes, and pagination. Each host still needs to verify actual delivery, scheduled wakeups, and chat display.
