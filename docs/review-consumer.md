# 审查结果交付

[中文](review-consumer.md) | [English](en/review-consumer.md)

宿主可调用 `asterun.review_consumer.run_once`，依次读取收件箱、核对结果、发送消息并向核心确认。该模块兼容提供 `review.inbox/status/ack` 的 a11 及后续核心；定时调度和实际发送由宿主实现。

为每个调用主体和聊天目的地配置稳定的 `consumer_id`，保存负责的 `review_ids` 和分页游标。更换主体或聊天时使用新的 ID。将消费者连接到实际执行实例。下面的 `LocalClient` 使用同机 socket，跨主机调用沿用宿主已有的授权通道。

## 接线示例

宿主实现 `deliver_once`，按 `delivery_key` 持久去重，确认消息送达后再返回成功。发送结果未知时，先按同一 key 查询；宿主缺少查询和幂等发送能力时，交回人工对账。

```python
from asterun.review_consumer import run_once
from asterun.service import LocalClient

def deliver(material):
    # host 是调用方的聊天适配器，report 作为数据展示。
    confirmed = host.deliver_once(
        destination=fixed_destination,
        idempotency_key=material["delivery_key"],
        report=material["report"],
    )
    if not confirmed:
        raise RuntimeError("宿主尚未确认交付")
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
# 持久保存 result["next_cursor"]，为 None 时下周期从头扫描。
```

每次最多扫描十页，每页最多 100 个审查目录。返回 `incomplete=true` 时，说明页数耗尽或读取失败，下次从返回的游标继续。宿主应设置回调超时并安排后续调度，直到结果交付完成或需要人工处理。

## 回执与恢复

返回值分别保存 `host_declared_delivered`、`core_acknowledged` 和 `chat_visibility`。`ack_pending` 表示确认回复缺失；下次继续使用同一交付 key，经宿主去重后再确认。结果摘要变化时重新读取，核心会拒绝过时摘要。并发消费者依赖宿主的原子去重。

消息材料包含目标、门禁、findings、原生会话定位、工作区和待批 ID/摘要，发送端按数据展示。任务提示、原始运行输出和原始审批命令保留在核心。

核心确认文件与审查清单位于 `STATE_DIR/reviews`，应另行备份。丢失后可能再次通知已有结果。

## 观察与验收

`review-watch REV_ID --json --timeout 60` 返回单个 Envelope。退出码 124 表示观察到期；`reason=waiting_input` 表示等待输入，审批转发不确定时为 `unknown`。策略不匹配的理由在 `policy_result`，待批对象在 `last_snapshot.execution.approval`。

源码测试 `test_review_consumer.py`、`test_review_watch_cli.py` 和 `test_review_follow_diagnostics.py` 覆盖回执丢失、重启去重、结果变化和分页。每个宿主仍需验证真实发送、定时唤醒和聊天显示。
