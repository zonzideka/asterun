[中文](fixed-read-scope.md) | [English](en/fixed-read-scope.md)

# 固定文件读取

新建 Codex PR 审查默认使用 `native_fixed_scope`。提交固定快照和 diff 后，模型通过 `asterun_read` 列出文件、按行读取和搜索字面量。读取范围绑定在任务上，范围内读取无需逐条 shell 审批。

## 选择读取模式

`review-prepare --read-mode auto` 为新 Codex 审查选择固定读取。使用 `native` 可显式指定该入口，使用 `manual` 则保留原生逐条审批。已有 attempt 的 `auto` 沿用原模式；切换模式需创建新的 attempt。

通用 CLI JSON 请求和 MCP `task_submit` 也接受 `read_scope`，其中包含工作区相对路径 `manifest_path` 和 `manifest_sha256`。清单协议为 `asterun-read-scope/v1`，每个文件记录虚拟 `name`、相对 `path`、字节数和 SHA-256。读取范围计入任务输入哈希，同一原生会话保持相同范围；续接时会重新安装本机读取处理器。

## 文件核验和分页

Asterun 逐级打开目录和普通文件，每次调用核对清单，每次读取核对大小、内容摘要及读取期间的变化。PR 配方还会核对清单是否恰好覆盖本次源码和 diff。符号链接、越界路径和特殊文件会返回错误。

范围最多包含 20,001 个文件，总量上限 96 MiB；清单上限 8 MiB，单文件上限 64 MiB。文本须为严格 UTF-8。每次工具输出上限 64 KiB，超出后按返回字段继续读取：

| 操作 | 续读字段 |
|---|---|
| `list` | `next_offset` |
| `read` | `next_start_line`、`next_start_column` |
| `search` | `next_cursor` |

行号从 1 开始，字符列从 0 开始。搜索按字面量匹配，每个匹配行返回一条结果，并核验所有选中文件。文件损坏或不可读时会明确报错。

## 原生权限和验证状态

启动前，Asterun 在隔离临时 HOME 中导出所需 schema，随后核验实际线程配置。进程和线程会关闭 shell、写入、外部工具、MCP、应用、插件、Web 及自动仓库指令来源，确认生效后发送 prompt。原生 HOME 中的全局 AGENTS 指令继续继承，工具访问仍限定在固定范围内。接口或配置不兼容时任务失败；远端结果不确定时保留对账状态。

普通任务、旧审查和其他后端沿用原审批路径。独立质量 reviewer 使用自己的 `snapshot_only_no_tools` 配置。真实模型使用新读取入口的情况及客户端显示待现场验收，见[发行说明](release-v0.1.md)。
