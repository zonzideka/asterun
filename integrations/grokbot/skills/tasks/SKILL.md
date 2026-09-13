---
name: asterun-bot-tasks
description: 按用户目标提交和跟踪 Asterun 任务，通过 Bot 原生 routine 持续观察并向原聊天交付。Submit and track user-defined tasks and deliver results to the originating chat.
---

# 任务跟踪 / Task tracking

## 中文

用户要求执行任务或查询已有任务时使用本技能。先读取[模板说明](https://github.com/zonzideka/asterun/blob/grokbot-template-v0.1.0-rc1/integrations/grokbot/README.md)，确认安装接入已完成。按用户目标选择 Agent、输入、真实工作区、允许动作和输出方式；单 Agent、非代码任务和用户自定步骤均使用这一入口。

安装材料从[固定版本发布页](https://github.com/zonzideka/asterun/releases/tag/grokbot-template-v0.1.0-rc1)取得，包名为 `asterun-grokbot-0.1.0-rc1.zip`，按同页 `SHA256SUMS` 核对完整包；`manifest.json` 记录源码 commit 和逐文件摘要。已有安装先读取私有安装记录，进入保存的绝对解压根目录再调用 `scripts/bridge.py`。routine 同样使用该路径，技能在宿主全局库中的位置不改变执行目录。

通过 `bridge.py submit` 登记请求并交给核心，指定 `--binding`、稳定的 `--request-id`、`--workspace`、`--backend` 和 `--input`；需要续接时添加 `--conversation-id`。全局 `--ledger` 指向已初始化的私有账本，完整命令见[配套 CLI](https://github.com/zonzideka/asterun/blob/grokbot-template-v0.1.0-rc1/integrations/grokbot/README.md#配套-cli)。实际执行结果按返回记录。

提交受理后保存 task/run/conversation 引用，关联原聊天和执行实例。同一次请求重试沿用原 request ID，回复不明时先读取原账本和任务。继续原生会话时，按后端支持和绑定检查使用原 conversation 引用。

长期任务由常驻核心执行。使用 `update_state` 的 `target=routine`、`action=create`，在原聊天创建同一 binding 的 routine，schedule 使用 `*/5 * * * *`。已有 routine 用 `resume` 恢复，执行安排变更时用 `update`。每次唤醒保持原执行位置：云端 `Shell` 不带 `machineId`，本机 `Shell` 使用安装接入时通过 `ListMachines` 选定的机器。

本轮运行一次 `poll --binding NAME --limit 3`，再读取 `inbox --binding NAME --limit 3`。检查 Envelope 及 `poll` 的错误项，保留未决任务。状态未变化时保持安静。完成下述发送、ack、末次 poll 和状态检查，按需暂停 routine 后，再结束本轮。

对 inbox 中的交付记录执行 `claim --binding NAME --delivery-key KEY`。按保存的私有聊天别名核对当前聊天和 routine 仍属于原聊天；别名用于账本关联，`SendToUser` 使用当前聊天发送。仅在 `ok=true`、`data.already_claimed=false` 且 `data.delivery_state=sending` 时调用 `SendToUser`，设置 `type=text`、`end_turn=false`，将 `content` 原样取自 `data.content`。`already_claimed=true` 时跳过发送。

取得 `SendToUser` 返回的不透明消息 ID 后，使用原 delivery key、该 ID 和 `data.content_sha256` 执行 `ack`。账本记录 `host_reported_delivered`，实际聊天显示另行确认。领取后的发送结果未知时保持 `sending`，按[恢复技能](https://github.com/zonzideka/asterun/blob/grokbot-template-v0.1.0-rc1/integrations/grokbot/skills/recovery/SKILL.md)处理。

任务进入 `waiting_input` 后，读取实际审批对象、task/run 和动作摘要。核心中已配置且匹配的授权按原范围处理；需要用户决定时，将待批消息按上述交付流程发回原聊天。用户答复后使用 Asterun 对应接口，按实际审批绑定处理。普通任务、PR 配方和质量流程各按用户选定的步骤推进。

任务完成后读取结果、产物和配置的验收状态。用户目标决定直接交付、继续另一步、执行检查或安排审查。后续提交作为明确的新步骤，保留原任务关系，并沿用该步骤的权限和预算约定。

交付并 ack 后再次运行 `poll`，核对当前运行，再读取 `status`。`needs_followup=true` 时保留 routine，包含活动任务、待发消息及 `sending` 记录；为 `false` 时使用 `update_state` 的 `action=pause` 暂停。提交新任务后恢复。用户主动结束跟踪时保留原任务和交付记录；任务暂停、取消和接管按恢复技能处理。

## English

Use this skill to execute work or inspect an existing task. Read the [template guide](https://github.com/zonzideka/asterun/blob/grokbot-template-v0.1.0-rc1/integrations/grokbot/README.en.md) and confirm setup is complete. Choose an Agent, inputs, real workspace, allowed actions, and output format according to the user's objective. This entry point covers a single Agent, non-code work, and user-defined sequences.

Obtain installation material from the [fixed release page](https://github.com/zonzideka/asterun/releases/tag/grokbot-template-v0.1.0-rc1): `asterun-grokbot-0.1.0-rc1.zip`, checked against its `SHA256SUMS`. The included `manifest.json` records the source commit and individual file digests. For an existing installation, read the private setup record and enter its saved absolute extraction root before calling `scripts/bridge.py`. The routine uses the same path, regardless of where the host stores the skill.

Use `bridge.py submit` to register a request and pass it to the core. Supply `--binding`, a stable `--request-id`, `--workspace`, `--backend`, and `--input`; add `--conversation-id` for continuation. The global `--ledger` selects initialized private records. See the [adapter CLI](https://github.com/zonzideka/asterun/blob/grokbot-template-v0.1.0-rc1/integrations/grokbot/README.en.md#adapter-cli). Record execution from actual responses.

Once the submission is accepted, save task/run/conversation references and associate them with the original chat and execution instance. Reuse the request ID for the same request. Read the original ledger and task if the reply is unclear. Continue native sessions with the original conversation reference where the backend and binding checks support it.

Let the persistent core execute long-running tasks. Use `update_state` with `target=routine` and `action=create` to create the binding's routine in the original chat, with schedule `*/5 * * * *`. Resume an existing routine and update it when execution arrangements change. Each wake keeps the original execution location: cloud `Shell` omits `machineId`; local `Shell` uses the machine selected through `ListMachines` during setup.

Run one `poll --binding NAME --limit 3`, then read `inbox --binding NAME --limit 3`. Check the Envelope and any poll errors, retaining unresolved tasks. Stay quiet while state is unchanged. Complete the sends, acknowledgements, final poll, and status checks below, pausing the routine if appropriate, before ending the turn.

Claim an inbox item with `claim --binding NAME --delivery-key KEY`. Use the saved private chat alias to confirm that the current chat and routine still belong to the original chat. The alias associates ledger records; `SendToUser` sends in the current chat. Call it only when `ok=true`, `data.already_claimed=false`, and `data.delivery_state=sending`, with `type=text`, `end_turn=false`, and `content` taken unchanged from `data.content`. Skip sending when `already_claimed=true`.

After `SendToUser` returns an opaque message ID, use `ack` with the original delivery key, that ID, and `data.content_sha256`. The ledger records `host_reported_delivered`; confirm actual chat visibility separately. Preserve `sending` when the outcome is unclear after a claim, and follow the [recovery skill](https://github.com/zonzideka/asterun/blob/grokbot-template-v0.1.0-rc1/integrations/grokbot/skills/recovery/SKILL.md).

When a task enters `waiting_input`, inspect the actual approval object, task/run references, and action summary. The core applies configured, matching authorization within its scope. Deliver requests requiring the user through the same delivery procedure. After a reply, use the corresponding Asterun interface with the actual approval binding. Follow the user's selected steps for ordinary tasks, PR recipes, or quality workflows.

On completion, read the result, artifacts, and configured acceptance status. The objective determines whether to deliver, continue another step, run checks, or arrange review. Submit a later step as an explicit operation linked to the original task, with its applicable permissions and budget.

After delivery, run `poll` again to check the current run, then read `status`. Retain the routine while `needs_followup=true`, including active tasks, pending messages, and `sending` records. When it is `false`, pause with `update_state`, `action=pause`. Resume after a new submission. Preserve task and delivery records if the user ends tracking. Use the recovery skill for task pause, cancellation, and handoff.
