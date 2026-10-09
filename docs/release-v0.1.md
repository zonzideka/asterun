[中文](release-v0.1.md) | [English](en/release-v0.1.md)

# 首版发行说明

当前公开源码版本为 `0.1.0a16`，正式稳定版 `0.1.0` 尚未发布。本次预发布的安装资产和校验文件见[对应版本发行页](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a16)。首版范围已冻结，发布准备集中在现有行为、兼容和打包修复。项目采用 [Apache-2.0](../LICENSE)。

## 0.1.0a16

Linux x86_64 支持 Antigravity CLI 1.2.0 profile。`webm_encoder` 按平台核对精确大小和 SHA-256：macOS 仍只接受原有 12781442 字节构建，Linux x86_64 只接受官方 Linux 1.2.0 构建（17056035 字节，`45c1b1edd50159fbd4eac95ffd82df97a79b8c345fc43408ddae09230a304ed6`），没有通配项，Linux aarch64 的编码器仍被拒绝。Playwright 空缓存目录在 Linux 为 `~/.cache/ms-playwright-go/1.57.0`，macOS 不变。agy 1.3.2 仍不支持。

Codex 后端新增可选 `approval_policy`，可取 `untrusted`、`on-failure`、`on-request`。`on-request` 在工作区沙箱内自动读写和执行命令，越界才询问；`on-failure` 仅在沙箱导致失败时询问。省略时普通任务仍发送 `untrusted` 与 `workspace-write`。`never`、granular 对象和 `danger-full-access` 在校验时拒绝，固定读取仍为 `never` 与 `read-only`。用法见[安装说明](install.md#原生审批模式)。配置 schema 仍为 v1/v2，SQLite 不变。

已知说明：Linux 上 profile HOME 中出现 `Library` 目录现在会被拒绝，比 a15 更严格。同步 Codex `dispatch` 现在默认显式发送 `untrusted` 与 `workspace-write`，不再沿用 `~/.codex/config.toml`。已创建任务在续接 `thread/resume` 时使用当前配置中的审批模式，而不是创建时的值。

本次资产为核心 wheel、源码包、`asterun-skill-0.1.0a16.zip` 和 `SHA256SUMS`。标准 skill 源码与 a15 相同，最低接口版本仍为 a14。两项改动的 PR 均通过 Python 3.11/3.12 CI；Linux 上真实 agy 与真实 Codex 的现场验收在发布后单独记录，见[发行页](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a16)。

## 0.1.0a15

修复 Grok 编码运行返回已绑定会话的明确未完成/取消结束事件、同时以退出码 1 结束时被误判为未知的问题。只有匹配的明确结束证据可以确认终态；成功事件配合非零退出、超时、错会话和损坏协议仍保持未决。旧误判可通过原任务的 `task-reconcile` 显式核对持久化回执，不创建替代运行，不更改验收结论。

标准 skill 新增 `scheduler` 占位诊断和 `reconcile --task-id ID`，占位引用遵循任务读取权限。对账可能释放名额并启动原有队列，因此须核对原任务和返回的终态，不能把请求成功当成任务成功。建议核心与技能一起升级到 a15；技能最低接口版本仍为 a14，但 a14 核心不含本次终态修复，单独替换技能不能解决核心占位。

本次资产为核心 wheel、源码包、`asterun-skill-0.1.0a15.zip` 和 `SHA256SUMS`。修复阶段完整离线回归 2644 项通过、4 项真实后端测试跳过；独立安装的恢复专项 21 项通过，CLI/MCP、重启与备份恢复验证通过。本机修复构建已实际对账并释放旧占位，原生会话、用量与无关历史记录保持不变，未新增真实模型任务。详见[现场验证](https://github.com/zonzideka/asterun/blob/v0.1.0a15/docs/validation/skill-runtime-recovery-2026-10-03.md)，最终发行提交 CI 与资产验证见[发行页](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a15)。

## 0.1.0a14

相对已发布的 `v0.1.0a13`，源码包含其后合入的外部主控紧凑观察（`task-get --compact`、`task-watch --compact --no-events`）、`workflow-snapshot`、标准 skill 入口，以及技能对核心版本的显式门禁。标准发行版最低要求 CLI 与常驻核心均为 `0.1.0a14`；仍准确报告 a13 的历史主线构建，仅在 CLI 参数与常驻核心只读接口探测均通过后兼容。公开标签版 a13 缺少这些接口，会在业务提交前返回 `CORE_VERSION_UNSUPPORTED`。探测失败不会发出变更或只读业务命令；连接失败、超时、未知版本和无法确认的能力分别报告，规则见[标准 skill](standard-skill.md)。本次还补全授权工作区发现、后端执行模式提示、用量分页和大响应读取。

本次发行资产为核心 wheel、源码包、`asterun-skill-0.1.0a14.zip` 和 `SHA256SUMS`。三个可选插件保持 `1.0.0`，沿用 [a13 发行页](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a13)的原有 wheel、源码包和校验文件。GrokBot 模板 `0.1.0-rc1` 的发行锁仍固定公开 a13 wheel 及其 SHA-256，聊天技能行为不变；该锁安装的实例不能使用标准 skill，模板锁的升级另行验证和发布。

## 运行范围

Asterun 支持 Python 3.11 及以上，按单用户、单执行主机和本地文件系统部署。核心提供 CLI、stdio MCP、常驻执行器、持久任务和会话引用、事件、幂等、审批、取消与对账、备份恢复，以及可选质量工作流和 PR 审查配方。

CLI、MCP 和 GrokBot 等入口共用核心。同机客户端通过本地 socket 连接，远程调用通过宿主已有的授权通道接入。

## 后端支持

| 后端 | 当前能力 | 验证情况与适用条件 |
|---|---|---|
| Codex | 文件读取与编辑、命令和测试执行、原生线程续接、命令/文件审批、取消与对账、可选固定读取和项目登记 | 工具执行遵循任务权限与审批。真实文本和 PR 审查有运行记录；新固定读取的模型行为、通用续接/取消及完整新会话桌面显示待现场验收，项目登记限同机 macOS Desktop |
| Grok | 默认文本模式；显式 `workspace-code-v1` 支持读取、编辑、测试和迭代，编码模式可选 `session_policy: "resume"` 续接受管原生会话 | 文本任务在 macOS/Linux 有运行记录；自主编码在 macOS 完成一次闭环。受管续接已通过离线协议替身，真实模型续接与原生取消/客户端显示仍待现场验证；文本模式原生续接、取消及审批桥接尚未接入 |
| Claude | 单次任务调用、文本处理、工作区文件读取和固定快照输入 | 普通调用配置 Read/Grep/Glob，快照调用使用传入内容。已做离线验证，真实后端待验收；原生续接尚未接入 |
| Antigravity CLI | CLI 1.2.0 的文本执行和受限工作区读取 | macOS 的文本、读取及权限拒绝检查通过。Linux x86_64 的 profile 接受官方 1.2.0 `webm_encoder` 固定摘要和空的 `~/.cache/ms-playwright-go/1.57.0`；该平台真实执行仍待现场验收。原生续接、恢复对账和审批桥接尚未接入 |
| 独立插件 | Antigravity 账户、Cursor local Agent、Jules REST | 预览包；真实供应商、计费和客户端验收待完成 |

任务目标、Agent 组合及执行流程由用户选择。按所选后端查阅[安装说明](install.md)、[Antigravity 用法](antigravity.md)或[插件说明](plugins.md)。用量按后端提供的原始范围记录；额度节省仍需同任务对照数据验证。

Codex 固定读取按实际协议和生效权限检查兼容性。项目登记根据真实目录及 Git worktree 关系关联原线程，展示重试使用 `task-present`。Grok 编码模式需显式启用，原文本配置沿用原权限。

## 验证与升级

2026-10-03 标准 skill 修复阶段的完整本机离线回归记录为 2610 项通过、4 项真实后端测试跳过、1 项版本探测超时失败；同一失败场景随后连续三次隔离复测通过，原整套命令并非全绿。五组合兼容矩阵、独立消费者流程及两项独立 wheel 端到端验收通过，wheel/sdist 的 102 个运行文件与固定源码一致。范围与原始失败见[本次修复验收记录](https://github.com/zonzideka/asterun/blob/v0.1.0a14/docs/validation/standard-skill-repair-2026-10-03.md)。最终发行提交的 CI 和资产校验结果以[对应版本发行页](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a14)为准，不以历史快照的结果代替。本轮没有新增真实模型任务；真实产出、原生客户端与 Token 收益仍需分别验收。

配置 v1/v2、SQLite schema 6、runner-control/v1 和 capability profile 保持兼容。升级前备份配置、状态和原生 HOME，处理在途及未知执行，再切换服务并应用配置修订。升级后产生的新任务纳入恢复记录，操作步骤见[安装说明](install.md#诊断备份与恢复)。

wheel 用于安装，sdist 另含用户文档、样例和发行检查脚本。部署环境保存运行状态、原生会话和凭据，完整 Git 源码提供开发测试与来源核验材料。
