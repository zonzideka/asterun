# 来源与证据范围

核验日期：2026-09-10。下面的 URL 是本次读取的一手公开文档；没有把其“文档说明”标成用户本机已经验证的能力。

S1 — OpenAI Codex SDK。线程创建/继续/恢复，以及语言 SDK 差异。
`https://developers.openai.com/codex/sdk/`
本次重定向到：`https://learn.chatgpt.com/docs/codex-sdk`

S2 — OpenAI Codex App Server。线程/轮次、审批、中断及生命周期。
`https://developers.openai.com/codex/app-server/`
本次重定向到：`https://learn.chatgpt.com/docs/app-server`

S3 — xAI Grok Build Headless & Scripting。headless、streaming-json、ACP stdio。
`https://docs.x.ai/build/cli/headless-scripting`

S4 — xAI Grok Build Sessions。会话落盘及 --resume/--session-id 语义；与 S3 的部分措辞有冲突，映射文档已注明。
`https://docs.x.ai/build/features/sessions`

S5 — xAI Grok Bot Overview。持久化云端计算环境、Bot 与用户级计算机共享边界。
`https://docs.x.ai/grok-bot/overview`

S6 — super.engineering CLI automation。应用依赖、live session、JSON flag、创建 worktree 上下文及 team 的重启行为。
`https://super.engineering/docs/cli-local-api/`

S7 — super.engineering Session history and restore。供应商续接绑定、验证失败后新建会话。
`https://super.engineering/docs/session-history-and-restore/`

S8 — super.engineering Coordination state。版本化 CAS、ownership、advisory locks。
`https://super.engineering/docs/orchestration-coordination-state/`

S9 — Agent Client Protocol v1 Overview。协商、session/load 的可选能力、prompt 和取消语义。
`https://agentclientprotocol.com/protocol/v1/overview`

S10 — JSON Schema Draft 2020-12。Schema 标准版本。
`https://json-schema.org/draft/2020-12`

S11 — RFC 8785 / JSON Canonicalization Scheme。规范化 JSON 摘要输入；该 RFC 为 Informational，不称为 IETF Standards Track。
`https://www.rfc-editor.org/info/rfc8785/`

S12 — xAI Grok Build Permissions。权限、沙箱和各执行模式的区分。
`https://docs.x.ai/build/features/permissions`

项目基线 — 已保存文件《agent-runner_通用化改进规划与GrokBot初始化规范_v0.1.md》。本次读取确认：现有 SDK 链路、持续会话、免费分发、非强制中转、唯一编排者、UNKNOWN_REMOTE、初始化门禁、授权和证据等设计基线。它是项目设计资料，不是上游平台能力认证。

本包的新对象、字段、状态机、命令、守卫、预算与恢复模型属于拟议规范。测试结果的范围单列于 VALIDATION.md，不用于证明上游产品行为。
