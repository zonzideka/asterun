[中文](user-manual.md) | [English](en/user-manual.md)

# Asterun 用户手册

Asterun 通过 CLI 和 MCP 接收任务，在配置的工作区调用 Agent 后端。先按[安装说明](install.md)准备程序和账户，后端能力及验证情况见[发行说明](release-v0.1.md)。

## 提交任务，查看进度

启动 `asterun serve` 后，使用同一状态目录连接。将以下路径、工作区和后端名换成实例配置中的值：

```sh
asterun version
asterun --state-dir /path/to/state --connect diagnose
asterun --state-dir /path/to/state --connect backend-inspect --backend BACKEND
asterun --state-dir /path/to/state --connect task-submit \
  --workspace WORKSPACE --backend BACKEND --input - \
  --idempotency-key task-attempt-1 < /path/to/prompt.txt
asterun --state-dir /path/to/state --connect task-watch TASK_ID \
  --timeout 60 --request-timeout 5
```

`version` 显示客户端版本，`--connect diagnose` 的 `data.version` 显示核心版本。升级后先核对两者，再让长连 MCP 客户端重新发现工具。

保存提交返回的任务、运行和会话 ID。同一次请求重试时复用原幂等键。`task-watch` 到期返回退出码 124；继续观察时沿用任务 ID，增量读取时再带上返回的 `--run-id` 和 `--cursor`。客户端退出后，后台核心继续执行任务。

## 处理审批，读取结果

任务进入 `waiting_input` 后，先通过 `task-get` 取得审批 ID、task/run 和目标摘要，再按[审批说明](approval-policy.md)答复。Codex 的新 PR 快照审查默认使用[固定文件读取](fixed-read-scope.md)，读取范围在提交时确定。

运行状态为 `succeeded` 时，后端执行已经结束。若任务配置了确定性检查或独立审查，继续查看验收和质量状态。PR 审查通过 `review-status` 读取 `gate_verdict` 与 findings；向外部发布评审时，先准备[发布计划](pr-review.md#发布评审)。

`task-submit` 提供兼容任务接口。需要标准 Task/Run、轮次预算、产物和事件时，使用 [runner-control/v1](protocol/IMPLEMENTATION.md)；需要固定能力计划时，使用 [capability profile](capability-profile.md)。

## 续接对话，处理异常

Task 保存任务，Run 保存一次执行。Asterun 用 `conversation_id` 关联会话，后端对话由原生会话 ID 定位，各自使用对应字段。查询和同键重试会回读已有记录。

Codex 在绑定校验通过后，可通过 `task-submit --conversation-id ID` 继续原生线程。其他后端的续接情况见发行说明。

遇到超时、断线或未知结果，先读取任务和事件，再用 `task-reconcile` 对账。先处理未决运行、待批操作和外部新增轮次，再继续任务。发起取消后持续观察，直到取得后端终止回执。

若需在 Codex 中查看项目会话，开启[项目登记](codex-projects.md)，将同一原生线程关联到真实项目目录。展示未完成时，使用 `task-present TASK_ID` 重试展示步骤。

## 后续配置

[安装说明](install.md)包含配置修订、会话绑定、工作流、备份和恢复。[PR 审查](pr-review.md)说明快照、复审与发布；[结果交付](review-consumer.md)说明宿主通知接入；[插件说明](plugins.md)说明独立插件的配置与凭据引用。
