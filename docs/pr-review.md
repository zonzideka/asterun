# GitHub PR 审查

[中文](pr-review.md) | [English](en/pr-review.md)

先固定 PR 的 head/base、源码快照和 diff，再调用已启用的后端审查。结果包含结构化门禁和 findings。新 Codex 审查默认使用[固定文件读取](fixed-read-scope.md)；测试、修复和发布由调用方安排。

## 准备工作区

需要将会话归入真实项目时，先为已取得的 PR HEAD 创建独立 detached worktree，再将项目和审查目录登记到实例配置。两目录应各自独立，例如项目 `/srv/projects/repository` 与审查目录 `/srv/reviews/repository-pr55`：

```sh
git -C /srv/projects/repository worktree add --detach /srv/reviews/repository-pr55 EXPECTED_HEAD_SHA
asterun --state-dir /path/to/state --connect review-prepare \
  --workspace project-review --project-workspace project --backend codex \
  --repo OWNER/REPOSITORY --pr 55 --attempt review-1 --head EXPECTED_HEAD_SHA
```

准备时核对 Git common dir、worktree 指针、origin、detached HEAD 和固定 SHA。审查目录须干净：未跟踪、忽略或索引隐藏的业务变更，以及符号链接、子模块和已跟踪的 `.asterun-reviews` 都会阻止准备。主项目可保留用户修改。

快照位于审查 worktree 的 `.asterun-reviews/REV_ID/`，原生 `cwd` 使用该 worktree 的根目录。新 SHA 的复审另建固定 worktree，保留旧目录供恢复使用。项目识别与展示配置见[Codex 项目登记](codex-projects.md)。

独立快照也可放在空的非 Git 工作区，其祖先须同样没有 `.git`：

```sh
asterun --state-dir /path/to/state --connect review-prepare \
  --workspace pr-review --backend codex --repo OWNER/REPOSITORY --pr 55 \
  --attempt review-1 --head EXPECTED_HEAD_SHA
```

这种方式以配置的独立工作区作为原生 `cwd`，客户端可能按该目录显示项目。`--repo` 指定 GitHub 审查目标。当前 GitHub 适配使用 `github.com`，`gh` 应已用操作者身份授权。

## 提交与观察

`review-prepare` 下载固定 head 的源码归档和 base→head 的 diff，下载后重新核对 SHA。压缩体积上限 32 MiB、解压流上限 64 MiB、最多 20,000 个成员。归档会校验路径、成员类型和重复项，源码普通文件去除可执行位。

保存返回的 `id`、head/base、`task_input_hash`、`config_revision` 和幂等键，后续操作按这些值核对本轮审查。清单位于 `STATE_DIR/reviews/REV_ID/manifest.json`。重试同一次审查时，repo、PR、head/base、工作区、后端和 attempt 均保持原值。

```sh
asterun --state-dir /path/to/state --connect review-submit REV_ID
asterun --state-dir /path/to/state --connect review-watch REV_ID --json --timeout 60
asterun --state-dir /path/to/state --connect review-status REV_ID
```

提交时重新检查快照、版本和配置。已有任务引用时回读原记录；回复丢失后，用原 review ID 和幂等键继续查询。

`review-watch --json` 的 stdout 包含一个最终 JSON Envelope。观察到期退出码为 124，继续观察同一 review ID。到结论、审批、未知或暂停状态时提前返回。省略 `--json` 时输出含过程记录。

使用 `--apply-policy` 可应用管理员预先配置的[审批规则](approval-policy.md)。未匹配时，`policy_result` 返回理由，当前待批对象在 `last_snapshot.execution.approval`。显式 shell 包装仍需人工处理；固定读取模式通过专用工具读取文件。

`review-status.result` 返回门禁、P0/P1/P2 计数、发布准备状态和下一步。`invalid_result` 表示缺少有效的绑定结论。Codex 标注输出阶段时以 `final_answer` 为结论来源，过程说明保留在运行事件中。

该流程使用兼容 `task.submit`，其 `standard_control_budget` 为 `not_applicable`，`automatic_quality_gate` 为 `not_configured`。需要标准预算或自动质量流程时使用对应协议入口。

## 关联复审

新 SHA 的正式复审创建新的审查对话，并带上上轮结果：

```sh
asterun --state-dir /path/to/state --connect review-prepare \
  --workspace project-review-next --project-workspace project --backend codex \
  --repo OWNER/REPOSITORY --pr 55 --attempt review-2 --head NEXT_HEAD_SHA \
  --previous-review PREVIOUS_REV_ID --max-rounds 3
```

关联复审会保存上轮 SHA、有效输出摘要和 findings，本轮先复核旧问题，再检查回归。轮次上限默认 3，可设为 1-10；后续轮次保持或降低原上限。达到上限后，由人工处理残留问题，门禁继续按 findings 判定。

不同 PR 使用独立对话。通用任务可用 Asterun `conversation_id` 续接 Codex 线程；正式审查配方按新的审查身份创建会话。同一已提交 review 的查询与同键重试继续回读原任务。

## 交付结论

`review-inbox --consumer-id chat-review` 返回当前主体尚未确认的结论和阻塞，按 review ID 分页。`next_cursor` 非空时用 `--after` 继续；下周期从第一页重新扫描。

宿主读完整结论并发送到指定聊天，确认送达后执行：

```sh
asterun --state-dir /path/to/state --connect review-ack REV_ID \
  --consumer-id chat-review --notification-sha256 EXACT_NOTICE_SHA256
```

为每个调用主体和聊天目的地固定一个 `consumer_id`。摘要变化时重新读取和交付。宿主负责定时唤醒及消息发送，可用[结果交付模块](review-consumer.md)连接 inbox、回执与 ack。MCP 对应 `review_status`、`review_inbox`、`review_ack`。

## 发布评审

模型结果须为带本次 `target_hash` 的 JSON，finding 包含 summary、相对路径和有效行号，summary 以 `[P0]`、`[P1]` 或 `[P2]` 开头。有 P0/P1 时为 `REQUEST_CHANGES`，否则为 `APPROVE`；解析失败、目标不符或运行未成功时保留无效结论。

```sh
asterun --state-dir /path/to/state --connect review-publish-plan REV_ID
```

该命令生成固定 repo/PR、commit SHA、正文、门禁和唯一标记的 `publication.json`。findings 以正文中的文件/行号引用发布。先审阅完整计划，确认发布授权后，再用返回的 SHA-256 执行本机发布命令：

```sh
asterun --state-dir /path/to/state review-publish REV_ID \
  --plan-sha256 APPROVED_PLAN_SHA256
```

发布前再次核对快照与 PR head/base。如果当前 GitHub 身份是 PR 作者，发布事件使用 `COMMENT`，正文保留技术门禁。POST 前保存发布意图。回复不确定时，按同一作者、commit、标记和正文查询已有评审；结果仍不明确则保留 unknown，先对账再处理。
