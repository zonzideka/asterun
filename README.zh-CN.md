[English](README.md) | 简体中文

# Asterun

Asterun 是面向编码 Agent 的自托管任务运行器，通过一套 CLI、一个 MCP 服务和一个本地套接字对外提供。Codex、Grok、Claude 和 Antigravity 是本机 CLI 后端。Cursor 插件运行本地 Cursor SDK，并使用 `CURSOR_API_KEY`。Jules 插件调用远程 REST，并使用 `JULES_API_KEY`。认证方式为原生登录，或显式的 API key 引用。

状态：alpha（`0.1.0aN` 预发布）。首版范围已冻结；已验证的内容见[发行说明](docs/release-v0.1.md)。采用 [Apache-2.0](LICENSE) 许可证。

## 它解决什么问题

各 Agent CLI 有自己的会话、审批提示和失败模式。直接在终端中运行 Agent，会把任务观察和生命周期管理绑在该终端上。Asterun 在它们前面放置常驻核心：

- 调用方断开后，任务继续运行。每个任务、运行和原生会话引用都保存在 SQLite 中，调用方可以稍后返回、观察或对账。
- 提交是幂等的。在同一幂等范围内，用同一键重试同一请求会返回已有任务。输入不同则返回 `IDEMPOTENCY_CONFLICT`。
- 未知结果保持为 `pending_reconcile`，不会被报告为成功或失败。
- 核心在派发前检查权限和额度准入，并在执行期间居中处理受支持的原生审批请求。

## 核心概念

| 概念 | 含义 |
|---|---|
| 工作区 | 任务可以在其中运行的已命名目录。核心只接受已配置的工作区作为任务工作目录；实际的文件访问边界取决于后端权限和另行配置的操作系统隔离。 |
| 后端 | 已命名、已配置的 Agent 接口。内置种类：`codex`、`grok`、`claude`、`antigravity` 和 `fake`（离线测试）。插件可以增加更多种类。 |
| 任务 / 运行 | 任务是工作单元；每次执行尝试是一次运行，有自己的 `run_id`。`conversation_id` 关联后续任务。 |
| 候选组 | 按角色命名的后端组（`implementation`、`review`、`planning`）。向 `@group` 提交后，核心从中选择成员；`routing-explain` 说明原因。审查组与实现组不能共享成员。 |
| 审批 | 支持原生审批桥的后端请求权限时，任务进入 `waiting_input`；用 `approval-respond` 答复。Claude 和 Antigravity 对审批返回 `CAPABILITY_UNSUPPORTED`。Jules 的计划审批使用 `capability.invoke`（`remote.plan.approve`）。见[审批说明](docs/approval-policy.md)。 |
| 额度与计费池 | 共用同一账户的后端共享一个带并发上限的计费池。本地额度缓存（`quota-inspect`）和严格的额度耗尽闩锁会停止向已耗尽的池派发。组内按额度优选默认关闭。 |
| 上下文胶囊与预算 | 可选的 `context_budget` 配置：`off`（默认）、`observe`（记录 token 用量和胶囊）或 `enforce`（按任务做 token 预算准入，并对列出的后端按需分段）。见[插件与配置](docs/plugins.md#上下文预算context_budget)。 |
| 工作流 | 针对固定修订的可选检查、独立审查、有界修复和 PR 审查配方。见 [PR 审查说明](docs/pr-review.md)。 |

## 安装

需要 Python 3.11 或更新版本。从[发行页](https://github.com/zonzideka/asterun/releases)下载 wheel 和 `SHA256SUMS`，校验后安装到虚拟环境：

```sh
# Linux
sha256sum -c SHA256SUMS --ignore-missing
# macOS
shasum -a 256 -c SHA256SUMS --ignore-missing
python3 -m venv .venv
.venv/bin/python -m pip install ./asterun-*.whl
.venv/bin/asterun version
```

可选后端插件（Antigravity、Cursor、Jules）是同一发行页上的独立 wheel；见[插件说明](docs/plugins.md)。

## 快速开始

先试离线的[非代码样例](examples/non-code/README.md)；它使用 `fake` 后端，不需要账户。

处理真实任务时，先编写配置（见[安装与配置](docs/install.md)），再启动核心：

```sh
asterun --config /path/to/config.json --state-dir /path/to/state serve
```

在另一个终端提交并观察任务：

```sh
asterun --state-dir /path/to/state --connect task-submit \
  --workspace demo --backend BACKEND --input - \
  --idempotency-key first-task < prompt.txt
asterun --state-dir /path/to/state --connect task-watch TASK_ID --timeout 60
```

用 `task-get` 读取结果；超时或断开后使用 `task-reconcile`。MCP 客户端运行 `asterun --state-dir /path/to/state --connect mcp`。

## 配置

配置是一份 JSON 文件，`revision` 单调递增。用 `asterun --config FILE config-validate` 校验。修改配置后，重启核心以加载，再用 `config-apply --expected-applied-revision N` 应用已加载的修订。Schema v2 增加插件注册、供应商账户、连接、计费池、候选组、`quota` 和 `context_budget`；v1 文件仍然有效。可从 [`examples/`](examples/) 中的示例和[安装说明](docs/install.md)开始。

## 后端

| 后端 | 支持 |
|---|---|
| Codex（内置） | 文件读取与编辑、命令和测试、原生线程续接、原生命令与文件变更审批、中断、对账。可选[固定范围读取](docs/fixed-read-scope.md)和[桌面项目关联](docs/codex-projects.md)。 |
| Grok（内置） | 文本任务（`text-only-v1`），以及在工作区中进行自主编码（`workspace-code-v1`），回合数和超时有界。见[Grok 自主编码](docs/install.md#grok-自主编码配置)。 |
| Claude（内置） | 单次任务、文本处理和工作区文件读取。 |
| Antigravity（内置或插件） | 通过 `agy` CLI 和专用权限 profile 执行文本任务，并只读访问工作区文件。见 [Antigravity 说明](docs/antigravity.md)。 |
| Cursor（插件） | 本地 Cursor SDK Agent 任务（`CURSOR_API_KEY`）、会话恢复、取消。 |
| Jules（插件） | 远程 REST 编码任务（`JULES_API_KEY`）、计划获取与审批、反馈、状态。 |

各后端和平台的验证状态见[发行说明](docs/release-v0.1.md)。

## 安全模型

- Asterun 保存账户引用。认证方式为原生登录，或显式的 API key 引用：本机 CLI 后端使用原生登录，Cursor 使用 `CURSOR_API_KEY`，Jules 使用 `JULES_API_KEY`。子进程获得过滤后的环境。
- 核心只接受已配置的工作区作为任务工作目录。Codex 默认原生审批模式为 `untrusted`，沙箱为 `workspace-write`。Grok 编码使用原生工作区沙箱、固定工具允许列表和 `dontAsk` 权限模式。Antigravity 的 profile 只允许读取已绑定的工作区，并拒绝写入、命令、URL 和 MCP 工具。文件访问边界取决于这些后端权限，以及另行配置的操作系统隔离。
- 权限提示默认不会自动批准。无人值守时，需要审批的拒绝会返回明确错误。Antigravity 策略硬拒绝在产出最终答复时仍可能得到成功的运行；拒绝元数据会保留。
- 核心监听同一用户的 Unix 套接字。调用方通过自己的通道认证；跨主机传输由调用方负责。
- 操作系统隔离另行配置，上述权限设置并不隐含操作系统隔离。

完整模型见[设计](https://github.com/zonzideka/asterun/blob/main/docs/design.md)。

## 文档

- [用户手册](docs/user-manual.md)
- [安装与配置](docs/install.md)
- [插件与 v2 配置](docs/plugins.md)
- [设计](https://github.com/zonzideka/asterun/blob/main/docs/design.md)
- [runner-control/v1 协议](docs/protocol/IMPLEMENTATION.md)和 [capability profile](docs/capability-profile.md)
- [标准 skill](docs/standard-skill.md)
- [发行说明](docs/release-v0.1.md)和[状态](https://github.com/zonzideka/asterun/blob/main/STATUS.md)

## 许可证

[Apache-2.0](LICENSE)
