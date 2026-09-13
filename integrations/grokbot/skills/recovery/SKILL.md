---
name: asterun-bot-recovery
description: 处理 Asterun 任务的观察到期、连接中断、审批、未知结果、取消和聊天交付恢复。Recover tracking, reconcile uncertain results, and handle cancellation or delivery interruptions.
---

# 恢复与接管 / Recovery and handoff

## 中文

观察到期、连接中断、Bot 重启或任务需要用户接管时使用本技能。先读取私有账本和安装记录，确认原实例、聊天、执行主机、工作区、task/run/conversation、request ID 和交付状态。

需要恢复安装材料时，从[固定版本发布页](https://github.com/zonzideka/asterun/releases/tag/grokbot-template-v0.1.0-rc1)取得 `asterun-grokbot-0.1.0-rc1.zip`，按同页 `SHA256SUMS` 核对完整 ZIP。`manifest.json` 保留源码 commit 和逐文件摘要。运行配套脚本前进入私有记录中的绝对解压根目录，继续使用原账本；技能移入宿主全局库后仍使用该目录及正文的固定标签文档链接。

重连时先核对核心和后端，再读取原任务与事件。云端使用不带 `machineId` 的 `Shell`；本机使用已核对的机器 ID，需要重新选择时用 `ListMachines`。观察到期后沿用任务 ID，原实例暂时不可用时保留记录并说明阻塞。

提交或后端结果不明时，查询原任务和原生引用，按[用户手册](https://github.com/zonzideka/asterun/blob/v0.1.0a13/docs/user-manual.md)使用 `task-reconcile` 对账。缺少终态证据时保持未决状态和预算占位。后续动作从核实结果继续，同一次提交重试沿用原 request ID 和输入。

审批回复前核对实际请求、任务、运行、目标和摘要。过期或已处理的请求先刷新；绑定变化、外部新增轮次或目标文件变化时，重新核对执行条件。继续原生会话前按后端支持验证最新轮次。

用户要求暂停、取消或接管任务时，使用 Asterun 对应接口，保存操作意图并观察后续状态。取消完成以实际后端终止回执为准。停止后续编排后，将已完成内容、未决操作和恢复信息交给用户。

Bot 或 routine 恢复后，使用原 `--ledger` 和 `--binding` 运行一次小批 `poll`，再读 `inbox` 与 `status`。核对 routine 仍属于原聊天；通过 `update_state` 的 `target=routine`、`action=resume` 恢复，计划调整使用 `update`，标准 schedule 为 `*/5 * * * *`。执行主机、账户和原生状态保持原绑定，迁移按已有实例流程处理。

交付记录为 `sending` 或 `claim` 返回 `already_claimed=true` 时，保留原记录并跳过发送。当前宿主没有可用的消息历史查询入口。若已取得原 `SendToUser` 的真实消息 ID，使用该 ID 和原内容摘要补记 `ack`；否则先核对实际聊天或请用户确认。

只有在实际核对或用户明确确认该消息未发送后，才执行 `resolve --binding NAME --delivery-key KEY --not-sent`，随后重新领取。新的 `claim` 必须返回 `ok=true`、`data.already_claimed=false`，再用 `SendToUser` 的 `type=text` 原样发送固定 `data.content`。发送结果仍不确定时继续保留 `sending`。`ack` 仅记录 `host_reported_delivered`，状态回读保留其实际含义。

`needs_followup=true` 时保留 routine；待任务和交付均处理完、状态为 `false` 时，通过 `update_state` 的 `action=pause` 暂停。用户要求结束跟踪时保留未决记录。

普通任务回读任务结果。使用 PR 审查配方时，另外核对 review 状态和收件箱回执，参照[结果交付](https://github.com/zonzideka/asterun/blob/v0.1.0a13/docs/review-consumer.md)。配置、状态库、原生 HOME、插件状态和审查材料按[安装说明](https://github.com/zonzideka/asterun/blob/v0.1.0a13/docs/install.md#诊断备份与恢复)分别备份；Bot 的私有账本和宿主绑定记录另行保存。

## English

Use this skill after an observation timeout, disconnection, Bot restart, or a request for human takeover. Read the private ledger and setup records first: original instance, chat, execution host, workspace, task/run/conversation references, request ID, and delivery state.

To restore installation material, obtain `asterun-grokbot-0.1.0-rc1.zip` from the [fixed release page](https://github.com/zonzideka/asterun/releases/tag/grokbot-template-v0.1.0-rc1) and verify the complete ZIP against its `SHA256SUMS`. The included `manifest.json` retains the source commit and individual file digests. Enter the absolute extraction root saved in private records before running scripts and continue using the original ledger. Keep that directory and the fixed-tag documentation links after importing the skill into the host's global library.

On reconnection, check the core and backend, then read the original task and events. Use `Shell` without `machineId` in the cloud; locally, use the verified machine ID and `ListMachines` if it needs to be selected again. Continue observations with the same task ID. If the original instance is unavailable, retain records and report the blocker.

For an unclear submission or backend result, query the original task and native references, then reconcile with `task-reconcile` as described in the [user manual](https://github.com/zonzideka/asterun/blob/v0.1.0a13/docs/en/user-manual.md). Preserve unresolved state and budget reservations while terminal evidence is missing. Continue from verified outcomes. Retain the original request ID and input when retrying the same submission.

Before answering an approval, verify the actual request, task, run, target, and digest. Refresh expired or already handled requests. Recheck conditions after binding changes, externally added turns, or target-file changes. Validate the latest native turn before continuation where supported.

For task pause, cancellation, or handoff, use the corresponding Asterun interface, record the intent, and observe the outcome. Cancellation completes when the backend confirms termination. After stopping later orchestration steps, hand the user completed work, unresolved operations, and recovery details.

After a Bot or routine restart, run a small `poll` with the original `--ledger` and `--binding`, then read `inbox` and `status`. Confirm that the routine still belongs to the original chat. Resume with `update_state`, `target=routine`, `action=resume`; use `update` for changes and the standard schedule `*/5 * * * *`. Keep the original host, account, and native-state bindings, using the existing instance migration procedure when needed.

If a delivery is `sending` or `claim` returns `already_claimed=true`, retain the original record and skip sending. The host currently has no available message-history query interface. If the original `SendToUser` returned a real message ID, use it with the original content digest to record `ack`. Otherwise inspect the actual chat or ask the user to confirm the outcome.

Only after inspection or the user's explicit confirmation that nothing was sent, run `resolve --binding NAME --delivery-key KEY --not-sent` and claim again. The new claim must return `ok=true` and `data.already_claimed=false` before `SendToUser` sends the fixed `data.content` with `type=text`. Retain `sending` if the outcome remains uncertain. `ack` records `host_reported_delivered`; preserve that meaning when reading status.

Retain the routine while `needs_followup=true`. After tasks and deliveries are resolved and the value is `false`, pause with `update_state`, `action=pause`. Preserve unresolved records if the user ends tracking.

Read task results for ordinary work. For a PR review recipe, also inspect review state and inbox receipts; see [result delivery](https://github.com/zonzideka/asterun/blob/v0.1.0a13/docs/en/review-consumer.md). Back up configuration, database state, native HOME, plugin state, and review material separately as described in the [installation guide](https://github.com/zonzideka/asterun/blob/v0.1.0a13/docs/en/install.md#diagnostics-backup-and-recovery). Preserve the Bot's private ledger and host-binding records separately.
