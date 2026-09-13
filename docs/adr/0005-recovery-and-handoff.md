# ADR 0005：对账不重派与唯一编排所有者

日期：2026-09-07。状态：已采纳（A2-02 离线）。

## 决策

发出请求后丢失回复或超时，运行进入 `pending_reconcile`，操作进入 `unknown`，错误码为 `REMOTE_STATE_UNKNOWN`。`task.reconcile` 只整理本地意图与运行事实，不重派后端。取消先记 `cancel_requested`；后端完成与取消竞争时保留两件事实，按实际结果归档。

审批答复必须匹配待处理请求、运行、任务和目标摘要；过期或错绑定不转发，也不把授权改成已通过。

`orchestration_owner` 在 `core` 与 `human` 之间显式切换。`task.handoff pause` 停止自动推进；同一 conversation 的新 `task.submit` 被拒绝，直到 `resume`。外部修改使旧验收证据失效，不把旧 `passed` 带到新版本。

## 回退

把 `task.handoff` / `task.reconcile` 改回拒绝，并删除 fake 的 `lost_reply` / `timeout` / `cancel_race_complete` 脚本。已写入的 `paused`、`evidence_stale` 字段可忽略。
